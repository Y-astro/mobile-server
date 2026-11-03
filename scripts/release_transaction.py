#!/usr/bin/env python3
"""
release_transaction.py: Standalone stdlib file transaction engine and recovery.

Implements a durable, atomic, and reversible file release transaction core for
Termux-hosted deployments. Enforces strict precondition validation (bytes,
SHA-256, modes, UIDs, and new-path absence), exclusive namespace locking,
atomic sibling file replacements, write-intent journaling, automatic rollback
on ordinary apply failure, and independent bundle-free recovery for interrupted
transactions.

Usage:
  python3 scripts/release_transaction.py status --transaction-dir <DIR>
  python3 scripts/release_transaction.py recover --transaction-dir <DIR> [--namespace-root <DIR>]
"""

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid
import fcntl

# Constants
TERMUX_ROOT_PREFIX = "/data/data/com.termux/files/"
KNOWN_BOOT_ALIAS_REMOTE = "/data/data/com.termux/files/home/start_services.sh"
KNOWN_BOOT_TARGET_REMOTE = "/data/data/com.termux/files/home/.termux/boot/start-services.sh"
AUDITED_BOOT_SCRIPT_SHA256 = "90dd7aacc85f3289ed53e35752b4dc804d48c92abd29136f4cc81b63f89d0bcc"
AUDITED_BOOT_SCRIPT_BYTES = 316
AUDITED_BOOT_SCRIPT_MODE = 0o700

JOURNAL_VERSION = 1

# Transaction States
STATE_PREPARED = "PREPARED"
STATE_APPLYING = "APPLYING"
STATE_APPLIED = "APPLIED"
STATE_COMMITTED = "COMMITTED"
STATE_ROLLING_BACK = "ROLLING_BACK"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

# Entry Actions
ACTION_CREATE = "CREATE"
ACTION_REPLACE = "REPLACE"
ACTION_UNTOUCHED = "UNTOUCHED"

# Entry Progress Status
STATUS_PENDING = "PENDING"
STATUS_INTENT_WRITE = "INTENT_WRITE"
STATUS_WRITTEN = "WRITTEN"
STATUS_UNTOUCHED = "UNTOUCHED"
STATUS_ROLLED_BACK = "ROLLED_BACK"

# Forbidden secret, runtime, credential, database, and history patterns
FORBIDDEN_SECRET_PATTERNS: Tuple[str, ...] = (
    "users.json",
    "*users.json*",
    "*.env",
    "*.env.*",
    ".env",
    "*.key",
    "*.pem",
    "*.crt",
    "*.cert",
    "*.pfx",
    "*.p12",
    "*id_rsa*",
    "*id_ed25519*",
    "*session.key*",
    "*authorized_keys*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*filebrowser.db*",
    "*.log",
    "*history*",
    "*.bak",
    "*.swp",
    "*services_disabled*",
    "*.phone-server-paused*",
    "*server_recovery*",
)


class TransactionError(Exception):
    """Base exception for release transaction errors."""
    pass


class PreconditionError(TransactionError):
    """Raised when preconditions are invalid, missing, or live state has drifted."""
    pass


class TransactionLockError(TransactionError):
    """Raised when namespace lock cannot be acquired."""
    pass


class ApplyError(TransactionError):
    """Raised when apply fails."""
    pass


class RollbackError(TransactionError):
    """Raised when rollback fails."""
    pass


class RecoveryError(TransactionError):
    """Raised when recovery fails."""
    pass


def path_lexists(path: Union[str, Path]) -> bool:
    """Check if path exists on filesystem without following symlinks."""
    return os.path.lexists(str(path))


def is_forbidden_secret_or_runtime_path(path_str: str) -> bool:
    """Check whether a path matches forbidden secret, runtime, DB, key, or credential patterns."""
    normalized = path_str.replace("\\", "/").lower()
    basename = os.path.basename(normalized)
    parts = normalized.split("/")

    for pattern in FORBIDDEN_SECRET_PATTERNS:
        pat_lower = pattern.lower()
        if fnmatch.fnmatch(basename, pat_lower) or fnmatch.fnmatch(normalized, pat_lower):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat_lower):
                return True

    for keyword in ("token", "credential", "private_key", "secret_key", "authorized_keys"):
        if keyword in basename or keyword in normalized:
            return True

    return False


def normalize_mode(mode: Any) -> int:
    """Normalize permission mode representation to standard Unix bitmask (e.g. 0o700)."""
    if isinstance(mode, int):
        return mode & 0o7777
    if isinstance(mode, str):
        s = mode.strip()
        if s.startswith("0o") or s.startswith("0O"):
            return int(s, 8) & 0o7777
        elif s.startswith("0") and len(s) > 1 and all(c in "01234567" for c in s):
            return int(s, 8) & 0o7777
        elif all(c in "01234567" for c in s):
            return int(s, 8) & 0o7777
        else:
            return int(s) & 0o7777
    raise PreconditionError(f"Invalid mode representation: {mode!r}")


def mode_to_octal_str(mode: int) -> str:
    """Format mode integer as standard octal string (e.g. '0o700')."""
    return oct(mode & 0o7777)


def compute_file_sha256(file_path: Path) -> Tuple[str, int]:
    """Deterministic SHA-256 and byte length using 64KB chunked reads."""
    if not file_path.exists():
        raise PreconditionError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise PreconditionError(f"Path is not a regular file: {file_path}")

    hasher = hashlib.sha256()
    size = 0
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)

    return hasher.hexdigest(), size


