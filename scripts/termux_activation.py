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
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple


class QuiescenceError(RuntimeError):
    """Raised when a service or daemon process survives termination or is ambiguous."""
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

    def reload_lifecycle(self) -> None:
        raise NotImplementedError


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def extract_python_script_and_args(cmdline: List[str]) -> Tuple[Optional[Path], List[str]]:
    """Parse python command line tokens to extract script path and trailing arguments."""
    if not cmdline or not cmdline[0]:
        return None, []
    base = Path(cmdline[0]).name
    if not (base.startswith("python") or base.startswith("python3")):
        return None, []
    idx = 1
    n = len(cmdline)
    while idx < n:
        arg = cmdline[idx]
        if arg == "--":
            idx += 1
            break
        if not arg.startswith("-"):
            break
        # Option -c or -m executes command or module, not a script file
        if arg in ("-c", "-m") or arg.startswith("-c") or arg.startswith("-m"):
            return None, []
        if arg in ("-W", "-X"):
            idx += 2
        else:
            idx += 1
    if idx < n:
        script_path = Path(cmdline[idx])
        remaining_args = cmdline[idx + 1 :]
        return script_path, remaining_args
    return None, []


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
        """Load supervisor module in an isolated context with bound state paths."""
        orig_path = list(sys.path)
        orig_modules = dict(sys.modules)
        try:
            mod_name = f"server_supervisor_isolated_{id(self)}"
            spec = importlib.util.spec_from_file_location(mod_name, str(self.supervisor_script))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not load supervisor from {self.supervisor_script}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod

            # Bind namespace paths
            mod.HOME = self.home_dir
            mod.PREFIX = self.prefix_dir
            mod.STATE = self.state_dir
            mod.DISABLED = self.disabled_dir
            mod.PAUSED = self.paused_path
            mod.PUBLIC_PAUSED = self.public_paused_path
            mod.MOBILE_LIB = self.home_dir / ".local/lib/mobile-server"

            spec.loader.exec_module(mod)

            # Re-bind paths in case module top-level initialized defaults
            mod.HOME = self.home_dir
            mod.PREFIX = self.prefix_dir
            mod.STATE = self.state_dir
            mod.DISABLED = self.disabled_dir
            mod.PAUSED = self.paused_path
            mod.PUBLIC_PAUSED = self.public_paused_path
            mod.MOBILE_LIB = self.home_dir / ".local/lib/mobile-server"
            return mod
        finally:
            sys.path[:] = orig_path
            for k in list(sys.modules.keys()):
                if k not in orig_modules and k != mod_name:
                    del sys.modules[k]

    def reload_lifecycle(self) -> None:
        """Reload supervisor module to bind newly applied code and configuration."""
        self._sup = self._load_supervisor()

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
        """
        Locate supervisor daemon process by exact script binding and valid python argv.
        Refuses ambiguous matches and non-matching decoy scripts.
        """
        uid = os.getuid()
        allowed_scripts = {
            self.supervisor_script.resolve(),
            (self.home_dir / "bin/server_supervisor.py").resolve(),
        }
        matches: List[Tuple[int, str]] = []

        for proc in self.proc_root.iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid != uid:
                    continue
                cmdline_bytes = (proc / "cmdline").read_bytes()
                cmdline = cmdline_bytes.decode(errors="replace").rstrip("\0").split("\0")
                script_path, remaining = extract_python_script_and_args(cmdline)
                if script_path is None:
                    continue

                # Must resolve to exact approved supervisor script
                try:
                    resolved_script = script_path.resolve()
                except OSError:
                    continue

                if resolved_script in allowed_scripts and "--daemon" in remaining:
                    pid = int(proc.name)
                    ident = self.process_identity(pid)
                    if ident:
                        matches.append((pid, ident))
            except (OSError, IndexError):
                continue

        if len(matches) > 1:
            raise QuiescenceError(
                f"Multiple matching supervisor daemon processes found: {matches} (ambiguity refusal)"
            )
        if len(matches) == 1:
            return matches[0]
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

        # Re-verify identity immediately before SIGKILL
        if self.process_identity(pid) == ident:
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
        """Start daemon using the restored LIVE home/bin/server_supervisor.py script."""
        live_script = self.home_dir / "bin/server_supervisor.py"
        script_to_run = live_script if live_script.exists() else self.supervisor_script
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "daemon-console.log").open("ab") as out:
            subprocess.Popen(
                [sys.executable, str(script_to_run), "--daemon"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
                close_fds=True,
            )


def load_isolated_lifecycle(
    recovery_dir: Path,
    namespace_root: Optional[Path] = None,
    expected_hashes: Optional[Dict[str, str]] = None,
) -> TermuxLifecycleDriver:
    """
    Load preserved supervisor and service catalog from recovery directory in an isolated
    module context without importing potentially corrupt live helpers.
    Validates fixed-name, same-UID, regular recovery files against recorded hashes before execution.
    """
    recovery_dir = Path(recovery_dir).resolve()
    current_uid = os.getuid()

    catalog_path = recovery_dir / "service_catalog.py"
    supervisor_path = recovery_dir / "server_supervisor.py"

    # Pre-execution validation
    for name, p in (("service_catalog.py", catalog_path), ("server_supervisor.py", supervisor_path)):
        if name == "service_catalog.py" and not p.exists():
            continue
        if not p.exists():
            raise FileNotFoundError(f"Preserved recovery asset missing: {p}")
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"Recovery asset {p} is not a regular file")
        if st.st_uid != current_uid:
            raise RuntimeError(f"Recovery asset {p} UID mismatch: expected {current_uid}, got {st.st_uid}")
        if expected_hashes and name in expected_hashes:
            actual_sha = _compute_sha256(p)
            if actual_sha.lower() != expected_hashes[name].lower():
                raise RuntimeError(
                    f"Recovery asset {name} hash mismatch: expected {expected_hashes[name]}, got {actual_sha}"
                )

    orig_path = list(sys.path)
    orig_modules = dict(sys.modules)
    try:
        if catalog_path.exists():
            cat_spec = importlib.util.spec_from_file_location("service_catalog", str(catalog_path))
            if cat_spec is None or cat_spec.loader is None:
                raise RuntimeError(f"Could not load recovery catalog from {catalog_path}")
            cat_mod = importlib.util.module_from_spec(cat_spec)
            sys.modules["service_catalog"] = cat_mod
            cat_spec.loader.exec_module(cat_mod)

        sup_spec = importlib.util.spec_from_file_location("server_supervisor_isolated", str(supervisor_path))
        if sup_spec is None or sup_spec.loader is None:
            raise RuntimeError(f"Could not load recovery supervisor from {supervisor_path}")
        sup_mod = importlib.util.module_from_spec(sup_spec)
        sys.modules["server_supervisor_isolated"] = sup_mod
        sup_spec.loader.exec_module(sup_mod)

        # Set live supervisor script path so start_daemon restarts live code
        ns = Path(namespace_root).resolve() if namespace_root else Path("/data/data/com.termux/files")
        live_supervisor = ns / "home/bin/server_supervisor.py"

        return TermuxLifecycleDriver(
            namespace_root=ns,
            supervisor_module=sup_mod,
            supervisor_script=live_supervisor if live_supervisor.exists() else supervisor_path,
        )
    finally:
        sys.path[:] = orig_path
        for k in list(sys.modules.keys()):
            if k not in orig_modules and k not in ("service_catalog", "server_supervisor_isolated"):
                del sys.modules[k]
