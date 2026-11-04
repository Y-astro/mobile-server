#!/usr/bin/env python3
"""
release_activation.py: Service-scoped release activation adapter.

Coordinates the accepted file transaction engine (scripts/release_transaction.py)
with Termux service lifecycle authority (scripts/server_supervisor.py), ensuring:
1. Destination-based service mapping (only actually changed files affect services).
2. Safe flag preservation and temporary guard files with ownership tokens.
3. Process quiescence with verified death rechecks before any file write.
4. Coherent atomic file replacement under lock hierarchy:
   ActivationLock -> services.lock -> NamespaceLock.
5. Dependency-ordered reactivation (FileBrowser before gateway; dashboard/gateway before Caddy).
6. Fail-closed rollback and standalone recovery without candidate bundle or live imports.
"""

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

# Sibling module dynamic loaders
def _load_sibling(module_name: str, filename: str) -> Any:
    current_dir = Path(__file__).resolve().parent
    candidates = [
        current_dir / filename,
        current_dir.parent / filename,
        current_dir.parent / "scripts" / filename,
    ]
    file_path = None
    for cand in candidates:
        if cand.exists():
            file_path = cand
            break

    if file_path is None:
        if module_name in sys.modules:
            return sys.modules[module_name]
        raise RuntimeError(f"Required module {filename} not found in {candidates}")

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

rt = _load_sibling("release_transaction", "release_transaction.py")
ta = _load_sibling("termux_activation", "termux_activation.py")

# Constants
JOURNAL_VERSION = 1
STATE_PREPARED = "PREPARED"
STATE_GUARDED = "GUARDED"
STATE_QUIESCED = "QUIESCED"
STATE_APPLYING = "APPLYING"
STATE_APPLIED = "APPLIED"
STATE_HEALTHY = "HEALTHY"
STATE_COMPLETED = "COMPLETED"
STATE_ROLLING_BACK = "ROLLING_BACK"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

DEPENDENCY_ORDER = [
    "filebrowser",
    "transfer_gateway",
    "dashboard",
    "caddy",
]


class ActivationError(Exception):
    """Base exception for release activation errors."""
    pass


class ActivationLockError(ActivationError):
    """Raised when the namespace activation lock cannot be acquired."""
    pass


class QuiescenceError(ActivationError):
    """Raised when processes survive quiescence."""
    pass


class ConcurrencyError(ActivationError):
    """Raised when a concurrent modification is detected on flags or files."""
    pass


class HealthCheckError(ActivationError):
    """Raised when candidate service health check fails."""
    pass


class ActivationLock:
    """Exclusive lock per namespace root serializing activation operations."""

    def __init__(self, namespace_root: Path):
        self.namespace_root = Path(namespace_root).resolve()
        self.lock_path = self.namespace_root / ".release_activation.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ActivationLockError(f"Could not open activation lock file {self.lock_path}: {exc}") from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise ActivationLockError(
                f"Activation lock held by another process: {self.lock_path}"
            ) from exc

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def map_destination_to_component(remote_path: str) -> Optional[Tuple[str, bool]]:
    """
    Map an owned destination path to (component/service_name, is_suwayomi_violation).
    Returns None if unexplained destination.
    """
    norm = remote_path.replace("\\", "/").rstrip("/")

    # Check for Suwayomi scripts - must NOT change
    for sw in ("start_suwayomi.sh", "suwayomi_daily_update.py", "suwayomi_graphql_filter.py", "suwayomi_update.sh"):
        if norm.endswith(f"home/bin/{sw}"):
            return ("suwayomi", True)

    # Dashboard group:
    if norm.endswith("usr/bin/dashboard"):
        return ("dashboard", False)
    if "/home/.config/dashboard/static/" in norm:
        return ("dashboard", False)
    if norm.endswith("home/.local/lib/mobile-server/service_catalog.py"):
        return ("service_catalog", False)
    if "/home/.local/lib/mobile-server/" in norm:
        return ("dashboard", False)

    # Gateway:
    if norm.endswith("home/bin/mobile-gateway"):
        return ("transfer_gateway", False)

    # Caddy:
    if norm.endswith("home/.config/caddy/Caddyfile"):
        return ("caddy", False)

    # Supervisor:
    if norm.endswith("home/bin/server_supervisor.py"):
        return ("supervisor", False)

    # Terminal:
    if norm.endswith("home/bin/termux_session.sh") or norm.endswith("home/.config/ttyd/termux_index.html"):
        return ("terminal", False)

    # Laptop terminal:
    if norm.endswith("home/bin/laptop_session.py"):
        return ("laptop_terminal", False)

    # AGY:
    if (
        norm.endswith("home/bin/agy_session.sh")
        or norm.endswith("home/.config/ttyd/agy_index.html")
        or norm.endswith("usr/bin/agy-resume")
        or "agy-runner-loop" in norm
    ):
        return ("agy", False)

    # Battery guard:
    if norm.endswith("home/bin/battery_guard.sh"):
        return ("battery_guard", False)

    # Tor:
    if norm.endswith("home/.config/tor/torrc"):
        return ("tor", False)

    # CLI tools and boot aliases (no running service to restart):
    cli_tools = (
        "usr/bin/server-user",
        "usr/bin/server-status",
        "usr/bin/server-health",
        "usr/bin/server-up",
        "usr/bin/server-down",
        "usr/bin/server-restart",
        "home/start_services.sh",
        "home/.termux/boot/start-services.sh",
    )
    for tool in cli_tools:
        if norm.endswith(tool):
            return ("cli_only", False)

    return None