def fsync_dir(path: Path) -> None:
    """Synchronize directory metadata to storage."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_atomic_json(path: Path, data: Any) -> None:
    """Atomically write JSON data with sibling replacement and fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp_{path.name}_{os.getpid()}_{uuid.uuid4().hex}"
    payload = json.dumps(data, indent=2) + "\n"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    fsync_dir(path.parent)


def get_canonical_target_mode(remote_path: str) -> int:
    """
    Derive canonical owned mode from destination path:
    - dashboard executable: 0700
    - Python helper modules: 0600
    - gateway: 0700
    - ordinary scripts: 0700
    - agy-runner-loop: 0755
    - agy_session.sh / termux_session.sh / start_suwayomi.sh: 0755
    - web/config: 0600 (termux_index.html: 0644)
    """
    norm = remote_path.replace("\\", "/")
    if norm.endswith("/usr/bin/dashboard"):
        return 0o700
    if "/home/.local/lib/mobile-server/" in norm:
        return 0o600
    if norm.endswith("/home/bin/mobile-gateway"):
        return 0o700
    if norm.endswith("/home/bin/agy-runner-loop") or norm.endswith("/usr/local/bin/agy-runner-loop"):
        return 0o755
    if norm.endswith("/home/bin/agy_session.sh") or norm.endswith("/home/bin/termux_session.sh"):
        return 0o755
    if "/home/bin/start_suwayomi" in norm or "/home/bin/suwayomi_" in norm:
        return 0o755
    if "/usr/bin/" in norm or "/home/bin/" in norm or norm.endswith("/start-services.sh") or norm.endswith("/start_services.sh"):
        return 0o700
    if norm.endswith("termux_index.html"):
        return 0o644
    if "/.config/" in norm or "/web/" in norm or "/static/" in norm:
        return 0o600
    return 0o600


def is_entrypoint(remote_path: str) -> bool:
    """Identify entrypoint executables that must be installed after helpers/assets."""
    norm = remote_path.replace("\\", "/")
    if "/usr/bin/" in norm or "/home/bin/" in norm:
        return True
    if norm.endswith("/start-services.sh") or norm.endswith("/start_services.sh"):
        return True
    if "/usr/local/bin/" in norm:
        return True
    return False


def resolve_disk_path(remote_path: str, namespace_root: Path) -> Tuple[Path, Optional[Path], bool]:
    """
    Resolve remote path under namespace_root.
    Returns: (target_disk_path, alias_disk_path_or_None, is_alias)
    Enforces path safety, parent symlink escape rejection, and the known boot alias.
    """
    if not isinstance(remote_path, str) or not remote_path.strip():
        raise PreconditionError(f"Remote path must be a non-empty string: {remote_path!r}")

    norm_remote = os.path.normpath(remote_path)
    if not norm_remote.startswith(TERMUX_ROOT_PREFIX):
        raise PreconditionError(
            f"Remote destination {remote_path} is outside permitted prefix {TERMUX_ROOT_PREFIX}"
        )

    if is_forbidden_secret_or_runtime_path(norm_remote):
        raise PreconditionError(f"Refused forbidden secret or runtime path: {remote_path}")

    rel_str = norm_remote[len(TERMUX_ROOT_PREFIX):].lstrip("/")
    parts = Path(rel_str).parts
    if not parts or ".." in parts:
        raise PreconditionError(f"Refused traversal in remote destination: {remote_path}")

    ns_resolved = namespace_root.resolve()

    # Verify no parent directory component between namespace_root and target is a symlink
    curr = namespace_root
    for part in parts[:-1]:
        curr = curr / part
        if curr.is_symlink():
            raise PreconditionError(
                f"Refused symlink in parent directory component '{part}' for {remote_path}"
            )
        if curr.exists():
            try:
                curr_res = curr.resolve()
                if not curr_res.is_relative_to(ns_resolved):
                    raise PreconditionError(
                        f"Parent directory component '{curr}' escapes namespace root"
                    )
            except (ValueError, RuntimeError) as exc:
                raise PreconditionError(f"Invalid parent directory resolution: {exc}") from exc

    disk_path = namespace_root / rel_str

    # Known boot alias exception
    if norm_remote == KNOWN_BOOT_ALIAS_REMOTE:
        boot_target_rel = KNOWN_BOOT_TARGET_REMOTE[len(TERMUX_ROOT_PREFIX):].lstrip("/")
        boot_target_disk = namespace_root / boot_target_rel

        # Verify parent directory containment of boot target
        boot_curr = namespace_root
        for part in Path(boot_target_rel).parts[:-1]:
            boot_curr = boot_curr / part
            if boot_curr.is_symlink():
                raise PreconditionError(
                    f"Refused symlink in parent directory component '{part}' for boot target"
                )

        if path_lexists(disk_path):
            if not disk_path.is_symlink():
                raise PreconditionError(
                    f"Expected known boot alias {remote_path} to be a symlink, but found non-symlink"
                )
            link_target = os.readlink(disk_path)
            if link_target.startswith(TERMUX_ROOT_PREFIX):
                target_rel = link_target[len(TERMUX_ROOT_PREFIX):].lstrip("/")
                resolved_link = (namespace_root / target_rel).resolve()
            elif os.path.isabs(link_target):
                resolved_link = Path(link_target).resolve()
            else:
                resolved_link = (disk_path.parent / link_target).resolve()

            if not resolved_link.is_relative_to(ns_resolved):
                raise PreconditionError(
                    f"Known boot alias {remote_path} target escapes namespace root: {link_target}"
                )
            if resolved_link != boot_target_disk.resolve():
                raise PreconditionError(
                    f"Known boot alias {remote_path} points to unexpected target: {link_target} "
                    f"(expected {boot_target_disk})"
                )

        return boot_target_disk, disk_path, True

    # Any non-alias destination: if it exists, it must NOT be a symlink
    if disk_path.is_symlink():
        raise PreconditionError(
            f"Arbitrary symlinks are forbidden: {remote_path} at {disk_path} is an unauthorized symlink"
        )

    return disk_path, None, False


