#!/usr/bin/env python3
"""
release_activation.py: Service-scoped release activation adapter.

Coordinates the accepted file transaction engine (scripts/release_transaction.py)
with Termux service lifecycle authority (scripts/server_supervisor.py), ensuring:
1. Destination-based service mapping (only actually changed files affect services).
2. Strict flag preservation (metadata, type, raw link, bytes) and temporary guards.
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
MANDATORY_RECOVERY_ASSETS = (
    "release_activation.py",
    "termux_activation.py",
    "release_transaction.py",
    "server_supervisor.py",
    "service_catalog.py",
)


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_sibling(module_name: str, filename: str) -> Any:
    current_dir = Path(__file__).resolve().parent
    is_recovery = (current_dir.name == "recovery_lifecycle")

    if is_recovery:
        tx_dir = current_dir.parent
        journal_path = tx_dir / "activation_journal.json"
        if not journal_path.exists():
            raise RuntimeError(f"Recovery bootstrap refused: activation journal missing at {journal_path}")
        try:
            jst = journal_path.lstat()
        except OSError as exc:
            raise RuntimeError(f"Recovery bootstrap refused: cannot stat journal: {exc}") from exc
        if stat.S_ISLNK(jst.st_mode) or not stat.S_ISREG(jst.st_mode):
            raise RuntimeError("Recovery bootstrap refused: activation journal is not a regular file")
        if jst.st_uid != os.getuid():
            raise RuntimeError(f"Recovery bootstrap refused: journal UID mismatch: expected {os.getuid()}, got {jst.st_uid}")

        try:
            with open(journal_path, "r", encoding="utf-8") as jf:
                jdata = json.load(jf)
        except Exception as exc:
            raise RuntimeError(f"Recovery bootstrap refused: invalid journal JSON: {exc}") from exc

        if not isinstance(jdata, dict) or type(jdata.get("version")) is not int or jdata["version"] != 1:
            raise RuntimeError("Recovery bootstrap refused: invalid journal version or shape")
        if type(jdata.get("top_uid")) is not int or jdata["top_uid"] != os.getuid():
            raise RuntimeError("Recovery bootstrap refused: invalid journal top_uid")

        tx_id = jdata.get("tx_id")
        tx_path_str = jdata.get("transaction_dir")
        if not isinstance(tx_id, str) or not tx_id or not isinstance(tx_path_str, str) or not tx_path_str:
            raise RuntimeError("Recovery bootstrap refused: journal missing transaction binding")
        if Path(tx_path_str).resolve() != tx_dir.resolve():
            raise RuntimeError(f"Recovery bootstrap refused: journal transaction_dir {tx_path_str} does not match {tx_dir}")

        if jdata.get("top_uid") is not None and jdata.get("top_uid") != os.getuid():
            raise RuntimeError(f"Recovery bootstrap refused: journal top_uid mismatch: expected {os.getuid()}, got {jdata.get('top_uid')}")

        rec_assets = jdata.get("recovery_assets")
        if not isinstance(rec_assets, dict):
            raise RuntimeError("Recovery bootstrap refused: recovery_assets missing or invalid schema")

        verified_bytes: Dict[str, bytes] = {}
        for req_asset in MANDATORY_RECOVERY_ASSETS:
            if req_asset not in rec_assets or not isinstance(rec_assets[req_asset], str) or not rec_assets[req_asset]:
                raise RuntimeError(f"Recovery bootstrap refused: omitted recorded hash for {req_asset}")
            asset_file = current_dir / req_asset
            if not asset_file.exists():
                raise RuntimeError(f"Recovery bootstrap refused: missing preserved code {req_asset}")
            try:
                ast = asset_file.lstat()
            except OSError as exc:
                raise RuntimeError(f"Recovery bootstrap refused: cannot stat {req_asset}: {exc}") from exc
            if stat.S_ISLNK(ast.st_mode) or not stat.S_ISREG(ast.st_mode):
                raise RuntimeError(f"Recovery bootstrap refused: {req_asset} is not a regular file")
            try:
                asset_res = asset_file.resolve()
                asset_res.relative_to(current_dir.resolve())
            except (ValueError, OSError) as exc:
                raise RuntimeError(f"Recovery bootstrap refused: path escape for {req_asset}: {exc}") from exc
            if ast.st_uid != os.getuid():
                raise RuntimeError(f"Recovery bootstrap refused: UID mismatch for {req_asset}: expected {os.getuid()}, got {ast.st_uid}")

            raw_bytes = asset_file.read_bytes()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha.lower() != rec_assets[req_asset].lower():
                raise RuntimeError(f"Recovery bootstrap refused: hash mismatch for {req_asset}")
            verified_bytes[req_asset] = raw_bytes

        file_path = current_dir / filename
        if not file_path.exists():
            raise RuntimeError(f"Recovery bootstrap refused: target sibling {filename} does not exist in recovery dir")
        source_bytes = verified_bytes[filename]
    else:
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
        source_bytes = file_path.read_bytes()

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None:
        raise RuntimeError(f"Could not load spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(file_path)
    sys.modules[module_name] = mod
    code_obj = compile(source_bytes, str(file_path), "exec")
    exec(code_obj, mod.__dict__)
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

ALLOWED_STATES = {
    STATE_PREPARED,
    STATE_GUARDED,
    STATE_QUIESCED,
    STATE_APPLYING,
    STATE_APPLIED,
    STATE_HEALTHY,
    STATE_COMPLETED,
    STATE_ROLLING_BACK,
    STATE_ROLLED_BACK,
    STATE_RECOVERY_REQUIRED,
}

DEPENDENCY_ORDER = [
    "filebrowser",
    "transfer_gateway",
    "dashboard",
    "caddy",
]

# Release Profiles
PROFILE_GENERAL = "general"
PROFILE_SUWAYOMI_FILTER = "suwayomi-filter"
VALID_PROFILES = (PROFILE_GENERAL, PROFILE_SUWAYOMI_FILTER)
SUWAYOMI_FILTER_REMOTE = "/data/data/com.termux/files/home/bin/suwayomi_graphql_filter.py"


def validate_profile(profile: Any) -> str:
    """Validate release profile strictly; fail-closed on unknown/nonstring/null."""
    if not isinstance(profile, str) or profile not in VALID_PROFILES:
        raise ActivationError(f"Invalid release profile: {profile!r}")
    return profile


class ActivationError(Exception):
    """Base exception for release activation errors."""
    pass


class ActivationLockError(ActivationError):
    """Raised when the namespace activation lock cannot be acquired."""
    pass


class QuiescenceError(ActivationError):
    """Raised when processes survive quiescence or multiple daemons match."""
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


def map_destination_to_component(remote_path: str) -> Optional[Tuple[str, bool, bool]]:
    """
    Map an owned destination path to (component_name, is_suwayomi_violation, is_agy_violation).
    Returns None if unexplained destination.
    """
    norm = remote_path.replace("\\", "/").rstrip("/")

    # Check for Suwayomi scripts
    if norm == SUWAYOMI_FILTER_REMOTE:
        return ("suwayomi_filter", True, False)

    for sw in ("start_suwayomi.sh", "suwayomi_daily_update.py", "suwayomi_update.sh"):
        if norm.endswith(f"home/bin/{sw}"):
            return ("suwayomi", True, False)

    # Check for AGY runtime assets - must NOT change
    if (
        norm.endswith("home/bin/agy_session.sh")
        or norm.endswith("home/.config/ttyd/agy_index.html")
        or norm.endswith("usr/bin/agy-resume")
        or "agy-runner-loop" in norm
    ):
        return ("agy", False, True)

    # Dashboard group:
    if norm.endswith("usr/bin/dashboard"):
        return ("dashboard", False, False)
    if "/home/.config/dashboard/static/" in norm:
        return ("dashboard", False, False)
    if norm.endswith("home/.local/lib/mobile-server/service_catalog.py"):
        return ("service_catalog", False, False)
    if "/home/.local/lib/mobile-server/" in norm:
        return ("dashboard", False, False)

    # Gateway:
    if norm.endswith("home/bin/mobile-gateway"):
        return ("transfer_gateway", False, False)

    # Caddy:
    if norm.endswith("home/.config/caddy/Caddyfile"):
        return ("caddy", False, False)

    # Supervisor:
    if norm.endswith("home/bin/server_supervisor.py"):
        return ("supervisor", False, False)

    # Terminal:
    if norm.endswith("home/bin/termux_session.sh") or norm.endswith("home/.config/ttyd/termux_index.html"):
        return ("terminal", False, False)

    # Laptop terminal:
    if norm.endswith("home/bin/laptop_session.py"):
        return ("laptop_terminal", False, False)

    # Battery guard:
    if norm.endswith("home/bin/battery_guard.sh"):
        return ("battery_guard", False, False)

    # Tor:
    if norm.endswith("home/.config/tor/torrc"):
        return ("tor", False, False)

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
            return ("cli_only", False, False)

    return None


def map_changed_destinations(
    entries: List[Dict[str, Any]],
    profile: str = PROFILE_GENERAL,
) -> Tuple[Set[str], bool, List[str]]:
    """
    Given prepared transaction entries and release profile, return:
    (affected_services, supervisor_affected, changed_destinations)
    """
    profile = validate_profile(profile)

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

        comp, is_sw, is_agy = mapping
        if is_agy:
            raise ActivationError("AGY launcher/runner/ttyd runtime-asset changes are prohibited in this release")

        if profile == PROFILE_GENERAL:
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

        elif profile == PROFILE_SUWAYOMI_FILTER:
            norm = remote.replace("\\", "/").rstrip("/")
            if norm != SUWAYOMI_FILTER_REMOTE:
                raise ActivationError(
                    f"Profile '{PROFILE_SUWAYOMI_FILTER}' permits only {SUWAYOMI_FILTER_REMOTE} to change, found: {remote}"
                )
            if action != rt.ACTION_REPLACE:
                raise ActivationError(
                    f"Profile '{PROFILE_SUWAYOMI_FILTER}' permits only modifying existing {SUWAYOMI_FILTER_REMOTE}"
                )
            if len(changed_destinations) > 1:
                raise ActivationError(
                    f"Profile '{PROFILE_SUWAYOMI_FILTER}' permits only a single changed destination"
                )
            affected_services.add("suwayomi_filter")
            supervisor_affected = False

    if "ssh" in affected_services:
        raise ActivationError("SSH service restart is strictly prohibited")

    return affected_services, supervisor_affected, changed_destinations


def save_activation_journal(tx_dir: Path, data: Dict[str, Any]) -> None:
    data["updated_at"] = time.time()
    payload = json.dumps(data, indent=2).encode("utf-8")
    journal_path = tx_dir / "activation_journal.json"
    tmp_path = tx_dir / f".tmp_act_{uuid.uuid4().hex[:8]}.json"

    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            rt.write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(journal_path))
        rt.fsync_dir(tx_dir)
    finally:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def load_activation_journal(tx_dir: Path) -> Dict[str, Any]:
    journal_path = tx_dir / "activation_journal.json"
    if not journal_path.exists():
        raise FileNotFoundError(f"Activation journal not found: {journal_path}")
    with open(journal_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != JOURNAL_VERSION:
        raise ActivationError(f"Unsupported activation journal version: {data.get('version')}")
    state = data.get("state")
    if state not in ALLOWED_STATES:
        raise ActivationError(f"Invalid activation journal state: {state}")
    if "profile" in data:
        validate_profile(data["profile"])
    return data


def validate_activation_journal(
    act_journal: Dict[str, Any],
    tx_journal: Dict[str, Any],
    expected_ns_root: Path,
    expected_tx_dir: Path,
    expected_uid: int,
) -> None:
    """Strictly validate activation journal bindings against the verified file transaction."""
    if not isinstance(act_journal, dict) or not isinstance(tx_journal, dict):
        raise ActivationError("Malformed journal dictionary")

    version = act_journal.get("version")
    if version != JOURNAL_VERSION:
        raise ActivationError(f"Unsupported activation journal version: {version}")

    state = act_journal.get("state")
    if state not in ALLOWED_STATES:
        raise ActivationError(f"Invalid activation journal state: {state}")

    # Validate profile: absent means general; explicit invalid refuses
    raw_profile = act_journal.get("profile", PROFILE_GENERAL)
    validated_profile = validate_profile(raw_profile)

    act_ns = Path(act_journal.get("namespace_root", "")).resolve()
    tx_ns = Path(tx_journal.get("namespace_root", "")).resolve()
    if act_ns != expected_ns_root or tx_ns != expected_ns_root:
        raise ActivationError(
            f"Namespace mismatch: act={act_ns}, tx={tx_ns}, expected={expected_ns_root}"
        )

    act_dir = Path(act_journal.get("transaction_dir", "")).resolve()
    if act_dir != expected_tx_dir:
        raise ActivationError(
            f"Transaction directory mismatch: act={act_dir}, expected={expected_tx_dir}"
        )

    top_uid = act_journal.get("top_uid")
    if top_uid != expected_uid or tx_journal.get("top_uid") != expected_uid:
        raise ActivationError(
            f"Top UID mismatch: act={top_uid}, tx={tx_journal.get('top_uid')}, expected={expected_uid}"
        )

    # Recompute affected services from verified file engine entries using validated profile
    recomputed_services, recomputed_sup, _ = map_changed_destinations(
        tx_journal.get("entries", []),
        profile=validated_profile,
    )

    act_services_raw = act_journal.get("affected_services")
    if not isinstance(act_services_raw, list):
        raise ActivationError("affected_services must be a list")
    for s in act_services_raw:
        if not isinstance(s, str):
            raise ActivationError(f"Service name in affected_services must be a string: {s}")
    if len(act_services_raw) != len(set(act_services_raw)):
        raise ActivationError("Duplicate service name in affected_services")

    act_services = set(act_services_raw)
    if act_services != recomputed_services:
        raise ActivationError(
            f"Affected services mismatch: journal={act_services}, recomputed={recomputed_services}"
        )

    sup_affected = act_journal.get("supervisor_affected")
    if not isinstance(sup_affected, bool) or sup_affected != recomputed_sup:
        raise ActivationError(
            f"Supervisor affected mismatch: journal={sup_affected}, recomputed={recomputed_sup}"
        )

    tx_id = act_journal.get("tx_id")
    if not isinstance(tx_id, str) or not tx_id:
        raise ActivationError(f"Invalid activation journal tx_id: {tx_id}")

    daemon_was_running = act_journal.get("daemon_was_running")
    if not isinstance(daemon_was_running, bool):
        raise ActivationError(
            f"daemon_was_running must be boolean: {daemon_was_running}"
        )

    # Preflight flag structure
    flags = act_journal.get("flags")
    if not isinstance(flags, dict):
        raise ActivationError("Malformed flags mapping in activation journal")

    # Safe branch for genuinely interrupted pre-snapshot PREPARED transaction:
    if state == STATE_PREPARED and len(flags) == 0:
        if tx_journal.get("state") != rt.STATE_PREPARED:
            raise ActivationError("Pre-snapshot activation requires an unchanged PREPARED file transaction")
        return

    # Once past pre-snapshot PREPARED, flags keys must strictly match act_services
    if set(flags.keys()) != act_services:
        raise ActivationError(
            f"Flags mapping keys do not match affected services: flags={set(flags.keys())}, affected={act_services}"
        )

    for sid, info in flags.items():
        if not isinstance(info, dict):
            raise ActivationError(f"Malformed flag info for {sid}: must be dict")
        pre_existing = info.get("pre_existing")
        if not isinstance(pre_existing, bool):
            raise ActivationError(f"Flag {sid} pre_existing must be boolean: {pre_existing}")

        if pre_existing:
            for field in ("dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"):
                val = info.get(field)
                if val is None or not isinstance(val, int) or isinstance(val, bool):
                    raise ActivationError(f"Pre-existing flag {sid} missing or non-integer metadata field: {field}={val}")
            kind = info.get("kind")
            if kind not in ("symlink", "file", "other"):
                raise ActivationError(f"Pre-existing flag {sid} invalid kind: {kind}")
            if kind == "symlink" and not isinstance(info.get("raw_link"), str):
                raise ActivationError(f"Pre-existing symlink flag {sid} missing string raw_link")
            if kind == "file":
                sha = info.get("sha256")
                if not isinstance(sha, str) or len(sha) != 64:
                    raise ActivationError(f"Pre-existing file flag {sid} missing or invalid sha256: {sha}")
        else:
            token = info.get("token")
            expected_token = f"release_guard:{tx_id}:{sid}"
            if token != expected_token:
                raise ActivationError(f"Temporary guard {sid} token forged or invalid: expected {expected_token}, got {token}")
            guard_created = info.get("guard_created")
            if not isinstance(guard_created, bool):
                raise ActivationError(f"Temporary guard {sid} guard_created must be boolean: {guard_created}")
            intent = info.get("intent")
            if intent is None:
                intent = "guarded" if guard_created else "create_guard"
            if intent not in ("create_guard", "recreate_guard", "guarded", "remove_guard", "removed"):
                raise ActivationError(f"Temporary guard {sid} invalid intent: {intent}")
            if guard_created:
                if info.get("ino") is not None and info.get("dev") is not None:
                    for field in ("dev", "ino", "mode", "uid", "mtime_ns", "ctime_ns"):
                        val = info.get(field)
                        if val is None or not isinstance(val, int) or isinstance(val, bool):
                            raise ActivationError(f"Created guard {sid} missing or invalid metadata field: {field}={val}")
                    if info.get("mode") != 0o600:
                        raise ActivationError(f"Created guard {sid} mode is not 0o600: {oct(info.get('mode', 0))}")
                    if info.get("uid") != expected_uid:
                        raise ActivationError(f"Created guard {sid} UID mismatch: expected {expected_uid}, got {info.get('uid')}")
                else:
                    if intent not in ("create_guard", "recreate_guard") or info.get("ino") is not None or info.get("dev") is not None:
                        raise ActivationError(f"Created guard {sid} missing ino/dev without active creation intent")


def _guard_payload(token: str, tx_id: str, sid: str) -> bytes:
    return f"RELEASE_GUARD_TOKEN={token}\nTX_ID={tx_id}\nSID={sid}\n".encode("utf-8")


def _recognize_interrupted_guard(
    disabled_dir: Path,
    sid: str,
    info: Dict[str, Any],
    tx_id: str,
    current_uid: int,
    tx_dir: Path,
    act_journal: Dict[str, Any],
) -> bool:
    """
    Recognize a guard on disk matching our exact ownership payload and recorded intent.
    Adopts its dev/inode and persists to activation journal.
    """
    flag_path = disabled_dir / sid
    if not flag_path.exists() and not flag_path.is_symlink():
        return False
    st = flag_path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ConcurrencyError(f"Marker {flag_path} is not a regular file")
    if st.st_uid != current_uid:
        raise ConcurrencyError(f"Marker {flag_path} UID mismatch: expected {current_uid}, got {st.st_uid}")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise ConcurrencyError(f"Marker {flag_path} mode mismatch: expected 0o600, got {oct(st.st_mode)}")

    expected_payload = _guard_payload(info["token"], tx_id, sid).decode("utf-8")
    content = flag_path.read_text(encoding="utf-8")
    if content != expected_payload:
        raise ConcurrencyError(f"Marker {flag_path} content does not match expected guard token")

    info["guard_created"] = True
    info["intent"] = "guarded"
    info["dev"] = int(st.st_dev)
    info["ino"] = int(st.st_ino)
    info["mode"] = int(stat.S_IMODE(st.st_mode))
    info["uid"] = int(st.st_uid)
    info["mtime_ns"] = int(st.st_mtime_ns)
    info["ctime_ns"] = int(st.st_ctime_ns)
    save_activation_journal(tx_dir, act_journal)
    return True


def _create_or_recreate_guard(
    disabled_dir: Path,
    sid: str,
    info: Dict[str, Any],
    tx_id: str,
    current_uid: int,
    tx_dir: Path,
    act_journal: Dict[str, Any],
    is_recreation: bool = False,
) -> None:
    """
    Journal intent before filesystem mutation.
    Create guard using O_EXCL/no-clobber, write exact token payload, fsync, and save new dev/inode.
    Never os.replace over a concurrently created marker or broken symlink.
    """
    flag_path = disabled_dir / sid

    # Journal intent and clear stale identity before filesystem mutation
    if is_recreation:
        info["intent"] = "recreate_guard"
        info["ino"] = None
        info["dev"] = None
        info["mtime_ns"] = None
        info["ctime_ns"] = None
        info["guard_created"] = False
        save_activation_journal(tx_dir, act_journal)
    else:
        info["intent"] = "create_guard"
        save_activation_journal(tx_dir, act_journal)

    # Check if a crash happened after file creation but before inode persistence
    if flag_path.exists() or flag_path.is_symlink():
        if info.get("ino") is None and _recognize_interrupted_guard(disabled_dir, sid, info, tx_id, current_uid, tx_dir, act_journal):
            return
        raise ConcurrencyError(f"Marker {flag_path} already exists and is not recognized as our guard")

    payload = _guard_payload(info["token"], tx_id, sid)
    try:
        fd = os.open(str(flag_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConcurrencyError(f"Concurrent marker or broken symlink appeared at {flag_path}") from exc

    try:
        rt.write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    rt.fsync_dir(disabled_dir)

    st = flag_path.lstat()
    info["guard_created"] = True
    info["intent"] = "guarded"
    info["dev"] = int(st.st_dev)
    info["ino"] = int(st.st_ino)
    info["mode"] = int(stat.S_IMODE(st.st_mode))
    info["uid"] = int(st.st_uid)
    info["mtime_ns"] = int(st.st_mtime_ns)
    info["ctime_ns"] = int(st.st_ctime_ns)
    save_activation_journal(tx_dir, act_journal)


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
    Snapshot original flag intent for ALL affected services and persist before creating first guard.
    Preserve pre-existing flags unchanged with full metadata.
    Install temporary guards for originally absent flags with ownership token and fsync.
    Returns: (active_affected_services, disabled_affected_services)
    """
    disabled_dir.mkdir(parents=True, exist_ok=True)
    flags = journal.setdefault("flags", {})
    active_services: List[str] = []
    disabled_services: List[str] = []

    # 1. Complete snapshot for ALL affected services
    for sid in sorted(affected_services):
        flag_path = disabled_dir / sid
        if flag_path.exists() or flag_path.is_symlink():
            st = flag_path.lstat()
            is_link = stat.S_ISLNK(st.st_mode)
            raw_link = os.readlink(flag_path) if is_link else None
            sha = ""
            if not is_link and stat.S_ISREG(st.st_mode):
                sha = compute_file_sha256(flag_path)
            flags[sid] = {
                "pre_existing": True,
                "kind": "symlink" if is_link else ("file" if stat.S_ISREG(st.st_mode) else "other"),
                "raw_link": raw_link,
                "dev": int(st.st_dev),
                "ino": int(st.st_ino),
                "mode": int(stat.S_IMODE(st.st_mode)),
                "uid": int(st.st_uid),
                "gid": int(st.st_gid),
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "ctime_ns": int(st.st_ctime_ns),
                "sha256": sha,
                "intent": "preserve",
            }
            disabled_services.append(sid)
        else:
            token = f"release_guard:{tx_id}:{sid}"
            flags[sid] = {
                "pre_existing": False,
                "intent": "create_guard",
                "token": token,
                "guard_created": False,
                "dev": None,
                "ino": None,
                "mode": 0o600,
                "uid": uid,
                "mtime_ns": None,
                "ctime_ns": None,
            }
            active_services.append(sid)

    # Persist complete snapshot before creating the first guard!
    save_activation_journal(tx_dir, journal)

    # 2. Create guards using no-clobber helper
    for sid in active_services:
        _create_or_recreate_guard(
            disabled_dir=disabled_dir,
            sid=sid,
            info=flags[sid],
            tx_id=tx_id,
            current_uid=uid,
            tx_dir=tx_dir,
            act_journal=journal,
            is_recreation=False,
        )

    return active_services, disabled_services


