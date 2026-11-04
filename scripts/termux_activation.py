#!/usr/bin/env python3
"""
termux_activation.py: Focused native lifecycle driver for Termux service activation.

Exposes the LifecycleDriver interface and TermuxLifecycleDriver implementation,
reusing scripts/server_supervisor.py primitives (locking, process identity,
termination, health probing, and launching) without modifying existing supervisor
algorithms or catalog behavior.
"""

import contextlib
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


class QuiescenceError(RuntimeError):
    """Raised when a service or daemon process survives termination."""
    pass


class LifecycleDriver:
    """Abstract interface for lifecycle management."""

    def services(self) -> Dict[str, Any]:
        raise NotImplementedError

    def lock(self, name: str, blocking: bool = True, timeout: Optional[float] = None):
        raise NotImplementedError

    def matching_processes(self, sid: str) -> List[Tuple[int, str]]:
        raise NotImplementedError

    def process_identity(self, pid: int) -> Optional[str]:
        raise NotImplementedError

    def terminate(self, records: List[Tuple[int, str]]) -> None:
        raise NotImplementedError

    def stop_agy(self) -> None:
        raise NotImplementedError

    def healthy(self, sid: str, cfg: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def launch(self, sid: str, cfg: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def is_paused(self) -> bool:
        raise NotImplementedError

    def is_daemon_running(self) -> bool:
        raise NotImplementedError

    def find_daemon_process(self) -> Optional[Tuple[int, str]]:
        raise NotImplementedError

    def stop_daemon(self, pid: int, ident: str, timeout: float = 5.0) -> None:
        raise NotImplementedError

    def start_daemon(self) -> None:
        raise NotImplementedError


class TermuxLifecycleDriver(LifecycleDriver):
    """Native Termux lifecycle driver backed by scripts/server_supervisor.py."""

    def __init__(
        self,
        namespace_root: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        disabled_dir: Optional[Path] = None,
        proc_root: Path = Path("/proc"),
        supervisor_module: Optional[Any] = None,
        supervisor_script: Optional[Path] = None,
    ):
        self.proc_root = proc_root
        if namespace_root is not None:
            self.namespace_root = Path(namespace_root).resolve()
            self.home_dir = self.namespace_root / "home"
            self.prefix_dir = self.namespace_root / "usr"
        else:
            self.namespace_root = Path("/data/data/com.termux/files")
            self.home_dir = self.namespace_root / "home"
            self.prefix_dir = self.namespace_root / "usr"

        self.state_dir = Path(state_dir).resolve() if state_dir else self.home_dir / ".config/server_recovery"
        self.disabled_dir = Path(disabled_dir).resolve() if disabled_dir else self.home_dir / ".config/services_disabled"
        self.paused_path = self.state_dir / "paused"
        self.public_paused_path = Path("/storage/emulated/0/.phone-server-paused")

        if supervisor_script is not None:
            self.supervisor_script = Path(supervisor_script).resolve()
        else:
            repo_script = Path(__file__).resolve().parent / "server_supervisor.py"
            live_script = self.home_dir / "bin/server_supervisor.py"
            self.supervisor_script = live_script if live_script.exists() else repo_script

        self._sup = supervisor_module or self._load_supervisor()

    def _load_supervisor(self) -> Any:
        """Load supervisor module with bound state paths."""
        mod_name = "server_supervisor_native"
        if mod_name in sys.modules:
            return sys.modules[mod_name]

        spec = importlib.util.spec_from_file_location(mod_name, str(self.supervisor_script))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load supervisor from {self.supervisor_script}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    def services(self) -> Dict[str, Any]:
        return self._sup.services()

    @contextlib.contextmanager
    def lock(self, name: str, blocking: bool = True, timeout: Optional[float] = None):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self.state_dir / f"{name}.lock"
        with lock_file.open("a") as f:
            if timeout is None:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Lock {name} busy; retry shortly")
                        time.sleep(0.1)
            yield

    def matching_processes(self, sid: str) -> List[Tuple[int, str]]:
        return self._sup.matching_processes(sid)

    def process_identity(self, pid: int) -> Optional[str]:
        if self.proc_root != Path("/proc"):
            try:
                stat_path = self.proc_root / str(pid) / "stat"
                stat = stat_path.read_text().rsplit(")", 1)[1].split()
                if stat[0] == "Z":
                    return None
                return stat[19]
            except (OSError, IndexError):
                return None
        return self._sup.process_identity(pid)

    def terminate(self, records: List[Tuple[int, str]]) -> None:
        self._sup.terminate(records)

    def stop_agy(self) -> None:
        self._sup.stop_agy()

    def healthy(self, sid: str, cfg: Dict[str, Any]) -> bool:
        return self._sup.healthy(sid, cfg)

    def launch(self, sid: str, cfg: Dict[str, Any]) -> Any:
        return self._sup.launch(sid, cfg)

    def is_paused(self) -> bool:
        if self.paused_path.exists():
            return True
        try:
            if self.public_paused_path.exists():
                return True
        except OSError:
            pass
        return False

    def is_daemon_running(self) -> bool:
        try:
            with self.lock("daemon", blocking=False):
                return False
        except (BlockingIOError, OSError):
            return True

    def find_daemon_process(self) -> Optional[Tuple[int, str]]:
        uid = os.getuid()
        for proc in self.proc_root.iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid != uid:
                    continue
                cmdline_bytes = (proc / "cmdline").read_bytes()
                cmdline = cmdline_bytes.decode(errors="replace").rstrip("\0").split("\0")
                if not cmdline or not cmdline[0]:
                    continue
                base = Path(cmdline[0]).name
                if (
                    base.startswith("python")
                    and any("server_supervisor.py" in arg for arg in cmdline)
                    and "--daemon" in cmdline
                ):
                    pid = int(proc.name)
                    ident = self.process_identity(pid)
                    if ident:
                        return (pid, ident)
            except (OSError, IndexError):
                continue
        return None

    def stop_daemon(self, pid: int, ident: str, timeout: float = 5.0) -> None:
        if self.process_identity(pid) != ident:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if self.process_identity(pid) != ident:
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.process_identity(pid) != ident:
                return
            time.sleep(0.1)
        if self.process_identity(pid) == ident:
            raise QuiescenceError(f"Supervisor daemon pid={pid} failed to terminate")

    def start_daemon(self) -> None:
        with (self.state_dir / "daemon-console.log").open("ab") as out:
            subprocess.Popen(
                [sys.executable, str(self.supervisor_script), "--daemon"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
                close_fds=True,
            )


def load_isolated_lifecycle(recovery_dir: Path, namespace_root: Optional[Path] = None) -> TermuxLifecycleDriver:
    """
    Load preserved supervisor and service catalog from recovery directory in an isolated
    module context without importing potentially corrupt live helpers.
    """
    recovery_dir = Path(recovery_dir).resolve()
    catalog_path = recovery_dir / "service_catalog.py"
    supervisor_path = recovery_dir / "server_supervisor.py"

    if not supervisor_path.exists():
        raise FileNotFoundError(f"Preserved supervisor script missing: {supervisor_path}")

    # Load service catalog first if present
    if catalog_path.exists():
        cat_spec = importlib.util.spec_from_file_location("service_catalog", str(catalog_path))
        if cat_spec is None or cat_spec.loader is None:
            raise RuntimeError(f"Could not load recovery catalog from {catalog_path}")
        cat_mod = importlib.util.module_from_spec(cat_spec)
        sys.modules["service_catalog"] = cat_mod
        cat_spec.loader.exec_module(cat_mod)

    # Load supervisor
    sup_spec = importlib.util.spec_from_file_location("server_supervisor_isolated", str(supervisor_path))
    if sup_spec is None or sup_spec.loader is None:
        raise RuntimeError(f"Could not load recovery supervisor from {supervisor_path}")
    sup_mod = importlib.util.module_from_spec(sup_spec)
    sys.modules["server_supervisor_isolated"] = sup_mod
    sup_spec.loader.exec_module(sup_mod)

    return TermuxLifecycleDriver(
        namespace_root=namespace_root,
        supervisor_module=sup_mod,
        supervisor_script=supervisor_path,
    )