class NamespaceLock:
    """Exclusive flock lock on namespace root to serialize file operations."""

    def __init__(self, namespace_root: Path):
        self.namespace_root = namespace_root
        self.lock_path = namespace_root / ".release_transaction.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise TransactionLockError(f"Could not open lock file {self.lock_path}: {exc}") from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise TransactionLockError(
                f"Namespace lock held by another process: {self.lock_path}"
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


def _load_release_manifest_module() -> Any:
    """Dynamically load reviewed release-manifest.py from sibling directory."""
    current_dir = Path(__file__).resolve().parent
    manifest_script = current_dir / "release-manifest.py"
    if not manifest_script.exists():
        raise RuntimeError(f"Sibling release-manifest.py not found at {manifest_script}")
    spec = importlib.util.spec_from_file_location("release_manifest", manifest_script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec for {manifest_script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_preconditions(
    preconditions_input: Union[str, Path, Dict[str, Any], List[Dict[str, Any]]]
) -> Tuple[Optional[int], Dict[str, Dict[str, Any]]]:
    """
    Parse reviewed preconditions.
    Returns: (top_level_uid_or_None, map_of_remote_path_to_precondition_dict)
    Detects and rejects duplicate entries.
    """
    data = preconditions_input
    if isinstance(data, (str, Path)):
        p = Path(data)
        if not p.exists():
            raise PreconditionError(f"Preconditions file not found: {p}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise PreconditionError(f"Failed to read preconditions from {p}: {exc}") from exc

    top_uid: Optional[int] = None
    files_list: List[Dict[str, Any]] = []

    if isinstance(data, dict):
        if "uid" in data:
            top_uid = data["uid"]
        if "files" in data and isinstance(data["files"], list):
            files_list = data["files"]
        else:
            # Could be a direct map of remote_path -> dict
            files_list = []
            for path_k, val in data.items():
                if isinstance(val, dict):
                    entry = dict(val)
                    entry.setdefault("path", path_k)
                    files_list.append(entry)

    elif isinstance(data, list):
        files_list = data
    else:
        raise PreconditionError(f"Invalid preconditions structure type: {type(data).__name__}")

    precond_map: Dict[str, Dict[str, Any]] = {}
    for idx, item in enumerate(files_list):
        if not isinstance(item, dict):
            raise PreconditionError(f"Precondition item at index {idx} is not an object: {item}")
        path_str = item.get("path")
        if not path_str or not isinstance(path_str, str):
            raise PreconditionError(f"Precondition item at index {idx} missing valid 'path': {item}")

        norm_path = os.path.normpath(path_str)
        if norm_path in precond_map:
            raise PreconditionError(f"Duplicate precondition declared for destination: '{norm_path}'")
        precond_map[norm_path] = item

    return top_uid, precond_map


def verify_live_preconditions(
    manifest_entries: List[Dict[str, Any]],
    precond_map: Dict[str, Dict[str, Any]],
    top_uid: Optional[int],
    namespace_root: Path,
    strict_match_count: bool = True,
) -> None:
    """
    Validate that live files strictly match reviewed preconditions.
    Rejects missing, extra, wrong UID, mode drift, hash drift, and absent-path presence.
    """
    manifest_remotes = set(os.path.normpath(e["remote"]) for e in manifest_entries)
    precond_remotes = set(precond_map.keys())

    if strict_match_count:
        missing_in_precond = manifest_remotes - precond_remotes
        if missing_in_precond:
            raise PreconditionError(
                f"Candidate manifest contains destinations missing from reviewed preconditions: "
                f"{', '.join(sorted(missing_in_precond))}"
            )
        extra_in_precond = precond_remotes - manifest_remotes
        if extra_in_precond:
            raise PreconditionError(
                f"Reviewed preconditions declare destinations not in candidate manifest: "
                f"{', '.join(sorted(extra_in_precond))}"
            )

    for entry in manifest_entries:
        remote_path = os.path.normpath(entry["remote"])
        precond = precond_map.get(remote_path)
        if not precond:
            raise PreconditionError(f"Missing precondition for candidate destination: {remote_path}")

        target_disk, alias_disk, is_alias = resolve_disk_path(remote_path, namespace_root)

        kind = precond.get("kind", "file")
        expected_uid = precond.get("uid", top_uid)

        if kind == "absent":
            if path_lexists(target_disk):
                raise PreconditionError(
                    f"Precondition drift: path expected to be absent currently exists on disk: {remote_path} (at {target_disk})"
                )
            continue

        if kind == "symlink":
            if not is_alias:
                raise PreconditionError(
                    f"Unauthorized symlink precondition for {remote_path}: only the known boot alias is permitted"
                )
            if alias_disk is None or not alias_disk.is_symlink():
                raise PreconditionError(
                    f"Precondition drift: boot alias {remote_path} must be a symlink on disk"
                )

            # Check alias lstat UID
            alias_st = alias_disk.lstat()
            if expected_uid is not None and alias_st.st_uid != expected_uid:
                raise PreconditionError(
                    f"UID mismatch on boot alias symlink {remote_path}: expected UID {expected_uid}, live has {alias_st.st_uid}"
                )

            # Check target file
            if not target_disk.exists() or target_disk.is_symlink():
                raise PreconditionError(
                    f"Boot target {target_disk} for alias {remote_path} must exist as a regular file"
                )

            target_st = target_disk.stat()
            if expected_uid is not None and target_st.st_uid != expected_uid:
                raise PreconditionError(
                    f"UID mismatch on boot target {target_disk}: expected UID {expected_uid}, live has {target_st.st_uid}"
                )

            exp_target_mode = normalize_mode(precond.get("target_mode", AUDITED_BOOT_SCRIPT_MODE))
            live_target_mode = stat.S_IMODE(target_st.st_mode)
            if live_target_mode != exp_target_mode:
                raise PreconditionError(
                    f"Mode drift on boot target {target_disk}: expected {mode_to_octal_str(exp_target_mode)}, "
                    f"live has {mode_to_octal_str(live_target_mode)}"
                )

            exp_target_bytes = precond.get("target_bytes", AUDITED_BOOT_SCRIPT_BYTES)
            if target_st.st_size != exp_target_bytes:
                raise PreconditionError(
                    f"Size drift on boot target {target_disk}: expected {exp_target_bytes} bytes, "
                    f"live has {target_st.st_size} bytes"
                )

            exp_target_sha256 = precond.get("target_sha256", AUDITED_BOOT_SCRIPT_SHA256).lower()
            live_sha256, _ = compute_file_sha256(target_disk)
            if live_sha256.lower() != exp_target_sha256:
                raise PreconditionError(
                    f"Hash drift on boot target {target_disk}: expected {exp_target_sha256}, "
                    f"live has {live_sha256}"
                )
            continue

        if kind == "file":
            if not path_lexists(target_disk):
                raise PreconditionError(
                    f"Precondition drift: required live file missing on disk: {remote_path} (expected at {target_disk})"
                )
            if target_disk.is_symlink() or not target_disk.is_file():
                raise PreconditionError(
                    f"Precondition drift: expected regular file for {remote_path}, found symlink or directory"
                )

            st = target_disk.stat()
            if expected_uid is not None and st.st_uid != expected_uid:
                raise PreconditionError(
                    f"UID mismatch for {remote_path}: expected UID {expected_uid}, live file has {st.st_uid}"
                )

            if "mode" in precond:
                exp_mode = normalize_mode(precond["mode"])
                live_mode = stat.S_IMODE(st.st_mode)
                if live_mode != exp_mode:
                    raise PreconditionError(
                        f"Mode drift for {remote_path}: expected {mode_to_octal_str(exp_mode)}, "
                        f"live file has {mode_to_octal_str(live_mode)}"
                    )

            if "bytes" in precond:
                exp_bytes = precond["bytes"]
                if st.st_size != exp_bytes:
                    raise PreconditionError(
                        f"Byte size drift for {remote_path}: expected {exp_bytes} bytes, "
                        f"live file has {st.st_size} bytes"
                    )

            if "sha256" in precond:
                exp_sha256 = precond["sha256"].lower()
                live_sha256, _ = compute_file_sha256(target_disk)
                if live_sha256.lower() != exp_sha256:
                    raise PreconditionError(
                        f"SHA-256 hash drift for {remote_path}: expected {exp_sha256}, "
                        f"live file has {live_sha256}"
                    )
            continue

        raise PreconditionError(f"Unsupported precondition kind '{kind}' for {remote_path}")


class ReleaseTransaction:
    """Manages file transaction execution, validation, rollback, and recovery."""

    def __init__(self, transaction_dir: Union[str, Path]):
        self.transaction_dir = Path(transaction_dir).resolve()
        self.journal_path = self.transaction_dir / "journal.json"
        self.staged_dir = self.transaction_dir / "staged"
        self.backups_dir = self.transaction_dir / "backups"

    def load_journal(self) -> Dict[str, Any]:
        if not self.journal_path.exists():
            raise TransactionError(f"Journal not found at {self.journal_path}")
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise TransactionError(f"Failed to read journal {self.journal_path}: {exc}") from exc

    def save_journal(self, data: Dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        write_atomic_json(self.journal_path, data)

    def status(self) -> Dict[str, Any]:
        """Return status summary of the transaction."""
        journal = self.load_journal()
        entries = journal.get("entries", [])
        return {
            "state": journal.get("state"),
            "version": journal.get("version"),
            "namespace_root": journal.get("namespace_root"),
            "created_at": journal.get("created_at"),
            "updated_at": journal.get("updated_at"),
            "total_entries": len(entries),
            "applied_count": sum(1 for e in entries if e.get("status") == STATUS_WRITTEN),
            "untouched_count": sum(1 for e in entries if e.get("status") == STATUS_UNTOUCHED),
            "rolled_back_count": sum(1 for e in entries if e.get("status") == STATUS_ROLLED_BACK),
            "error": journal.get("error"),
        }

    def apply(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Apply transaction to live namespace root."""
        journal = self.load_journal()
        curr_state = journal.get("state")

        # Idempotent checks
        if curr_state in (STATE_COMMITTED, STATE_APPLIED):
            return {"state": curr_state, "status": "idempotent_success"}

        if curr_state != STATE_PREPARED:
            raise ApplyError(f"Cannot apply transaction in state {curr_state}")

        ns_root_str = namespace_root if namespace_root is not None else journal.get("namespace_root")
        if not ns_root_str:
            raise ApplyError("Namespace root not defined in journal or arguments")
        ns_root = Path(ns_root_str).resolve()

        with NamespaceLock(ns_root):
            # Recheck live preconditions immediately before apply
            precond_map = journal.get("preconditions", {})
            top_uid = journal.get("top_uid")
            candidate_manifest = journal.get("candidate_manifest", [])
            verify_live_preconditions(
                manifest_entries=candidate_manifest,
                precond_map=precond_map,
                top_uid=top_uid,
                namespace_root=ns_root,
                strict_match_count=True,
            )

            # Verify integrity of all staged files before writes
            entries = journal.get("entries", [])
            for entry in entries:
                if entry["action"] != ACTION_UNTOUCHED:
                    staged_rel = entry["staged_file"]
                    staged_path = self.transaction_dir / staged_rel
                    if not staged_path.exists():
                        raise ApplyError(f"Staged file missing for {entry['remote']}: {staged_path}")
                    staged_sha, staged_bytes = compute_file_sha256(staged_path)
                    if staged_bytes != entry["candidate_bytes"] or staged_sha.lower() != entry["candidate_sha256"].lower():
                        raise ApplyError(f"Staged file tampering detected for {entry['remote']}")

            # Verify integrity of all backups before writes
            for entry in entries:
                if entry["action"] == ACTION_REPLACE:
                    backup_rel = entry.get("backup_file")
                    if not backup_rel:
                        raise ApplyError(f"Missing backup record for replaced file {entry['remote']}")
                    backup_path = self.transaction_dir / backup_rel
                    if not backup_path.exists():
                        raise ApplyError(f"Backup file missing for {entry['remote']}: {backup_path}")
                    bk_sha, bk_bytes = compute_file_sha256(backup_path)
                    if bk_bytes != entry["original_bytes"] or bk_sha.lower() != entry["original_sha256"].lower():
                        raise ApplyError(f"Backup file tampering detected for {entry['remote']}")

            # Transition to APPLYING
            journal["state"] = STATE_APPLYING
            self.save_journal(journal)

            touched_entries: List[Dict[str, Any]] = []

            try:
                for entry in entries:
                    if entry["action"] == ACTION_UNTOUCHED:
                        entry["status"] = STATUS_UNTOUCHED
                        continue

                    # Record write intent before live modification
                    entry["status"] = STATUS_INTENT_WRITE
                    self.save_journal(journal)
                    touched_entries.append(entry)

                    # Destination on disk
                    target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], ns_root)
                    target_disk.parent.mkdir(parents=True, exist_ok=True)

                    staged_path = self.transaction_dir / entry["staged_file"]
                    with open(staged_path, "rb") as sf:
                        staged_bytes_data = sf.read()

                    # Atomic sibling replacement
                    tmp_sibling = target_disk.parent / f".tmp_{target_disk.name}_{os.getpid()}_{uuid.uuid4().hex}"
                    target_mode = normalize_mode(entry["target_mode"])
                    fd = os.open(str(tmp_sibling), os.O_WRONLY | os.O_CREAT | os.O_EXCL, target_mode)
                    try:
                        os.write(fd, staged_bytes_data)
                        os.chmod(fd, target_mode)
                        os.fsync(fd)
                    finally:
                        os.close(fd)

                    os.replace(tmp_sibling, target_disk)
                    fsync_dir(target_disk.parent)

                    # Verify candidate bytes and mode on disk after replacement
                    target_st = target_disk.stat()
                    live_mode = stat.S_IMODE(target_st.st_mode)
                    if live_mode != target_mode:
                        raise ApplyError(
                            f"Post-apply mode verification failed for {entry['remote']}: "
                            f"expected {mode_to_octal_str(target_mode)}, got {mode_to_octal_str(live_mode)}"
                        )
                    if target_st.st_size != entry["candidate_bytes"]:
                        raise ApplyError(
                            f"Post-apply byte count verification failed for {entry['remote']}: "
                            f"expected {entry['candidate_bytes']}, got {target_st.st_size}"
                        )
                    live_sha, _ = compute_file_sha256(target_disk)
                    if live_sha.lower() != entry["candidate_sha256"].lower():
                        raise ApplyError(
                            f"Post-apply SHA-256 verification failed for {entry['remote']}: "
                            f"expected {entry['candidate_sha256']}, got {live_sha}"
                        )

                    entry["status"] = STATUS_WRITTEN
                    self.save_journal(journal)

                # All entries successfully replaced
                journal["state"] = STATE_APPLIED
                self.save_journal(journal)
                return {"state": STATE_APPLIED, "applied_count": len(touched_entries)}

            except Exception as apply_exc:
                # Injected or unexpected failure: trigger automatic rollback of touched entries
                rollback_success = False
                rb_error: Optional[Exception] = None
                try:
                    self._perform_rollback(journal, ns_root, touched_only=True)
                    journal["state"] = STATE_ROLLED_BACK
                    self.save_journal(journal)
                    rollback_success = True
                except Exception as exc:
                    rb_error = exc
                    journal["state"] = STATE_RECOVERY_REQUIRED
                    journal["error"] = f"Apply error: {apply_exc}; Rollback error: {rb_error}"
                    self.save_journal(journal)

                if rollback_success:
                    raise ApplyError(
                        f"Apply failed and all touched files rolled back cleanly: {apply_exc}"
                    ) from apply_exc
                else:
                    raise RollbackError(
                        f"Apply failed ({apply_exc}) and rollback also failed: {rb_error}"
                    ) from rb_error

    def commit(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Commit applied transaction."""
        journal = self.load_journal()
        curr_state = journal.get("state")

        if curr_state == STATE_COMMITTED:
            return {"state": STATE_COMMITTED, "status": "idempotent_success"}

        if curr_state != STATE_APPLIED:
            raise TransactionError(f"Cannot commit transaction in state {curr_state} (must be APPLIED)")

        journal["state"] = STATE_COMMITTED
        self.save_journal(journal)
        return {"state": STATE_COMMITTED, "status": "committed"}

    def rollback(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Explicitly rollback prepared or applied transaction."""
        return self.recover(namespace_root=namespace_root)

    def recover(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """
        Independent bundle-free recovery of an interrupted, applying, or applied transaction.
        Validates all backups and current file states before modifying any live file.
        Refuses unknown concurrent edits.
        """
        journal = self.load_journal()
        curr_state = journal.get("state")

        if curr_state == STATE_ROLLED_BACK:
            return {"state": STATE_ROLLED_BACK, "status": "idempotent_success"}

        if curr_state == STATE_COMMITTED:
            return {"state": STATE_COMMITTED, "status": "idempotent_success"}

        ns_root_str = namespace_root if namespace_root is not None else journal.get("namespace_root")
        if not ns_root_str:
            raise RecoveryError("Namespace root not defined in journal or arguments")
        ns_root = Path(ns_root_str).resolve()

        with NamespaceLock(ns_root):
            journal["state"] = STATE_ROLLING_BACK
            self.save_journal(journal)

            self._perform_rollback(journal, ns_root, touched_only=False)

            journal["state"] = STATE_ROLLED_BACK
            self.save_journal(journal)
            return {"state": STATE_ROLLED_BACK, "status": "recovered"}

    def _perform_rollback(
        self,
        journal: Dict[str, Any],
        namespace_root: Path,
        touched_only: bool = False,
    ) -> None:
        """
        Internal rollback worker.
        1. Validate all backups.
        2. Validate live files against expected candidate or original states; refuse unknown edits.
        3. Restore touched files in reverse order (entrypoints first, helpers/assets last).
        """
        entries = journal.get("entries", [])

        # 1. Validate all backups
        for entry in entries:
            if entry["action"] == ACTION_REPLACE:
                backup_rel = entry.get("backup_file")
                if not backup_rel:
                    raise RecoveryError(f"Missing backup specification for {entry['remote']}")
                backup_path = self.transaction_dir / backup_rel
                if not backup_path.exists():
                    raise RecoveryError(f"Corrupted or missing backup file for {entry['remote']}: {backup_path}")
                bk_sha, bk_bytes = compute_file_sha256(backup_path)
                if bk_bytes != entry["original_bytes"] or bk_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Corrupted backup checksum for {entry['remote']}")

        # 2. Validate live file states: must match candidate state or original state
        for entry in entries:
            target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)

            if is_alias:
                # Ensure alias symlink itself is still intact
                if alias_disk is None or not alias_disk.is_symlink():
                    raise RecoveryError(f"Boot alias {entry['remote']} is not a symlink; cannot safely recover")

            if entry["action"] == ACTION_UNTOUCHED:
                if not target_disk.exists():
                    raise RecoveryError(f"Untouched file disappeared: {entry['remote']}")
                cur_sha, cur_bytes = compute_file_sha256(target_disk)
                if cur_bytes != entry["original_bytes"] or cur_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Unknown concurrent edit on untouched file: {entry['remote']}")
                continue

            if entry["action"] == ACTION_CREATE:
                if path_lexists(target_disk):
                    cur_sha, cur_bytes = compute_file_sha256(target_disk)
                    if cur_bytes != entry["candidate_bytes"] or cur_sha.lower() != entry["candidate_sha256"].lower():
                        raise RecoveryError(
                            f"Unknown concurrent edit on new file {entry['remote']}: "
                            "content matches neither absence nor candidate"
                        )
                continue

            if entry["action"] == ACTION_REPLACE:
                if not path_lexists(target_disk):
                    raise RecoveryError(f"Live file missing during recovery: {entry['remote']}")
                cur_sha, cur_bytes = compute_file_sha256(target_disk)
                matches_candidate = (
                    cur_bytes == entry["candidate_bytes"]
                    and cur_sha.lower() == entry["candidate_sha256"].lower()
                )
                matches_original = (
                    cur_bytes == entry["original_bytes"]
                    and cur_sha.lower() == entry["original_sha256"].lower()
                )
                if not matches_candidate and not matches_original:
                    raise RecoveryError(
                        f"Unknown concurrent edit on {entry['remote']}: "
                        f"current hash {cur_sha} matches neither candidate nor original preimage"
                    )

        # 3. Restore files in reverse entrypoint order
        for entry in reversed(entries):
            if entry["action"] == ACTION_UNTOUCHED:
                continue

            if touched_only and entry.get("status") not in (STATUS_INTENT_WRITE, STATUS_WRITTEN):
                continue

            target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)

            if entry["action"] == ACTION_CREATE:
                # Return newly created file to absence
                if path_lexists(target_disk):
                    try:
                        os.unlink(target_disk)
                        fsync_dir(target_disk.parent)
                    except OSError as exc:
                        raise RecoveryError(f"Failed to remove new file {target_disk} during rollback: {exc}") from exc
                entry["status"] = STATUS_ROLLED_BACK
                continue

            if entry["action"] == ACTION_REPLACE:
                orig_mode = normalize_mode(entry["original_mode"])
                if path_lexists(target_disk):
                    cur_sha, cur_bytes = compute_file_sha256(target_disk)
                    cur_mode = stat.S_IMODE(target_disk.stat().st_mode)
                    if (
                        cur_bytes == entry["original_bytes"]
                        and cur_sha.lower() == entry["original_sha256"].lower()
                        and cur_mode == orig_mode
                    ):
                        entry["status"] = STATUS_ROLLED_BACK
                        continue

                backup_path = self.transaction_dir / entry["backup_file"]
                with open(backup_path, "rb") as bf:
                    backup_bytes = bf.read()

                orig_mode = normalize_mode(entry["original_mode"])
                tmp_sibling = target_disk.parent / f".tmp_{target_disk.name}_rb_{os.getpid()}_{uuid.uuid4().hex}"
                fd = os.open(str(tmp_sibling), os.O_WRONLY | os.O_CREAT | os.O_EXCL, orig_mode)
                try:
                    os.write(fd, backup_bytes)
                    os.chmod(fd, orig_mode)
                    os.fsync(fd)
                finally:
                    os.close(fd)

                os.replace(tmp_sibling, target_disk)
                fsync_dir(target_disk.parent)

                # Verify restored bytes, mode, and hash
                restored_st = target_disk.stat()
                live_mode = stat.S_IMODE(restored_st.st_mode)
                if live_mode != orig_mode:
                    raise RecoveryError(f"Restored mode mismatch for {entry['remote']}")
                if restored_st.st_size != entry["original_bytes"]:
                    raise RecoveryError(f"Restored byte count mismatch for {entry['remote']}")
                restored_sha, _ = compute_file_sha256(target_disk)
                if restored_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Restored SHA-256 mismatch for {entry['remote']}")

                entry["status"] = STATUS_ROLLED_BACK


def prepare(
    bundle_root: Union[str, Path],
    candidate_manifest: Union[str, Path, List[Dict[str, Any]]],
    preconditions: Union[str, Path, Dict[str, Any], List[Dict[str, Any]]],
    transaction_dir: Union[str, Path],
    namespace_root: Union[str, Path],
) -> ReleaseTransaction:
    """
    Prepare a release transaction.
    1. Validates candidate bundle with sibling release-manifest.py (requires gateway).
    2. Parses and verifies caller-provided reviewed preconditions against live disk.
    3. Stages changed candidate files and creates live backups.
    4. Saves standalone recovery copy of this script in transaction directory.
    5. Writes initial PREPARED atomic journal.
    """
    b_root = Path(bundle_root).resolve()
    tx_dir = Path(transaction_dir).resolve()
    ns_root = Path(namespace_root).resolve()

    # Load and validate candidate manifest
    manifest_entries: List[Dict[str, Any]] = []
    if isinstance(candidate_manifest, (str, Path)):
        mp = Path(candidate_manifest)
        if not mp.exists():
            raise PreconditionError(f"Manifest file not found: {mp}")
        with open(mp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "files" in data:
            manifest_entries = data["files"]
        elif isinstance(data, list):
            manifest_entries = data
        else:
            raise PreconditionError("Manifest file must be a JSON array or object with 'files' list")
    elif isinstance(candidate_manifest, list):
        manifest_entries = candidate_manifest
    else:
        raise PreconditionError(f"Invalid candidate_manifest type: {type(candidate_manifest).__name__}")

    # Validate bundle with sibling release-manifest.py
    rm_mod = _load_release_manifest_module()
    rm_mod.validate_manifest(manifest_entries, b_root, require_gateway=True)

    # Parse and verify reviewed preconditions
    top_uid, precond_map = parse_preconditions(preconditions)
    verify_live_preconditions(
        manifest_entries=manifest_entries,
        precond_map=precond_map,
        top_uid=top_uid,
        namespace_root=ns_root,
        strict_match_count=True,
    )

    # Setup transaction directories with restricted permissions (0700)
    tx_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tx_dir, 0o700)

    staged_dir = tx_dir / "staged"
    staged_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(staged_dir, 0o700)

    backups_dir = tx_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backups_dir, 0o700)

    # Save standalone recovery copy of this script alongside journal
    recovery_script_path = tx_dir / "release_transaction.py"
    current_script_path = Path(__file__).resolve()
    shutil.copy2(current_script_path, recovery_script_path)
    os.chmod(recovery_script_path, 0o700)

    entries_data: List[Dict[str, Any]] = []

    for entry in manifest_entries:
        local_rel = entry["local"]
        remote_path = os.path.normpath(entry["remote"])
        precond = precond_map[remote_path]

        src_file = b_root / local_rel
        if not src_file.exists():
            raise PreconditionError(f"Candidate source file missing: {src_file}")
        cand_sha256, cand_bytes = compute_file_sha256(src_file)

        target_disk, alias_disk, is_alias = resolve_disk_path(remote_path, ns_root)
        target_mode = normalize_mode(entry.get("mode", get_canonical_target_mode(remote_path)))

        kind = precond.get("kind", "file")

        # Determine action and whether live file is untouched
        action = ACTION_REPLACE
        orig_sha256 = None
        orig_bytes = None
        orig_mode = None
        orig_uid = precond.get("uid", top_uid)

        if kind == "absent":
            action = ACTION_CREATE
        elif kind == "symlink":
            # Boot alias: live target script details
            orig_sha256 = precond.get("target_sha256", AUDITED_BOOT_SCRIPT_SHA256)
            orig_bytes = precond.get("target_bytes", AUDITED_BOOT_SCRIPT_BYTES)
            orig_mode = mode_to_octal_str(normalize_mode(precond.get("target_mode", AUDITED_BOOT_SCRIPT_MODE)))
            if (
                cand_sha256.lower() == orig_sha256.lower()
                and cand_bytes == orig_bytes
                and target_mode == normalize_mode(orig_mode)
            ):
                action = ACTION_UNTOUCHED
        elif kind == "file":
            orig_sha256 = precond["sha256"]
            orig_bytes = precond["bytes"]
            orig_mode = mode_to_octal_str(normalize_mode(precond["mode"]))
            if (
                cand_sha256.lower() == orig_sha256.lower()
                and cand_bytes == orig_bytes
                and target_mode == normalize_mode(orig_mode)
            ):
                action = ACTION_UNTOUCHED

        entry_record: Dict[str, Any] = {
            "local": local_rel,
            "remote": remote_path,
            "is_alias": is_alias,
            "is_entrypoint": is_entrypoint(remote_path),
            "action": action,
            "candidate_sha256": cand_sha256,
            "candidate_bytes": cand_bytes,
            "target_mode": mode_to_octal_str(target_mode),
            "original_sha256": orig_sha256,
            "original_bytes": orig_bytes,
            "original_mode": orig_mode,
            "original_uid": orig_uid,
            "status": STATUS_PENDING,
        }

        # Stage candidate file if not untouched
        if action != ACTION_UNTOUCHED:
            rel_disk_path = target_disk.relative_to(ns_root)
            staged_file_path = staged_dir / rel_disk_path
            staged_file_path.parent.mkdir(parents=True, exist_ok=True)

            tmp_stage = staged_file_path.parent / f".tmp_stage_{staged_file_path.name}_{uuid.uuid4().hex}"
            with open(src_file, "rb") as sf:
                cand_content = sf.read()

            fd = os.open(str(tmp_stage), os.O_WRONLY | os.O_CREAT | os.O_EXCL, target_mode)
            try:
                os.write(fd, cand_content)
                os.chmod(fd, target_mode)
                os.fsync(fd)
            finally:
                os.close(fd)

            os.replace(tmp_stage, staged_file_path)
            fsync_dir(staged_file_path.parent)

            # Verify staged file checksum
            st_sha, st_bytes = compute_file_sha256(staged_file_path)
            if st_bytes != cand_bytes or st_sha.lower() != cand_sha256.lower():
                raise PreconditionError(f"Staged file verification failed for {remote_path}")

            entry_record["staged_file"] = str(staged_file_path.relative_to(tx_dir))

        # Backup live target if replacing
        if action == ACTION_REPLACE:
            rel_disk_path = target_disk.relative_to(ns_root)
            backup_file_path = backups_dir / rel_disk_path
            backup_file_path.parent.mkdir(parents=True, exist_ok=True)

            tmp_bk = backup_file_path.parent / f".tmp_bk_{backup_file_path.name}_{uuid.uuid4().hex}"
            with open(target_disk, "rb") as tf:
                live_content = tf.read()

            bk_mode = normalize_mode(orig_mode)
            fd = os.open(str(tmp_bk), os.O_WRONLY | os.O_CREAT | os.O_EXCL, bk_mode)
            try:
                os.write(fd, live_content)
                os.chmod(fd, bk_mode)
                os.fsync(fd)
            finally:
                os.close(fd)

            os.replace(tmp_bk, backup_file_path)
            fsync_dir(backup_file_path.parent)

            # Verify backup checksum against recorded original
            bk_sha, bk_bytes = compute_file_sha256(backup_file_path)
            if bk_bytes != orig_bytes or bk_sha.lower() != orig_sha256.lower():
                raise PreconditionError(f"Backup verification failed for {remote_path}")

            entry_record["backup_file"] = str(backup_file_path.relative_to(tx_dir))

        entries_data.append(entry_record)

    # Sort entries: helpers and assets before entrypoints
    entries_data.sort(key=lambda x: (1 if x["is_entrypoint"] else 0, x["remote"]))

    journal_data: Dict[str, Any] = {
        "version": JOURNAL_VERSION,
        "state": STATE_PREPARED,
        "created_at": time.time(),
        "updated_at": time.time(),
        "namespace_root": str(ns_root),
        "top_uid": top_uid,
        "preconditions": precond_map,
        "candidate_manifest": manifest_entries,
        "entries": entries_data,
    }

    tx = ReleaseTransaction(tx_dir)
    tx.save_journal(journal_data)
    return tx


def apply(
    transaction_dir: Union[str, Path],
    namespace_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Apply prepared transaction."""
    tx = ReleaseTransaction(transaction_dir)
    return tx.apply(namespace_root=namespace_root)


def commit(
    transaction_dir: Union[str, Path],
    namespace_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Commit applied transaction."""
    tx = ReleaseTransaction(transaction_dir)
    return tx.commit(namespace_root=namespace_root)


def rollback(
    transaction_dir: Union[str, Path],
    namespace_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Roll back prepared or applied transaction."""
    tx = ReleaseTransaction(transaction_dir)
    return tx.rollback(namespace_root=namespace_root)


def recover(
    transaction_dir: Union[str, Path],
    namespace_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Recover interrupted transaction."""
    tx = ReleaseTransaction(transaction_dir)
    return tx.recover(namespace_root=namespace_root)


def status(
    transaction_dir: Union[str, Path],
) -> Dict[str, Any]:
    """Get transaction status."""
    tx = ReleaseTransaction(transaction_dir)
    return tx.status()


def main() -> int:
    """CLI for transaction status and file recovery only. No apply CLI exposed."""
    parser = argparse.ArgumentParser(
        description="Release transaction status inspector and recovery tool"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    status_parser = subparsers.add_parser("status", help="Inspect transaction status")
    status_parser.add_argument(
        "--transaction-dir", "-t", required=True, help="Path to transaction directory"
    )

    recover_parser = subparsers.add_parser(
        "recover", help="Recover or rollback interrupted transaction"
    )
    recover_parser.add_argument(
        "--transaction-dir", "-t", required=True, help="Path to transaction directory"
    )
    recover_parser.add_argument(
        "--namespace-root", "-n", default=None, help="Optional live namespace root override"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    try:
        if args.command == "status":
            res = status(args.transaction_dir)
            print("Release Transaction Status:")
            for k, v in res.items():
                print(f"  {k}: {v}")
            return 0

        elif args.command == "recover":
            res = recover(args.transaction_dir, namespace_root=args.namespace_root)
            print(f"Release transaction recovery completed: state is {res.get('state')}")
            return 0

    except TransactionError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"UNEXPECTED ERROR: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