def revalidate_all_flags(
    disabled_dir: Path,
    flags: Dict[str, Any],
    expected_uid: int,
    tx_id: str,
) -> None:
    """
    Strictly re-verify every affected marker under services.lock.
    Pre-existing markers: verify existence, type, raw link, UID, mode, device, inode, size, mtime, ctime, and hash.
    Temporary guards: verify regular file, UID, mode 0600, device, inode, exact token payload, and timestamps.
    Fails closed immediately on any mismatch.
    """
    for sid, info in flags.items():
        if not isinstance(info, dict):
            raise ActivationError(f"Malformed flag info for {sid}: must be dict")
        pre_existing = info.get("pre_existing")
        if not isinstance(pre_existing, bool):
            raise ActivationError(f"Flag {sid} pre_existing must be boolean: {pre_existing}")

        flag_path = disabled_dir / sid
        if pre_existing:
            for field in ("dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"):
                val = info.get(field)
                if val is None or not isinstance(val, int) or isinstance(val, bool):
                    raise ActivationError(f"Pre-existing flag {sid} missing or non-integer metadata field: {field}={val}")
            kind = info.get("kind")
            if kind not in ("symlink", "file", "other"):
                raise ActivationError(f"Pre-existing flag {sid} invalid kind: {kind}")
            if kind == "symlink" and not isinstance(info.get("raw_link"), str):
                raise ActivationError(f"Pre-existing symlink flag {sid} missing string raw_link")
            if kind == "file":
                sha = info.get("sha256")
                if not isinstance(sha, str) or len(sha) != 64:
                    raise ActivationError(f"Pre-existing file flag {sid} missing or invalid sha256: {sha}")

            if not flag_path.exists() and not flag_path.is_symlink():
                raise ConcurrencyError(f"Pre-existing disabled flag {flag_path} vanished before file mutation")
            st = flag_path.lstat()
            if st.st_dev != info["dev"] or st.st_ino != info["ino"]:
                raise ConcurrencyError(f"Pre-existing flag {flag_path} inode/dev replaced concurrently")
            if stat.S_IMODE(st.st_mode) != info["mode"]:
                raise ConcurrencyError(f"Pre-existing flag {flag_path} mode changed concurrently")
            if st.st_uid != info["uid"] or st.st_gid != info["gid"]:
                raise ConcurrencyError(f"Pre-existing flag {flag_path} UID/GID changed concurrently")
            if st.st_size != info["size"]:
                raise ConcurrencyError(f"Pre-existing flag {flag_path} size changed concurrently")
            if st.st_mtime_ns != info["mtime_ns"] or st.st_ctime_ns != info["ctime_ns"]:
                raise ConcurrencyError(f"Pre-existing flag {flag_path} timestamps modified concurrently")
            if info["kind"] == "symlink":
                if not stat.S_ISLNK(st.st_mode) or os.readlink(flag_path) != info["raw_link"]:
                    raise ConcurrencyError(f"Pre-existing symlink {flag_path} retargeted concurrently")
            elif info["kind"] == "file":
                if not stat.S_ISREG(st.st_mode) or compute_file_sha256(flag_path) != info["sha256"]:
                    raise ConcurrencyError(f"Pre-existing flag {flag_path} content modified concurrently")
        else:
            token = info.get("token")
            expected_token = f"release_guard:{tx_id}:{sid}"
            if token != expected_token:
                raise ActivationError(f"Temporary guard {sid} token forged or invalid: expected {expected_token}, got {token}")

            if not flag_path.exists():
                raise ConcurrencyError(f"Temporary guard {flag_path} vanished before verification")
            st = flag_path.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise ConcurrencyError(f"Guard flag {flag_path} is not a regular file")
            if st.st_uid != expected_uid:
                raise ConcurrencyError(f"Guard flag {flag_path} UID mismatch: expected {expected_uid}, got {st.st_uid}")
            if stat.S_IMODE(st.st_mode) != 0o600:
                raise ConcurrencyError(f"Guard flag {flag_path} mode mismatch: expected 0o600, got {oct(st.st_mode)}")
            expected_payload = f"RELEASE_GUARD_TOKEN={info['token']}\nTX_ID={tx_id}\nSID={sid}\n"
            content = flag_path.read_text(encoding="utf-8")
            if content != expected_payload:
                raise ConcurrencyError(f"Guard flag {flag_path} content tampered concurrently")

            for field in ("dev", "ino", "mode", "uid", "mtime_ns", "ctime_ns"):
                val = info.get(field)
                if val is None or not isinstance(val, int) or isinstance(val, bool):
                    raise ActivationError(f"Created guard {sid} missing or invalid metadata field: {field}={val}")
            if info.get("mode") != 0o600:
                raise ActivationError(f"Created guard {sid} mode is not 0o600: {oct(info.get('mode', 0))}")
            if info.get("uid") != expected_uid:
                raise ActivationError(f"Created guard {sid} UID mismatch: expected {expected_uid}, got {info.get('uid')}")

            if st.st_ino != info["ino"] or st.st_dev != info["dev"]:
                raise ConcurrencyError(f"Guard flag {flag_path} inode/dev mismatch")
            if st.st_mtime_ns != info["mtime_ns"] or st.st_ctime_ns != info["ctime_ns"]:
                raise ConcurrencyError(f"Guard flag {flag_path} timestamps modified concurrently")


