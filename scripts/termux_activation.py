#!/usr/bin/env python3
"""
termux_activation.py: Process control and service lifecycle adapter for Termux.

Provides low-level lifecycle operations required by release_activation:
1. Process inspection via exact script binding and valid python argv (/proc scanning).
2. Clean process termination with SIGTERM and verified death recheck before SIGKILL.
3. Service dependency restart order execution.
4. Supervisor daemon matching and restart.
5. In-process lifecycle module reloading and isolated recovery loading.
"""

import builtins
import contextlib
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

MANDATORY_RECOVERY_ASSETS = (
    "release_activation.py",
    "termux_activation.py",
    "release_transaction.py",
    "server_supervisor.py",
    "service_catalog.py",
)


class LifecycleDriver:
    """Abstract base interface for service lifecycle authority."""

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


def _execute_supervisor(module: Any, source: bytes, filename: Path, home_dir: Path) -> None:
    """Keep the inherited diagnostic append within the selected namespace."""
    original = module.__dict__.get("open")

    def scoped_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and str(file) == "/data/data/com.termux/files/home/parent_pid.txt":
            file = home_dir / "parent_pid.txt"
        return builtins.open(file, *args, **kwargs)

    module.__dict__["open"] = scoped_open
    try:
        exec(compile(source, str(filename), "exec"), module.__dict__)
    finally:
        if original is None:
            module.__dict__.pop("open", None)
        else:
            module.__dict__["open"] = original


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
        if arg in ("-c", "-m") or arg.startswith("-c") or arg.startswith("-m"):
            return None, []
        idx += 1
    if idx < n:
        script = Path(cmdline[idx])
        return script, cmdline[idx + 1 :]
    return None, []