def map_changed_destinations(entries: List[Dict[str, Any]]) -> Tuple[Set[str], bool, List[str]]:
    """
    Given prepared transaction entries, return:
    (affected_services, supervisor_affected, changed_destinations)
    """
    affected_services: Set[str] = set()
    supervisor_affected = False
    changed_destinations: List[str] = []

    for entry in entries:
        action = entry.get("action")
        if action == rt.ACTION_UNTOUCHED:
            continue

        remote = entry.get("remote", "")
        changed_destinations.append(remote)
        mapping = map_destination_to_component(remote)

        if mapping is None:
            raise ActivationError(f"Unexplained changed destination: {remote}")

        comp, is_sw = mapping
        if is_sw:
            raise ActivationError("Suwayomi launcher/filter/scheduler sources must be unchanged in this release")

        if comp == "cli_only":
            continue
        elif comp == "service_catalog":
            affected_services.add("dashboard")
            supervisor_affected = True
        elif comp == "supervisor":
            supervisor_affected = True
        else:
            affected_services.add(comp)

    if "ssh" in affected_services:
        raise ActivationError("SSH service restart is strictly prohibited")

    return affected_services, supervisor_affected, changed_destinations


def save_activation_journal(tx_dir: Path, data: Dict[str, Any]) -> None:
    data["updated_at"] = time.time()
    payload = json.dumps(data, indent=2).encode("utf-8")
    journal_path = tx_dir / "activation_journal.json"
    tmp_path = tx_dir / ".tmp_act_journal.json"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_path), str(journal_path))

    try:
        dfd = os.open(str(tx_dir), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def load_activation_journal(tx_dir: Path) -> Dict[str, Any]:
    journal_path = tx_dir / "activation_journal.json"
    if not journal_path.exists():
        raise FileNotFoundError(f"Activation journal not found: {journal_path}")
    with open(journal_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != JOURNAL_VERSION:
        raise ActivationError(f"Unsupported activation journal version: {data.get('version')}")
    return data


def install_guards(
    disabled_dir: Path,
    affected_services: List[str],
    tx_id: str,
    uid: int,
    journal: Dict[str, Any],
    tx_dir: Path,
) -> Tuple[List[str], List[str]]:
    """
    Inspect affected services flags under services.lock.
    Preserve pre-existing flags unchanged.
    Install temporary guards for originally absent flags with ownership token.
    Returns: (active_affected_services, disabled_affected_services)
    """
    disabled_dir.mkdir(parents=True, exist_ok=True)
    flags = journal.setdefault("flags", {})
    active_services: List[str] = []
    disabled_services: List[str] = []

    for sid in sorted(affected_services):
        flag_path = disabled_dir / sid
        if flag_path.exists() or flag_path.is_symlink():
            # Pre-existing! Never touch bytes or metadata.
            st = flag_path.lstat()
            sha = ""
            if flag_path.is_file() and not flag_path.is_symlink():
                try:
                    with open(flag_path, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    pass
            flags[sid] = {
                "pre_existing": True,
                "ino": st.st_ino,
                "dev": st.st_dev,
                "mode": oct(st.st_mode),
                "uid": st.st_uid,
                "size": st.st_size,
                "sha256": sha,
            }
            disabled_services.append(sid)
        else:
            token = f"release_guard:{tx_id}:{sid}"
            flags[sid] = {
                "pre_existing": False,
                "intent": "create_guard",
                "token": token,
                "guard_created": False,
            }
            save_activation_journal(tx_dir, journal)

            payload = f"RELEASE_GUARD_TOKEN={token}\nTX_ID={tx_id}\nSID={sid}\n"
            fd = os.open(str(flag_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)

            st = flag_path.stat()
            flags[sid]["guard_created"] = True
            flags[sid]["ino"] = st.st_ino
            flags[sid]["dev"] = st.st_dev
            flags[sid]["mode"] = oct(st.st_mode)
            flags[sid]["uid"] = st.st_uid
            active_services.append(sid)

    save_activation_journal(tx_dir, journal)
    return active_services, disabled_services


def remove_guards(
    disabled_dir: Path,
    flags: Dict[str, Any],
    expected_uid: int,
) -> None:
    """
    Remove temporary guards after verifying token, regular file, UID, and recorded identity.
    Refuse concurrent edits fail-closed.
    """
    for sid, info in flags.items():
        if info.get("pre_existing", False):
            continue
        token = info.get("token")
        flag_path = disabled_dir / sid
        if not flag_path.exists():
            continue

        st = flag_path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ConcurrencyError(f"Guard flag {flag_path} was modified concurrently (not a regular file)")
        if st.st_uid != expected_uid:
            raise ConcurrencyError(f"Guard flag {flag_path} UID mismatch: expected {expected_uid}, found {st.st_uid}")
        if info.get("ino") is not None and st.st_ino != info["ino"]:
            raise ConcurrencyError(f"Guard flag {flag_path} inode mismatch: expected {info['ino']}, found {st.st_ino}")

        try:
            content = flag_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConcurrencyError(f"Could not read guard flag {flag_path}: {exc}") from exc

        if not token or token not in content:
            raise ConcurrencyError(f"Guard flag {flag_path} token mismatch: unknown concurrent edit")

        flag_path.unlink()


def sort_services_by_dependencies(services: List[str]) -> List[str]:
    order_map = {sid: idx for idx, sid in enumerate(DEPENDENCY_ORDER)}
    return sorted(services, key=lambda s: (order_map.get(s, 999), s))


def verify_prerequisites_before_launch(
    sid: str,
    driver: ta.LifecycleDriver,
    active_services: Set[str],
) -> None:
    all_cfgs = driver.services()
    if sid == "transfer_gateway":
        fb_cfg = all_cfgs.get("filebrowser")
        if fb_cfg and "filebrowser" in active_services:
            if not driver.healthy("filebrowser", fb_cfg):
                raise HealthCheckError("FileBrowser must already be healthy before launching transfer_gateway")
    elif sid == "caddy":
        dash_cfg = all_cfgs.get("dashboard")
        if dash_cfg and "dashboard" in active_services:
            if not driver.healthy("dashboard", dash_cfg):
                raise HealthCheckError("dashboard must already be healthy before launching caddy")
        gw_cfg = all_cfgs.get("transfer_gateway")
        if gw_cfg and "transfer_gateway" in active_services:
            if not driver.healthy("transfer_gateway", gw_cfg):
                raise HealthCheckError("transfer_gateway must already be healthy before launching caddy")


def preserve_recovery_lifecycle_code(
    tx_dir: Path,
    namespace_root: Path,
) -> Dict[str, str]:
    """
    Preserve standalone copies of server_supervisor.py, service_catalog.py,
    release_activation.py, and termux_activation.py in tx_dir/recovery_lifecycle.
    Returns mapping of {filename: sha256}.
    """
    rec_dir = tx_dir / "recovery_lifecycle"
    rec_dir.mkdir(parents=True, exist_ok=True)
    hashes: Dict[str, str] = {}

    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent

    # 1. Supervisor script (from live if exists, else repo)
    live_sup = namespace_root / "home/bin/server_supervisor.py"
    src_sup = live_sup if live_sup.exists() else scripts_dir / "server_supervisor.py"
    dst_sup = rec_dir / "server_supervisor.py"
    shutil.copy2(src_sup, dst_sup)
    hashes["server_supervisor.py"] = compute_file_sha256(dst_sup)

    # 2. Service catalog (from live if exists, else repo)
    live_cat = namespace_root / "home/.local/lib/mobile-server/service_catalog.py"
    src_cat = live_cat if live_cat.exists() else repo_root / "app/service_catalog.py"
    if src_cat.exists():
        dst_cat = rec_dir / "service_catalog.py"
        shutil.copy2(src_cat, dst_cat)
        hashes["service_catalog.py"] = compute_file_sha256(dst_cat)

    # 3. Activation adapter and driver
    dst_act = rec_dir / "release_activation.py"
    shutil.copy2(scripts_dir / "release_activation.py", dst_act)
    hashes["release_activation.py"] = compute_file_sha256(dst_act)

    dst_drv = rec_dir / "termux_activation.py"
    shutil.copy2(scripts_dir / "termux_activation.py", dst_drv)
    hashes["termux_activation.py"] = compute_file_sha256(dst_drv)

    # 4. Standalone file transaction engine
    dst_tx = rec_dir / "release_transaction.py"
    shutil.copy2(scripts_dir / "release_transaction.py", dst_tx)
    hashes["release_transaction.py"] = compute_file_sha256(dst_tx)

    return hashes


def plan(
    bundle_root: Path,
    preconditions: Dict[str, Any],
    namespace_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Dry planning: determine changed destinations and affected services without disk writes."""
    b_root = Path(bundle_root).resolve()
    manifest_mod = rt._load_release_manifest_module()
    manifest_entries = manifest_mod.generate_manifest(b_root, require_gateway=True)

    precond_files = {p["path"]: p for p in preconditions.get("files", [])}
    changed_destinations: List[Dict[str, Any]] = []

    for entry in manifest_entries:
        remote = entry["remote"]
        precond = precond_files.get(remote)
        if not precond:
            raise ActivationError(f"Missing precondition for {remote}")

        kind = precond.get("kind")
        action = rt.ACTION_UNTOUCHED

        if kind == "absent":
            action = rt.ACTION_CREATE
        elif kind == "symlink":
            orig_sha = precond.get("target_sha256", rt.AUDITED_BOOT_SCRIPT_SHA256)
            orig_bytes = precond.get("target_bytes", rt.AUDITED_BOOT_SCRIPT_BYTES)
            orig_mode = rt.normalize_mode(precond.get("target_mode", rt.AUDITED_BOOT_SCRIPT_MODE))
            cand_sha = entry["sha256"]
            cand_bytes = entry["bytes"]
            target_mode = rt.get_canonical_target_mode(remote)
            if not (cand_sha.lower() == orig_sha.lower() and cand_bytes == orig_bytes and target_mode == orig_mode):
                action = rt.ACTION_REPLACE
        elif kind == "file":
            orig_sha = precond["sha256"]
            orig_bytes = precond["bytes"]
            orig_mode = rt.normalize_mode(precond["mode"])
            cand_sha = entry["sha256"]
            cand_bytes = entry["bytes"]
            target_mode = rt.get_canonical_target_mode(remote)
            if not (cand_sha.lower() == orig_sha.lower() and cand_bytes == orig_bytes and target_mode == orig_mode):
                action = rt.ACTION_REPLACE

        if action != rt.ACTION_UNTOUCHED:
            changed_destinations.append({
                "local": entry["local"],
                "remote": remote,
                "action": action,
            })

    affected_services, supervisor_affected, _ = map_changed_destinations(changed_destinations)

    return {
        "status": "plan_ok",
        "changed_destinations": changed_destinations,
        "affected_services": sorted(list(affected_services)),
        "supervisor_affected": supervisor_affected,
        "unchanged_count": len(manifest_entries) - len(changed_destinations),
        "total_count": len(manifest_entries),
    }


def apply(
    bundle_root: Path,
    preconditions: Dict[str, Any],
    transaction_dir: Path,
    namespace_root: Optional[Path] = None,
    driver: Optional[ta.LifecycleDriver] = None,
    health_timeout: float = 10.0,
) -> Dict[str, Any]:
    """Execute service-scoped release activation."""
    b_root = Path(bundle_root).resolve()
    tx_dir = Path(transaction_dir).resolve()
    ns_root = Path(namespace_root).resolve() if namespace_root else Path("/data/data/com.termux/files")

    current_uid = os.getuid()
    if preconditions.get("uid") != current_uid:
        raise ActivationError(f"Runtime UID {current_uid} does not match preconditions UID {preconditions.get('uid')}")

    if driver is None:
        driver = ta.TermuxLifecycleDriver(namespace_root=ns_root)

    # 1. Acquire ActivationLock
    with ActivationLock(ns_root):
        # Refuse deliberately paused server
        if driver.is_paused():
            raise ActivationError("Server is deliberately paused; activation refused")

        manifest_mod = rt._load_release_manifest_module()
        manifest_entries = manifest_mod.generate_manifest(b_root, require_gateway=True)

        # 2. Prepare file transaction
        tx = rt.prepare(
            bundle_root=b_root,
            candidate_manifest=manifest_entries,
            preconditions=preconditions,
            transaction_dir=tx_dir,
            namespace_root=ns_root,
        )

        tx_journal = tx.load_journal()
        entries = tx_journal.get("entries", [])
        affected_services_set, supervisor_affected, _ = map_changed_destinations(entries)
        affected_services = sorted(list(affected_services_set))

        tx_id = f"act_{uuid.uuid4().hex[:12]}"
        recovery_hashes = preserve_recovery_lifecycle_code(tx_dir, ns_root)

        act_journal: Dict[str, Any] = {
            "version": JOURNAL_VERSION,
            "state": STATE_PREPARED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": tx_id,
            "namespace_root": str(ns_root),
            "transaction_dir": str(tx_dir),
            "top_uid": current_uid,
            "affected_services": affected_services,
            "supervisor_affected": supervisor_affected,
            "daemon_was_running": False,
            "flags": {},
            "quiescence_records": {},
            "daemon_record": None,
            "recovery_assets": recovery_hashes,
        }
        save_activation_journal(tx_dir, act_journal)

        disabled_dir = ns_root / "home/.config/services_disabled"

        # If no services affected (e.g. CLI only or no-op), apply files directly without service disturbance
        if not affected_services and not supervisor_affected:
            act_journal["state"] = STATE_APPLYING
            save_activation_journal(tx_dir, act_journal)
            tx.apply(namespace_root=ns_root)
            act_journal["state"] = STATE_APPLIED
            save_activation_journal(tx_dir, act_journal)
            tx.commit(namespace_root=ns_root)
            act_journal["state"] = STATE_COMPLETED
            save_activation_journal(tx_dir, act_journal)
            return {"state": STATE_COMPLETED, "affected_services": [], "status": "completed_noop_services"}

        # 3. Guarding & Quiescence under services.lock
        active_services: List[str] = []
        daemon_proc = None
        daemon_was_running = False

        try:
            with driver.lock("services", timeout=10.0):
                active_services, _ = install_guards(
                    disabled_dir=disabled_dir,
                    affected_services=affected_services,
                    tx_id=tx_id,
                    uid=current_uid,
                    journal=act_journal,
                    tx_dir=tx_dir,
                )
                act_journal["state"] = STATE_GUARDED
                save_activation_journal(tx_dir, act_journal)

                # Quiesce active services
                quiescence_records: Dict[str, List[Tuple[int, str]]] = {}
                for sid in active_services:
                    records = driver.matching_processes(sid)
                    quiescence_records[sid] = records
                    if records:
                        driver.terminate(records)
                        if sid == "agy":
                            driver.stop_agy()

                act_journal["quiescence_records"] = quiescence_records
                save_activation_journal(tx_dir, act_journal)

                # Explicit recheck of service termination
                for sid in active_services:
                    records = quiescence_records.get(sid, [])
                    surviving = [r for r in records if driver.process_identity(r[0]) == r[1]]
                    remaining = driver.matching_processes(sid)
                    if surviving or remaining:
                        raise QuiescenceError(
                            f"Service {sid} processes survived termination: surviving={surviving}, remaining={remaining}"
                        )

                act_journal["state"] = STATE_QUIESCED
                save_activation_journal(tx_dir, act_journal)

                if supervisor_affected:
                    daemon_was_running = driver.is_daemon_running()
                    act_journal["daemon_was_running"] = daemon_was_running
                    save_activation_journal(tx_dir, act_journal)
                    if daemon_was_running:
                        daemon_proc = driver.find_daemon_process()
                        act_journal["daemon_record"] = daemon_proc
                        save_activation_journal(tx_dir, act_journal)

            # If supervisor affected and daemon was running, stop daemon outside services.lock
            if supervisor_affected and daemon_was_running and daemon_proc:
                driver.stop_daemon(daemon_proc[0], daemon_proc[1])

            # Re-acquire services.lock and re-verify quiescence & guards
            with driver.lock("services", timeout=10.0):
                # Re-verify paused state
                if driver.is_paused():
                    raise ActivationError("Server became paused during activation; refusing file mutation")

                # Re-verify guards
                for sid, info in act_journal["flags"].items():
                    if info.get("pre_existing", False):
                        continue
                    gp = disabled_dir / sid
                    if not gp.exists():
                        raise ConcurrencyError(f"Guard flag {gp} vanished before file mutation")
                    st = gp.lstat()
                    if st.st_ino != info.get("ino"):
                        raise ConcurrencyError(f"Guard flag {gp} replaced concurrently")

                # Re-verify active services dead
                for sid in active_services:
                    if driver.matching_processes(sid):
                        raise QuiescenceError(f"Service {sid} processes reappeared before file mutation")

                if supervisor_affected and daemon_proc:
                    if driver.process_identity(daemon_proc[0]) == daemon_proc[1]:
                        raise QuiescenceError("Supervisor daemon survived termination")

                # 4. Apply file transaction
                act_journal["state"] = STATE_APPLYING
                save_activation_journal(tx_dir, act_journal)
                tx.apply(namespace_root=ns_root)
                act_journal["state"] = STATE_APPLIED
                save_activation_journal(tx_dir, act_journal)

                # 5. Dependency-ordered reactivation & health checks
                sorted_active = sort_services_by_dependencies(active_services)
                launched_services: Set[str] = set()

                for sid in sorted_active:
                    verify_prerequisites_before_launch(sid, driver, set(active_services))
                    cfg = driver.services()[sid]
                    driver.launch(sid, cfg)
                    launched_services.add(sid)

                    # Health probe loop
                    deadline = time.monotonic() + health_timeout
                    is_healthy = False
                    while time.monotonic() < deadline:
                        if driver.healthy(sid, cfg):
                            is_healthy = True
                            break
                        time.sleep(0.1)

                    if not is_healthy:
                        raise HealthCheckError(f"Service {sid} failed health check after start")

                if supervisor_affected and daemon_was_running:
                    driver.start_daemon()
                    # Verify daemon started
                    d_deadline = time.monotonic() + 3.0
                    d_started = False
                    while time.monotonic() < d_deadline:
                        if driver.is_daemon_running():
                            d_started = True
                            break
                        time.sleep(0.1)
                    if not d_started:
                        raise HealthCheckError("Supervisor daemon failed to start")

                act_journal["state"] = STATE_HEALTHY
                save_activation_journal(tx_dir, act_journal)

                # 6. Remove temporary guards
                remove_guards(disabled_dir, act_journal["flags"], current_uid)

            # 7. Commit
            tx.commit(namespace_root=ns_root)
            act_journal["state"] = STATE_COMPLETED
            save_activation_journal(tx_dir, act_journal)

            return {
                "state": STATE_COMPLETED,
                "tx_id": tx_id,
                "affected_services": affected_services,
                "reactivated_services": active_services,
                "status": "success",
            }

        except Exception as exc:
            # If quiescence failed, keep safe guards in place and refuse file mutation
            if isinstance(exc, QuiescenceError):
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise

            # If failure happened before file apply:
            if act_journal["state"] in (STATE_PREPARED, STATE_GUARDED, STATE_QUIESCED):
                with driver.lock("services", timeout=10.0):
                    remove_guards(disabled_dir, act_journal.get("flags", {}), current_uid)
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise

            # Otherwise, files were applied or being applied: rollback coherently
            act_journal["state"] = STATE_ROLLING_BACK
            save_activation_journal(tx_dir, act_journal)
            _rollback_internal(
                tx=tx,
                act_journal=act_journal,
                driver=driver,
                disabled_dir=disabled_dir,
                ns_root=ns_root,
                tx_dir=tx_dir,
                current_uid=current_uid,
                health_timeout=health_timeout,
            )
            raise


def _rollback_internal(
    tx: rt.ReleaseTransaction,
    act_journal: Dict[str, Any],
    driver: ta.LifecycleDriver,
    disabled_dir: Path,
    ns_root: Path,
    tx_dir: Path,
    current_uid: int,
    health_timeout: float = 10.0,
) -> None:
    """Internal rollback logic executed under ActivationLock."""
    with driver.lock("services", timeout=10.0):
        # 1. Quiesce any candidate processes for affected services
        active_services = [sid for sid, info in act_journal.get("flags", {}).items() if not info.get("pre_existing")]
        for sid in active_services:
            cand_records = driver.matching_processes(sid)
            if cand_records:
                driver.terminate(cand_records)
                if sid == "agy":
                    driver.stop_agy()

        # If daemon was restarted, stop it
        if act_journal.get("supervisor_affected") and act_journal.get("daemon_was_running"):
            d_proc = driver.find_daemon_process()
            if d_proc:
                driver.stop_daemon(d_proc[0], d_proc[1])

        # 2. Rollback files
        tx.rollback(namespace_root=ns_root)

        # 3. Relaunch prior services
        sorted_active = sort_services_by_dependencies(active_services)
        for sid in sorted_active:
            cfg = driver.services()[sid]
            driver.launch(sid, cfg)
            deadline = time.monotonic() + health_timeout
            while time.monotonic() < deadline:
                if driver.healthy(sid, cfg):
                    break
                time.sleep(0.1)

        # Restart prior daemon if it was running
        if act_journal.get("supervisor_affected") and act_journal.get("daemon_was_running"):
            driver.start_daemon()

        # 4. Remove temporary guards
        remove_guards(disabled_dir, act_journal.get("flags", {}), current_uid)

    act_journal["state"] = STATE_ROLLED_BACK
    save_activation_journal(tx_dir, act_journal)


def rollback(
    transaction_dir: Path,
    namespace_root: Optional[Path] = None,
    driver: Optional[ta.LifecycleDriver] = None,
) -> Dict[str, Any]:
    """Caller-initiated rollback of prepared, applied, or committed activation."""
    tx_dir = Path(transaction_dir).resolve()
    act_journal = load_activation_journal(tx_dir)
    ns_root = Path(namespace_root).resolve() if namespace_root else Path(act_journal["namespace_root"]).resolve()
    current_uid = os.getuid()

    if act_journal.get("top_uid") != current_uid:
        raise ActivationError(f"Runtime UID {current_uid} does not match journal top_uid {act_journal.get('top_uid')}")

    if driver is None:
        rec_dir = tx_dir / "recovery_lifecycle"
        if rec_dir.exists() and (rec_dir / "server_supervisor.py").exists():
            driver = ta.load_isolated_lifecycle(rec_dir, namespace_root=ns_root)
        else:
            driver = ta.TermuxLifecycleDriver(namespace_root=ns_root)

    with ActivationLock(ns_root):
        if act_journal["state"] == STATE_ROLLED_BACK:
            # Idempotent verification
            tx = rt.ReleaseTransaction(tx_dir)
            tx_res = tx.rollback(namespace_root=ns_root)
            return {"state": STATE_ROLLED_BACK, "status": "idempotent_success", "file_status": tx_res}

        tx = rt.ReleaseTransaction(tx_dir)
        disabled_dir = ns_root / "home/.config/services_disabled"
        act_journal["state"] = STATE_ROLLING_BACK
        save_activation_journal(tx_dir, act_journal)

        _rollback_internal(
            tx=tx,
            act_journal=act_journal,
            driver=driver,
            disabled_dir=disabled_dir,
            ns_root=ns_root,
            tx_dir=tx_dir,
            current_uid=current_uid,
        )

        return {"state": STATE_ROLLED_BACK, "status": "rolled_back"}


def recover(
    transaction_dir: Path,
    namespace_root: Optional[Path] = None,
    driver: Optional[ta.LifecycleDriver] = None,
) -> Dict[str, Any]:
    """
    Recover interrupted activation without candidate bundle or live imports.
    Quiesces candidate processes, rolls back files, and restores prior services.
    """
    tx_dir = Path(transaction_dir).resolve()
    act_journal = load_activation_journal(tx_dir)
    ns_root = Path(namespace_root).resolve() if namespace_root else Path(act_journal["namespace_root"]).resolve()

    if act_journal["state"] == STATE_COMPLETED:
        # Idempotent check
        tx = rt.ReleaseTransaction(tx_dir)
        tx_res = tx.recover(namespace_root=ns_root)
        return {"state": STATE_COMPLETED, "status": "idempotent_success", "file_status": tx_res}

    return rollback(transaction_dir=tx_dir, namespace_root=ns_root, driver=driver)


def status(transaction_dir: Path) -> Dict[str, Any]:
    """Inspect activation status."""
    tx_dir = Path(transaction_dir).resolve()
    act_journal = load_activation_journal(tx_dir)
    tx = rt.ReleaseTransaction(tx_dir)
    tx_status = tx.status()
    return {
        "activation_state": act_journal.get("state"),
        "tx_id": act_journal.get("tx_id"),
        "affected_services": act_journal.get("affected_services"),
        "supervisor_affected": act_journal.get("supervisor_affected"),
        "created_at": act_journal.get("created_at"),
        "updated_at": act_journal.get("updated_at"),
        "file_transaction": tx_status,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # plan
    plan_parser = subparsers.add_parser("plan", help="Dry planning of affected destinations and services")
    plan_parser.add_argument("--bundle", required=True, help="Candidate bundle directory")
    plan_parser.add_argument("--preconditions", required=True, help="Preconditions JSON file")
    plan_parser.add_argument("--namespace-root", help="Live namespace root directory")

    # apply
    apply_parser = subparsers.add_parser("apply", help="Execute service-scoped release activation")
    apply_parser.add_argument("--bundle", required=True, help="Candidate bundle directory")
    apply_parser.add_argument("--preconditions", required=True, help="Preconditions JSON file")
    apply_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    apply_parser.add_argument("--namespace-root", help="Live namespace root directory")

    # rollback
    rb_parser = subparsers.add_parser("rollback", help="Roll back activation")
    rb_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    rb_parser.add_argument("--namespace-root", help="Live namespace root directory")

    # recover
    rec_parser = subparsers.add_parser("recover", help="Recover interrupted activation")
    rec_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    rec_parser.add_argument("--namespace-root", help="Live namespace root directory")

    # status
    st_parser = subparsers.add_parser("status", help="Inspect activation status")
    st_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")

    args = parser.parse_args()

    try:
        if args.command == "plan":
            with open(args.preconditions, "r", encoding="utf-8") as f:
                preconds = json.load(f)
            res = plan(
                bundle_root=Path(args.bundle),
                preconditions=preconds,
                namespace_root=Path(args.namespace_root) if args.namespace_root else None,
            )
            print(json.dumps(res, indent=2))
        elif args.command == "apply":
            with open(args.preconditions, "r", encoding="utf-8") as f:
                preconds = json.load(f)
            res = apply(
                bundle_root=Path(args.bundle),
                preconditions=preconds,
                transaction_dir=Path(args.transaction_dir),
                namespace_root=Path(args.namespace_root) if args.namespace_root else None,
            )
            print(json.dumps(res, indent=2))
        elif args.command == "rollback":
            res = rollback(
                transaction_dir=Path(args.transaction_dir),
                namespace_root=Path(args.namespace_root) if args.namespace_root else None,
            )
            print(json.dumps(res, indent=2))
        elif args.command == "recover":
            res = recover(
                transaction_dir=Path(args.transaction_dir),
                namespace_root=Path(args.namespace_root) if args.namespace_root else None,
            )
            print(json.dumps(res, indent=2))
        elif args.command == "status":
            res = status(transaction_dir=Path(args.transaction_dir))
            print(json.dumps(res, indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