def remove_temporary_guards(
    disabled_dir: Path,
    flags: Dict[str, Any],
    expected_uid: int,
    tx_id: str,
    act_journal: Optional[Dict[str, Any]] = None,
    tx_dir: Optional[Path] = None,
) -> None:
    """Remove temporary guards after full revalidation; never unlink on concurrency failure."""
    revalidate_all_flags(disabled_dir, flags, expected_uid, tx_id)
    for sid, info in flags.items():
        if info.get("pre_existing", False):
            continue
        flag_path = disabled_dir / sid
        if flag_path.exists() or flag_path.is_symlink():
            if act_journal is not None and tx_dir is not None:
                info["intent"] = "remove_guard"
                save_activation_journal(tx_dir, act_journal)
            flag_path.unlink()
            rt.fsync_dir(disabled_dir)
            if act_journal is not None and tx_dir is not None:
                info["guard_created"] = False
                info["intent"] = "removed"
                info["ino"] = None
                info["dev"] = None
                info["mtime_ns"] = None
                info["ctime_ns"] = None
                save_activation_journal(tx_dir, act_journal)
    rt.fsync_dir(disabled_dir)


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
        if fb_cfg:
            if not driver.healthy("filebrowser", fb_cfg):
                raise HealthCheckError("FileBrowser must already be healthy before launching transfer_gateway")
    elif sid == "caddy":
        dash_cfg = all_cfgs.get("dashboard")
        if dash_cfg:
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
    """Preserve standalone copies of lifecycle scripts in tx_dir/recovery_lifecycle."""
    rec_dir = tx_dir / "recovery_lifecycle"
    rec_dir.mkdir(parents=True, exist_ok=True)
    hashes: Dict[str, str] = {}

    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent

    # 1. server_supervisor.py
    live_sup = namespace_root / "home/bin/server_supervisor.py"
    src_sup = live_sup if live_sup.exists() else scripts_dir / "server_supervisor.py"
    dst_sup = rec_dir / "server_supervisor.py"
    shutil.copy2(src_sup, dst_sup)

    # 2. service_catalog.py
    live_cat = namespace_root / "home/.local/lib/mobile-server/service_catalog.py"
    src_cat = live_cat if live_cat.exists() else (repo_root / "app/service_catalog.py" if (repo_root / "app/service_catalog.py").exists() else scripts_dir / "service_catalog.py")
    dst_cat = rec_dir / "service_catalog.py"
    shutil.copy2(src_cat, dst_cat)

    # 3. release_activation.py
    dst_act = rec_dir / "release_activation.py"
    shutil.copy2(scripts_dir / "release_activation.py", dst_act)

    # 4. termux_activation.py
    dst_ta = rec_dir / "termux_activation.py"
    shutil.copy2(scripts_dir / "termux_activation.py", dst_ta)

    # 5. release_transaction.py
    dst_rt = rec_dir / "release_transaction.py"
    shutil.copy2(scripts_dir / "release_transaction.py", dst_rt)

    # Flush copies and directory before recording hashes/publishing journal
    for fname in MANDATORY_RECOVERY_ASSETS:
        fpath = rec_dir / fname
        with open(fpath, "rb") as f:
            os.fsync(f.fileno())
        hashes[fname] = compute_file_sha256(fpath)

    rt.fsync_dir(rec_dir)

    return hashes