def validate_candidate_runtime_services(
    bundle_root: Path,
    driver: LifecycleDriver,
) -> None:
    """
    Preflight verification: assert candidate service catalog matches prior runtime facts
    (order, argv, port, path, protocol) before any quiescence, signals, or file replacement.
    """
    prior_services = driver.services()
    cand_cat_path = Path(bundle_root).resolve() / "app/service_catalog.py"
    if not cand_cat_path.exists():
        raise RuntimeError(f"Candidate service catalog missing: {cand_cat_path}")

    orig_modules = dict(sys.modules)
    orig_path = list(sys.path)
    cand_mod_name = f"cand_service_catalog_{uuid.uuid4().hex}"
    try:
        spec = importlib.util.spec_from_file_location(cand_mod_name, str(cand_cat_path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load spec for candidate catalog: {cand_cat_path}")
        cand_mod = importlib.util.module_from_spec(spec)
        sys.modules[cand_mod_name] = cand_mod
        exec(compile(cand_cat_path.read_bytes(), str(cand_cat_path), "exec"), cand_mod.__dict__)

        if not hasattr(cand_mod, "runtime_services"):
            raise RuntimeError(f"Candidate catalog {cand_cat_path} missing runtime_services function")

        home_str = str(getattr(driver, "home_dir", Path("/data/data/com.termux/files/home")))
        prefix_str = str(getattr(driver, "prefix_dir", Path("/data/data/com.termux/files/usr")))
        cand_services = cand_mod.runtime_services(home_str, prefix_str)

        if prior_services:
            prior_keys = list(prior_services.keys())
            cand_keys = list(cand_services.keys())
            if prior_keys != cand_keys:
                raise RuntimeError(
                    f"Candidate service catalog key order mismatch: expected {prior_keys}, got {cand_keys}"
                )
            for sid, p_cfg in prior_services.items():
                c_cfg = cand_services.get(sid)
                if c_cfg is None:
                    raise RuntimeError(f"Candidate catalog missing prior service: {sid}")
                for attr in ("port", "path", "protocol", "cmd", "is_ondemand", "client_port"):
                    p_val = p_cfg.get(attr)
                    c_val = c_cfg.get(attr)
                    if p_val != c_val:
                        raise RuntimeError(
                            f"Candidate service {sid} runtime drift on '{attr}': prior={p_val!r}, candidate={c_val!r}"
                        )
    finally:
        sys.path[:] = orig_path
        for k in list(sys.modules.keys()):
            if k not in orig_modules:
                del sys.modules[k]
            else:
                sys.modules[k] = orig_modules[k]


class TermuxLifecycleDriver(LifecycleDriver):
    """Production lifecycle driver invoking scripts/server_supervisor.py."""

    def __init__(
        self,
        namespace_root: Optional[Path] = None,
        supervisor_script: Optional[Path] = None,
        proc_root: Optional[Path] = None,
        supervisor_module: Optional[Any] = None,
    ):
        self.namespace_root = Path(namespace_root).resolve() if namespace_root else Path("/data/data/com.termux/files")
        self.proc_root = Path(proc_root).resolve() if proc_root else Path("/proc")
        self.home_dir = self.namespace_root / "home"
        self.prefix_dir = self.namespace_root / "usr"
        self.state_dir = self.home_dir / ".config/server_recovery"
        self.disabled_dir = self.home_dir / ".config/services_disabled"
        self.paused_path = self.state_dir / "paused"
        self.public_paused_path = Path("/storage/emulated/0/.phone-server-paused")

        if supervisor_script is not None:
            self.supervisor_script = Path(supervisor_script).resolve()
        else:
            repo_script = Path(__file__).resolve().parent / "server_supervisor.py"
            live_script = self.home_dir / "bin/server_supervisor.py"
            self.supervisor_script = live_script if live_script.exists() else repo_script

        self._sup = supervisor_module or self._load_supervisor()

    def validate_candidate_services(self, bundle_root: Path) -> None:
        validate_candidate_runtime_services(bundle_root=bundle_root, driver=self)

    def _load_supervisor(self) -> Any:
        """Load supervisor module in an isolated context with bound state paths."""
        orig_path = list(sys.path)
        orig_modules = dict(sys.modules)
        mod_name = f"server_supervisor_isolated_{id(self)}"

        try:
            live_cat = self.home_dir / ".local/lib/mobile-server/service_catalog.py"
            repo_cat = Path(__file__).resolve().parent.parent / "app/service_catalog.py"
            scripts_cat = Path(__file__).resolve().parent / "service_catalog.py"
            cat_path = live_cat if live_cat.exists() else (repo_cat if repo_cat.exists() else (scripts_cat if scripts_cat.exists() else None))

            cat_mod = None
            if cat_path and cat_path.exists():
                cat_spec = importlib.util.spec_from_file_location("service_catalog", str(cat_path))
                if cat_spec and cat_spec.loader:
                    cat_mod = importlib.util.module_from_spec(cat_spec)
                    sys.modules["service_catalog"] = cat_mod
                    exec(compile(cat_path.read_bytes(), str(cat_path), "exec"), cat_mod.__dict__)

            spec = importlib.util.spec_from_file_location(mod_name, str(self.supervisor_script))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not load supervisor from {self.supervisor_script}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod

            _execute_supervisor(mod, self.supervisor_script.read_bytes(), self.supervisor_script, self.home_dir)

            for m in (mod, cat_mod):
                if m is None:
                    continue
                m.HOME = self.home_dir
                m.PREFIX = self.prefix_dir
                m.STATE = self.state_dir
                m.DISABLED = self.disabled_dir
                m.PAUSED = self.paused_path
                m.PUBLIC_PAUSED = self.public_paused_path
                m.MOBILE_LIB = self.home_dir / ".local/lib/mobile-server"
            return mod
        finally:
            sys.path[:] = orig_path
            for k in list(sys.modules.keys()):
                if k not in orig_modules and k != mod_name:
                    del sys.modules[k]
                elif k in orig_modules:
                    sys.modules[k] = orig_modules[k]

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
                cmdline = cmdline_bytes.decode(errors="replace").rstrip(chr(0)).split(chr(0))
                script_path, remaining = extract_python_script_and_args(cmdline)
                if script_path is None:
                    continue

                if not script_path.is_absolute():
                    try:
                        target_cwd = Path(os.readlink(str(proc / "cwd")))
                        resolved_script = (target_cwd / script_path).resolve()
                    except (OSError, ValueError):
                        continue
                else:
                    resolved_script = script_path.resolve()

                if resolved_script not in allowed_scripts:
                    continue

                if "--daemon" not in remaining:
                    continue

                # Ensure --daemon is a standalone option flag, not value to an option
                is_standalone_daemon = False
                for token in remaining:
                    if token == "--daemon":
                        is_standalone_daemon = True
                        break
                    elif token.startswith("--daemon=") or "--daemon" in token:
                        is_standalone_daemon = False
                        break
                if not is_standalone_daemon:
                    continue

                ident = self.process_identity(int(proc.name))
                if ident is None:
                    continue
                matches.append((int(proc.name), ident))
            except (OSError, IndexError, ValueError):
                continue

        if len(matches) > 1:
            raise RuntimeError(f"Multiple supervisor daemon processes detected: {matches}; refusing ambiguous match")
        return matches[0] if matches else None

    def stop_daemon(self, pid: int, ident: str, timeout: float = 5.0) -> None:
        """Stop supervisor daemon with verified termination before returning."""
        cur_ident = self.process_identity(pid)
        if cur_ident != ident:
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur_ident = self.process_identity(pid)
            if cur_ident != ident:
                return
            time.sleep(0.1)

        cur_ident = self.process_identity(pid)
        if cur_ident == ident:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            time.sleep(0.1)

    def start_daemon(self) -> None:
        """Start daemon using the restored LIVE home/bin/server_supervisor.py script."""
        live_script = (self.home_dir / "bin/server_supervisor.py").resolve()
        if not live_script.exists():
            raise RuntimeError(f"Restored live supervisor script not found at {live_script}")
        if "recovery_lifecycle" in live_script.parts:
            raise RuntimeError(f"Refusing to start daemon from recovery path: {live_script}")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "daemon-console.log").open("ab") as out:
            subprocess.Popen(
                [sys.executable, str(live_script), "--daemon"],
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

    if not expected_hashes or not isinstance(expected_hashes, dict):
        raise ValueError("load_isolated_lifecycle requires non-empty expected_hashes dictionary")

    for req in ("server_supervisor.py", "service_catalog.py"):
        if req not in expected_hashes or not expected_hashes[req]:
            raise ValueError(f"Mandatory recovery asset {req} missing from expected_hashes")

    catalog_path = recovery_dir / "service_catalog.py"
    supervisor_path = recovery_dir / "server_supervisor.py"

    verified_bytes: Dict[str, bytes] = {}
    for name, p in (("service_catalog.py", catalog_path), ("server_supervisor.py", supervisor_path)):
        if not p.exists():
            raise FileNotFoundError(f"Preserved recovery asset missing: {p}")
        try:
            p.resolve().relative_to(recovery_dir)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Path traversal escape in recovery asset {name}: {exc}") from exc
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"Preserved recovery asset {name} is not a regular file")
        if st.st_uid != current_uid:
            raise RuntimeError(f"Preserved recovery asset {name} UID mismatch: expected {current_uid}, got {st.st_uid}")
        raw = p.read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha.lower() != expected_hashes[name].lower():
            raise RuntimeError(
                f"Preserved recovery asset {name} hash mismatch: expected {expected_hashes[name]}, got {actual_sha}"
            )
        verified_bytes[name] = raw

    orig_path = list(sys.path)
    orig_modules = dict(sys.modules)
    token = uuid.uuid4().hex
    sup_mod_name = f"server_supervisor_iso_{token}"

    ns_root = Path(namespace_root).resolve() if namespace_root else Path("/data/data/com.termux/files")
    home_dir = ns_root / "home"
    prefix_dir = ns_root / "usr"
    state_dir = home_dir / ".config/server_recovery"
    disabled_dir = home_dir / ".config/services_disabled"
    paused_path = state_dir / "paused"
    public_paused_path = Path("/storage/emulated/0/.phone-server-paused")
    mobile_lib = home_dir / ".local/lib/mobile-server"

    try:
        sys.path.insert(0, str(recovery_dir))

        cat_spec = importlib.util.spec_from_file_location("service_catalog", str(catalog_path))
        cat_mod = importlib.util.module_from_spec(cat_spec)
        cat_mod.__file__ = str(catalog_path)
        sys.modules["service_catalog"] = cat_mod
        cat_code = compile(verified_bytes["service_catalog.py"], str(catalog_path), "exec")
        exec(cat_code, cat_mod.__dict__)

        sup_spec = importlib.util.spec_from_file_location(sup_mod_name, str(supervisor_path))
        if sup_spec is None:
            raise RuntimeError(f"Could not load supervisor spec from {supervisor_path}")
        sup_mod = importlib.util.module_from_spec(sup_spec)
        sup_mod.__file__ = str(supervisor_path)
        sys.modules[sup_mod_name] = sup_mod
        _execute_supervisor(sup_mod, verified_bytes["server_supervisor.py"], supervisor_path, home_dir)

        for m in (sup_mod, cat_mod):
            if m is None:
                continue
            m.HOME = home_dir
            m.PREFIX = prefix_dir
            m.STATE = state_dir
            m.DISABLED = disabled_dir
            m.PAUSED = paused_path
            m.PUBLIC_PAUSED = public_paused_path
            m.MOBILE_LIB = mobile_lib

        driver = TermuxLifecycleDriver(
            namespace_root=ns_root,
            supervisor_script=supervisor_path,
            supervisor_module=sup_mod,
        )
        return driver
    finally:
        sys.path[:] = orig_path
        for k in list(sys.modules.keys()):
            if k not in orig_modules:
                del sys.modules[k]
            else:
                sys.modules[k] = orig_modules[k]