def validate_recovery_assets(
    recovery_dir: Path,
    expected_hashes: Dict[str, str],
    current_uid: int,
) -> None:
    """Validate all preserved recovery assets before loading or executing them."""
    rec_dir = Path(recovery_dir).resolve()
    if not isinstance(expected_hashes, dict):
        raise ActivationError("Recovery expected_hashes must be a dictionary")

    for name in MANDATORY_RECOVERY_ASSETS:
        if name not in expected_hashes or not expected_hashes[name]:
            raise ActivationError(f"Mandatory recovery asset {name} missing from expected_hashes")

    for name in MANDATORY_RECOVERY_ASSETS:
        expected_sha = expected_hashes[name]
        file_path = rec_dir / name
        try:
            resolved = file_path.resolve()
            resolved.relative_to(rec_dir)
        except (ValueError, OSError) as exc:
            raise ActivationError(f"Recovery asset {name} path traversal escape: {exc}") from exc

        if not file_path.exists():
            raise ActivationError(f"Recovery asset missing: {file_path}")
        st = file_path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ActivationError(f"Recovery asset {name} is not a regular file")
        if st.st_uid != current_uid:
            raise ActivationError(f"Recovery asset {name} UID mismatch: expected {current_uid}, got {st.st_uid}")
        actual_sha = compute_file_sha256(file_path)
        if actual_sha.lower() != expected_sha.lower():
            raise ActivationError(
                f"Recovery asset {name} hash mismatch: expected {expected_sha}, got {actual_sha}"
            )


def plan(
    bundle_root: Path,
    preconditions: Dict[str, Any],
    namespace_root: Optional[Path] = None,
    profile: str = PROFILE_GENERAL,
) -> Dict[str, Any]:
    """Dry planning: determine changed destinations and affected services without disk writes."""
    profile = validate_profile(profile)
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

    affected_services, supervisor_affected, _ = map_changed_destinations(
        changed_destinations,
        profile=profile,
    )

    return {
        "status": "plan_ok",
        "profile": profile,
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
    profile: str = PROFILE_GENERAL,
) -> Dict[str, Any]:
    """Execute service-scoped release activation."""
    profile = validate_profile(profile)
    b_root = Path(bundle_root).resolve()
    tx_dir = Path(transaction_dir).resolve()
    ns_root = Path(namespace_root).resolve() if namespace_root else Path("/data/data/com.termux/files")

    current_uid = os.getuid()
    if preconditions.get("uid") != current_uid:
        raise ActivationError(f"Runtime UID {current_uid} does not match preconditions UID {preconditions.get('uid')}")

    if driver is None:
        driver = ta.TermuxLifecycleDriver(namespace_root=ns_root)

    with ActivationLock(ns_root):
        if driver.is_paused():
            raise ActivationError("Server is deliberately paused; activation refused")

        manifest_mod = rt._load_release_manifest_module()
        manifest_entries = manifest_mod.generate_manifest(b_root, require_gateway=True)

        tx = rt.prepare(
            bundle_root=b_root,
            candidate_manifest=manifest_entries,
            preconditions=preconditions,
            transaction_dir=tx_dir,
            namespace_root=ns_root,
        )

        if hasattr(driver, "validate_candidate_services"):
            driver.validate_candidate_services(b_root)

        tx_journal = tx.load_journal()
        entries = tx_journal.get("entries", [])
        affected_services_set, supervisor_affected, _ = map_changed_destinations(
            entries,
            profile=profile,
        )
        affected_services = sorted(list(affected_services_set))

        tx_id = f"act_{uuid.uuid4().hex[:12]}"
        recovery_hashes = preserve_recovery_lifecycle_code(tx_dir, ns_root)

        act_journal: Dict[str, Any] = {
            "version": JOURNAL_VERSION,
            "profile": profile,
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

        # No-op / CLI-only path wrapped in robust fault boundary
        if not affected_services and not supervisor_affected:
            try:
                act_journal["state"] = STATE_APPLYING
                save_activation_journal(tx_dir, act_journal)
                tx.apply(namespace_root=ns_root)
                act_journal["state"] = STATE_APPLIED
                save_activation_journal(tx_dir, act_journal)
                tx.commit(namespace_root=ns_root)
                act_journal["state"] = STATE_COMPLETED
                save_activation_journal(tx_dir, act_journal)
                return {
                    "state": STATE_COMPLETED,
                    "profile": profile,
                    "affected_services": [],
                    "status": "completed_noop_services",
                }
            except Exception:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                with contextlib.suppress(Exception):
                    tx.rollback(namespace_root=ns_root)
                raise

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
                if "agy" in active_services:
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
                        if daemon_proc is None:
                            raise QuiescenceError(
                                "Supervisor daemon is running (lock held) but exact process could not be identified"
                            )
                        act_journal["daemon_record"] = daemon_proc
                        save_activation_journal(tx_dir, act_journal)

            # Stop daemon outside services.lock
            if supervisor_affected and daemon_was_running and daemon_proc:
                driver.stop_daemon(daemon_proc[0], daemon_proc[1])

            # Re-acquire services.lock and re-verify everything
            with driver.lock("services", timeout=10.0):
                if driver.is_paused():
                    raise ActivationError("Server became paused during activation; refusing file mutation")

                # Revalidate ALL flags (pre-existing and guards)
                revalidate_all_flags(disabled_dir, act_journal["flags"], current_uid, tx_id)

                # Re-verify active services dead
                for sid in active_services:
                    if driver.matching_processes(sid):
                        raise QuiescenceError(f"Service {sid} processes reappeared before file mutation")

                # Re-verify daemon dead and not replaced
                if supervisor_affected and daemon_was_running:
                    if driver.is_daemon_running() or driver.find_daemon_process() is not None:
                        raise QuiescenceError("Supervisor daemon reappeared or replaced during unlocked gap")

                # Apply file transaction
                act_journal["state"] = STATE_APPLYING
                save_activation_journal(tx_dir, act_journal)
                tx.apply(namespace_root=ns_root)
                act_journal["state"] = STATE_APPLIED
                save_activation_journal(tx_dir, act_journal)

                # Reload lifecycle authority to bind candidate configuration
                driver.reload_lifecycle()

                # Dependency-ordered reactivation & health checks
                sorted_active = sort_services_by_dependencies(active_services)
                for sid in sorted_active:
                    verify_prerequisites_before_launch(sid, driver, set(active_services))
                    cfg = driver.services()[sid]
                    driver.launch(sid, cfg)

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

                # Remove temporary guards
                remove_temporary_guards(disabled_dir, act_journal["flags"], current_uid, tx_id, act_journal, tx_dir)

            # Commit
            tx.commit(namespace_root=ns_root)
            act_journal["state"] = STATE_COMPLETED
            save_activation_journal(tx_dir, act_journal)

            return {
                "state": STATE_COMPLETED,
                "profile": profile,
                "tx_id": tx_id,
                "affected_services": affected_services,
                "reactivated_services": active_services,
                "status": "success",
            }

        except Exception as exc:
            if isinstance(exc, (QuiescenceError, ta.QuiescenceError, ConcurrencyError)):
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise

            if act_journal["state"] in (STATE_PREPARED, STATE_GUARDED, STATE_QUIESCED):
                with driver.lock("services", timeout=10.0):
                    with contextlib.suppress(Exception):
                        remove_temporary_guards(disabled_dir, act_journal.get("flags", {}), current_uid, tx_id)
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise

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
    """Internal rollback logic: reinstall guards, verify candidate death, rollback files, restart prior."""
    tx_id = act_journal["tx_id"]
    affected_services = act_journal.get("affected_services", [])
    flags = act_journal.get("flags", {})
    if set(flags.keys()) != set(affected_services):
        raise ActivationError("Flags mapping keys do not match affected services")
    active_services = [sid for sid in affected_services if not flags[sid].get("pre_existing")]

    with driver.lock("services", timeout=10.0):
        disabled_dir.mkdir(parents=True, exist_ok=True)
        # 1. Reinstall temporary guards for all active services whose guards are missing
        for sid in active_services:
            info = flags[sid]
            gp = disabled_dir / sid
            if not gp.exists() and not gp.is_symlink():
                _create_or_recreate_guard(
                    disabled_dir=disabled_dir,
                    sid=sid,
                    info=info,
                    tx_id=tx_id,
                    current_uid=current_uid,
                    tx_dir=tx_dir,
                    act_journal=act_journal,
                    is_recreation=True,
                )
            elif info.get("ino") is None and info.get("intent") in ("create_guard", "recreate_guard"):
                _recognize_interrupted_guard(
                    disabled_dir=disabled_dir,
                    sid=sid,
                    info=info,
                    tx_id=tx_id,
                    current_uid=current_uid,
                    tx_dir=tx_dir,
                    act_journal=act_journal,
                )

        # Revalidate all flags
        revalidate_all_flags(disabled_dir, flags, current_uid, tx_id)

        # 2. Quiesce candidate processes and verify death
        for sid in active_services:
            cand_records = driver.matching_processes(sid)
            if cand_records:
                driver.terminate(cand_records)
            if sid == "agy":
                driver.stop_agy()

            # Verify death
            surviving = [r for r in cand_records if driver.process_identity(r[0]) == r[1]]
            remaining = driver.matching_processes(sid)
            if surviving or remaining:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise QuiescenceError(
                    f"Candidate process {sid} survived termination during rollback: surviving={surviving}, remaining={remaining}"
                )

        d_proc = None
        if act_journal.get("supervisor_affected") and act_journal.get("daemon_was_running"):
            if driver.is_daemon_running():
                d_proc = driver.find_daemon_process()
                if d_proc is None:
                    act_journal["state"] = STATE_RECOVERY_REQUIRED
                    save_activation_journal(tx_dir, act_journal)
                    raise QuiescenceError("Daemon lock held during rollback but exact process could not be found")

    # Stop daemon outside services.lock if running
    if d_proc:
        driver.stop_daemon(d_proc[0], d_proc[1])

    with driver.lock("services", timeout=10.0):
        # Recheck flag integrity immediately before tx.rollback
        revalidate_all_flags(disabled_dir, flags, current_uid, tx_id)

        # Recheck candidate process identities and fresh matching_processes
        for sid in active_services:
            reappeared = driver.matching_processes(sid)
            if reappeared:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise QuiescenceError(
                    f"Candidate process {sid} reappeared during unlocked daemon gap before file rollback: {reappeared}"
                )

        # Recheck required daemon quiescence
        if act_journal.get("supervisor_affected") and act_journal.get("daemon_was_running"):
            if driver.is_daemon_running() or driver.find_daemon_process() is not None:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise QuiescenceError("Supervisor daemon reappeared or replaced during unlocked gap before file rollback")

        # 3. Rollback files
        tx.rollback(namespace_root=ns_root)

        # Reload prior lifecycle
        driver.reload_lifecycle()

        # 4. Relaunch prior services
        sorted_active = sort_services_by_dependencies(active_services)
        for sid in sorted_active:
            cfg = driver.services()[sid]
            driver.launch(sid, cfg)
            deadline = time.monotonic() + health_timeout
            is_healthy = False
            while time.monotonic() < deadline:
                if driver.healthy(sid, cfg):
                    is_healthy = True
                    break
                time.sleep(0.1)
            if not is_healthy:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise HealthCheckError(f"Prior service {sid} failed health check during rollback")

        # Restart prior daemon if it was running
        if act_journal.get("supervisor_affected") and act_journal.get("daemon_was_running"):
            driver.start_daemon()
            d_deadline = time.monotonic() + 3.0
            d_started = False
            while time.monotonic() < d_deadline:
                if driver.is_daemon_running():
                    d_started = True
                    break
                time.sleep(0.1)
            if not d_started:
                act_journal["state"] = STATE_RECOVERY_REQUIRED
                save_activation_journal(tx_dir, act_journal)
                raise HealthCheckError("Prior supervisor daemon failed to restart during rollback")

        # 5. Remove temporary guards
        remove_temporary_guards(disabled_dir, flags, current_uid, tx_id, act_journal, tx_dir)

    act_journal["state"] = STATE_ROLLED_BACK
    save_activation_journal(tx_dir, act_journal)


def _rollback_locked(
    tx_dir: Path,
    act_journal: Dict[str, Any],
    tx: rt.ReleaseTransaction,
    tx_journal: Dict[str, Any],
    ns_root: Path,
    driver: Optional[ta.LifecycleDriver],
    current_uid: int,
    health_timeout: float = 10.0,
) -> Dict[str, Any]:
    """Execute rollback under existing ActivationLock."""
    if act_journal["state"] == STATE_ROLLED_BACK:
        tx_res = tx.rollback(namespace_root=ns_root)
        return {"state": STATE_ROLLED_BACK, "status": "idempotent_success", "file_status": tx_res}

    if driver is None:
        rec_dir = tx_dir / "recovery_lifecycle"
        if rec_dir.exists() and (rec_dir / "server_supervisor.py").exists():
            validate_recovery_assets(rec_dir, act_journal.get("recovery_assets", {}), current_uid)
            driver = ta.load_isolated_lifecycle(
                recovery_dir=rec_dir,
                namespace_root=ns_root,
                expected_hashes=act_journal.get("recovery_assets"),
            )
        else:
            driver = ta.TermuxLifecycleDriver(namespace_root=ns_root)

    # Genuinely interrupted pre-snapshot PREPARED transaction:
    if act_journal["state"] == STATE_PREPARED and not act_journal.get("flags"):
        tx_res = tx.rollback(namespace_root=ns_root)
        act_journal["state"] = STATE_ROLLED_BACK
        save_activation_journal(tx_dir, act_journal)
        return {"state": STATE_ROLLED_BACK, "status": "rolled_back_prepared_presnapshot", "file_status": tx_res}

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
        health_timeout=health_timeout,
    )

    return {"state": STATE_ROLLED_BACK, "status": "rolled_back"}


def rollback(
    transaction_dir: Path,
    namespace_root: Optional[Path] = None,
    driver: Optional[ta.LifecycleDriver] = None,
) -> Dict[str, Any]:
    """Caller-initiated rollback of prepared, applied, or committed activation."""
    tx_dir = Path(transaction_dir).resolve()
    act_pre = load_activation_journal(tx_dir)
    ns_root = Path(namespace_root).resolve() if namespace_root else Path(act_pre["namespace_root"]).resolve()

    with ActivationLock(ns_root):
        current_uid = os.getuid()
        act_journal = load_activation_journal(tx_dir)
        tx = rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()
        validate_activation_journal(act_journal, tx_journal, ns_root, tx_dir, current_uid)

        return _rollback_locked(
            tx_dir=tx_dir,
            act_journal=act_journal,
            tx=tx,
            tx_journal=tx_journal,
            ns_root=ns_root,
            driver=driver,
            current_uid=current_uid,
        )


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
    act_pre = load_activation_journal(tx_dir)
    ns_root = Path(namespace_root).resolve() if namespace_root else Path(act_pre["namespace_root"]).resolve()

    with ActivationLock(ns_root):
        current_uid = os.getuid()
        act_journal = load_activation_journal(tx_dir)
        tx = rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()
        validate_activation_journal(act_journal, tx_journal, ns_root, tx_dir, current_uid)

        if act_journal["state"] == STATE_COMPLETED and tx_journal.get("state") == rt.STATE_COMMITTED:
            tx_res = tx.recover(namespace_root=ns_root)
            return {"state": STATE_COMPLETED, "status": "idempotent_success", "file_status": tx_res}

        return _rollback_locked(
            tx_dir=tx_dir,
            act_journal=act_journal,
            tx=tx,
            tx_journal=tx_journal,
            ns_root=ns_root,
            driver=driver,
            current_uid=current_uid,
        )


def status(transaction_dir: Path) -> Dict[str, Any]:
    """Inspect activation status."""
    tx_dir = Path(transaction_dir).resolve()
    act_journal = load_activation_journal(tx_dir)
    profile = act_journal.get("profile", PROFILE_GENERAL)
    tx = rt.ReleaseTransaction(tx_dir)
    tx_status = tx.status()
    return {
        "activation_state": act_journal.get("state"),
        "profile": profile,
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

    plan_parser = subparsers.add_parser("plan", help="Dry planning of affected destinations and services")
    plan_parser.add_argument("--bundle", required=True, help="Candidate bundle directory")
    plan_parser.add_argument("--preconditions", required=True, help="Preconditions JSON file")
    plan_parser.add_argument("--namespace-root", help="Live namespace root directory")
    plan_parser.add_argument("--profile", choices=list(VALID_PROFILES), default=PROFILE_GENERAL, help="Release profile")

    apply_parser = subparsers.add_parser("apply", help="Execute service-scoped release activation")
    apply_parser.add_argument("--bundle", required=True, help="Candidate bundle directory")
    apply_parser.add_argument("--preconditions", required=True, help="Preconditions JSON file")
    apply_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    apply_parser.add_argument("--namespace-root", help="Live namespace root directory")
    apply_parser.add_argument("--profile", choices=list(VALID_PROFILES), default=PROFILE_GENERAL, help="Release profile")

    rb_parser = subparsers.add_parser("rollback", help="Roll back activation")
    rb_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    rb_parser.add_argument("--namespace-root", help="Live namespace root directory")

    rec_parser = subparsers.add_parser("recover", help="Recover interrupted activation")
    rec_parser.add_argument("--transaction-dir", required=True, help="Transaction directory")
    rec_parser.add_argument("--namespace-root", help="Live namespace root directory")

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
                profile=args.profile,
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
                profile=args.profile,
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
