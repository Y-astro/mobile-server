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
import errno
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


def validate_uid(uid_val: Any, field_name: str = "uid") -> int:
    """Validate that UID is a non-negative integer (never bool, null, or string)."""
    if type(uid_val) is not int or isinstance(uid_val, bool) or uid_val < 0:
        raise PreconditionError(
            f"Invalid {field_name}: expected non-negative integer, got {uid_val!r} ({type(uid_val).__name__})"
        )
    return uid_val


def verify_boot_alias_identity(
    alias_disk: Path,
    expected_raw_link: str,
    expected_dev: int,
    expected_ino: int,
    expected_uid: int,
    expected_mode: Optional[int] = None,
) -> None:
    """
    Verify exact identity of the known boot alias symlink.
    Checks existence, symlink type, exact raw link text, device, inode, UID, and mode.
    Fails closed if the symlink is missing, retargeted, unlinked, or replaced with a new inode.
    """
    if not path_lexists(alias_disk):
        raise PreconditionError(f"Boot alias symlink is missing: {alias_disk}")
    if not alias_disk.is_symlink():
        raise PreconditionError(f"Boot alias is not a symlink: {alias_disk}")

    actual_raw_link = os.readlink(alias_disk)
    if actual_raw_link != expected_raw_link:
        raise PreconditionError(
            f"Boot alias raw link mismatch: expected '{expected_raw_link}', got '{actual_raw_link}'"
        )

    st = alias_disk.lstat()
    if st.st_dev != expected_dev:
        raise PreconditionError(
            f"Boot alias device mismatch: expected dev {expected_dev}, got {st.st_dev}"
        )
    if st.st_ino != expected_ino:
        raise PreconditionError(
            f"Boot alias inode mismatch: expected ino {expected_ino}, got {st.st_ino}"
        )
    if st.st_uid != expected_uid:
        raise PreconditionError(
            f"Boot alias UID mismatch: expected UID {expected_uid}, got {st.st_uid}"
        )
    if expected_mode is not None and stat.S_IMODE(st.st_mode) != normalize_mode(expected_mode):
        raise PreconditionError(
            f"Boot alias mode mismatch: expected {mode_to_octal_str(expected_mode)}, got {mode_to_octal_str(stat.S_IMODE(st.st_mode))}"
        )


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


def write_all(fd: int, payload: bytes, max_blocking_retries: int = 5) -> None:
    """
    Write entire byte payload to file descriptor.
    Handles partial writes, EINTR retries, bounded BlockingIOError retries,
    and raises on zero-write or I/O error.
    """
    total = len(payload)
    offset = 0
    blocking_retries = 0
    while offset < total:
        try:
            written = os.write(fd, payload[offset:])
            if written == 0:
                raise OSError(errno.ENOSPC, "os.write wrote 0 bytes; storage full or I/O failure")
            offset += written
            blocking_retries = 0
        except InterruptedError:
            continue
        except BlockingIOError:
            blocking_retries += 1
            if blocking_retries > max_blocking_retries:
                raise
            time.sleep(0.01)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise


def fsync_dir(path: Path) -> None:
    """
    Synchronize directory metadata to storage.
    Propagates critical I/O errors (EIO, ENOSPC, EROFS) and programming bugs (EBADF).
    Narrowly tolerates documented non-fatal errnos (EINVAL/ENOTSUP on virtual mounts).
    """
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, getattr(errno, "ENOTSUP", 95), getattr(errno, "EOPNOTSUPP", 95)):
            pass
        else:
            raise
    finally:
        os.close(fd)


def write_atomic_file(
    dest_path: Path,
    content: bytes,
    mode: int,
    expected_bytes: Optional[int] = None,
    expected_sha256: Optional[str] = None,
    expected_uid: Optional[int] = None,
    prefix: str = ".tmp_",
) -> None:
    """
    Atomically write content to dest_path via temporary sibling replacement.
    Ensures temporary sibling is cleaned up on any failure (write, fsync, verify, replace).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.parent / f"{prefix}{dest_path.name}_{os.getpid()}_{uuid.uuid4().hex}"
    replaced = False
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            write_all(fd, content)
            os.chmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)

        # Pre-replace verification
        st = tmp_path.stat()
        if expected_bytes is not None and st.st_size != expected_bytes:
            raise OSError(
                errno.EIO,
                f"Pre-replace sibling size verification failed for {dest_path}: expected {expected_bytes}, got {st.st_size}",
            )
        if normalize_mode(st.st_mode) != normalize_mode(mode):
            raise OSError(
                errno.EIO,
                f"Pre-replace sibling mode verification failed for {dest_path}: expected {mode_to_octal_str(mode)}, got {mode_to_octal_str(stat.S_IMODE(st.st_mode))}",
            )
        if expected_sha256 is not None:
            actual_sha, _ = compute_file_sha256(tmp_path)
            if actual_sha.lower() != expected_sha256.lower():
                raise OSError(
                    errno.EIO,
                    f"Pre-replace sibling SHA-256 verification failed for {dest_path}: expected {expected_sha256}, got {actual_sha}",
                )
        if expected_uid is not None and st.st_uid != expected_uid:
            raise OSError(
                errno.EIO,
                f"Pre-replace sibling UID verification failed for {dest_path}: expected UID {expected_uid}, got {st.st_uid}",
            )

        os.replace(tmp_path, dest_path)
        replaced = True
        fsync_dir(dest_path.parent)
    finally:
        if not replaced and path_lexists(tmp_path):
            try:
                tmp_path.unlink()
            except OSError:
                pass


def write_atomic_json(path: Path, data: Any) -> None:
    """Atomically write JSON data with sibling replacement, write_all, and fsync."""
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    write_atomic_file(
        dest_path=path,
        content=payload,
        mode=0o600,
        expected_bytes=len(payload),
        prefix=".tmp_json_",
    )


VALID_STATES: Set[str] = {
    STATE_PREPARED,
    STATE_APPLYING,
    STATE_APPLIED,
    STATE_COMMITTED,
    STATE_ROLLING_BACK,
    STATE_ROLLED_BACK,
    STATE_RECOVERY_REQUIRED,
}
VALID_ACTIONS: Set[str] = {ACTION_CREATE, ACTION_REPLACE, ACTION_UNTOUCHED}
VALID_STATUSES: Set[str] = {
    STATUS_PENDING,
    STATUS_INTENT_WRITE,
    STATUS_WRITTEN,
    STATUS_UNTOUCHED,
    STATUS_ROLLED_BACK,
}


def validate_journal(journal: Any, transaction_dir: Path) -> None:
    """
    Strictly validate journal schema, state enums, types, and 1:1 correspondences.
    Rejects corrupted, emptied, tampered, or invalid journals fail-closed.
    """
    if not isinstance(journal, dict):
        raise TransactionError(f"Invalid journal: root must be a JSON object, got {type(journal).__name__}")

    version = journal.get("version")
    if type(version) is not int or isinstance(version, bool) or version != JOURNAL_VERSION:
        raise TransactionError(f"Invalid journal version: expected integer {JOURNAL_VERSION}, got {version!r}")

    state = journal.get("state")
    if state not in VALID_STATES:
        raise TransactionError(f"Invalid journal state: {state!r}")

    ns_root = journal.get("namespace_root")
    if not isinstance(ns_root, str) or not ns_root.strip():
        raise TransactionError("Invalid journal: missing or non-string 'namespace_root'")

    top_uid = journal.get("top_uid")
    if type(top_uid) is not int or isinstance(top_uid, bool) or top_uid < 0:
        raise TransactionError(f"Invalid journal: 'top_uid' must be a non-negative integer, got {top_uid!r}")

    preconds = journal.get("preconditions")
    if not isinstance(preconds, dict):
        raise TransactionError("Invalid journal: 'preconditions' must be a dict")

    manifest = journal.get("candidate_manifest")
    if not isinstance(manifest, list):
        raise TransactionError("Invalid journal: 'candidate_manifest' must be a list")

    entries = journal.get("entries")
    if not isinstance(entries, list) or len(entries) == 0:
        raise TransactionError("Invalid journal: 'entries' must be a non-empty list")

    if len(entries) != len(manifest) or len(entries) != len(preconds):
        raise TransactionError(
            f"Journal entry count mismatch: {len(entries)} entries, {len(manifest)} manifest items, {len(preconds)} preconditions"
        )

    seen_remotes: Set[str] = set()
    manifest_remotes = {os.path.normpath(m.get("remote", "")) for m in manifest if isinstance(m, dict)}

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TransactionError(f"Journal entry at index {idx} is not an object")

        remote = entry.get("remote")
        if not isinstance(remote, str) or not remote.startswith(TERMUX_ROOT_PREFIX):
            raise TransactionError(f"Journal entry at index {idx} has invalid remote path: {remote!r}")
        norm_remote = os.path.normpath(remote)
        if norm_remote in seen_remotes:
            raise TransactionError(f"Duplicate journal entry destination: {norm_remote}")
        seen_remotes.add(norm_remote)

        if norm_remote not in manifest_remotes:
            raise TransactionError(f"Journal entry destination not in candidate manifest: {norm_remote}")
        if norm_remote not in preconds:
            raise TransactionError(f"Journal entry destination not in preconditions: {norm_remote}")

        action = entry.get("action")
        if action not in VALID_ACTIONS:
            raise TransactionError(f"Invalid action {action!r} in journal entry for {remote}")

        status = entry.get("status")
        if status not in VALID_STATUSES:
            raise TransactionError(f"Invalid status {status!r} in journal entry for {remote}")

        cand_sha = entry.get("candidate_sha256")
        if not isinstance(cand_sha, str) or len(cand_sha) != 64 or not all(c in "0123456789abcdefABCDEF" for c in cand_sha):
            raise TransactionError(f"Invalid candidate_sha256 in journal entry for {remote}")

        cand_bytes = entry.get("candidate_bytes")
        if type(cand_bytes) is not int or isinstance(cand_bytes, bool) or cand_bytes < 0:
            raise TransactionError(f"Invalid candidate_bytes in journal entry for {remote}")

        target_mode = normalize_mode(entry.get("target_mode"))
        canonical_mode = get_canonical_target_mode(remote)
        if target_mode != canonical_mode:
            raise TransactionError(
                f"Journal entry target mode {mode_to_octal_str(target_mode)} does not match canonical {mode_to_octal_str(canonical_mode)} for {remote}"
            )

        orig_uid = entry.get("original_uid")
        if type(orig_uid) is not int or isinstance(orig_uid, bool) or orig_uid < 0:
            raise TransactionError(f"Invalid original_uid in journal entry for {remote}")

        if action in (ACTION_REPLACE, ACTION_UNTOUCHED):
            orig_sha = entry.get("original_sha256")
            if not isinstance(orig_sha, str) or len(orig_sha) != 64:
                raise TransactionError(f"Invalid original_sha256 in journal entry for {remote}")
            orig_bytes = entry.get("original_bytes")
            if type(orig_bytes) is not int or isinstance(orig_bytes, bool) or orig_bytes < 0:
                raise TransactionError(f"Invalid original_bytes in journal entry for {remote}")
            normalize_mode(entry.get("original_mode"))

        if action == ACTION_REPLACE:
            bk_rel = entry.get("backup_file")
            if not bk_rel:
                raise TransactionError(f"Missing backup_file in REPLACE entry for {remote}")
            validate_tx_relpath(bk_rel, transaction_dir)

        if action in (ACTION_REPLACE, ACTION_CREATE):
            stg_rel = entry.get("staged_file")
            if not stg_rel:
                raise TransactionError(f"Missing staged_file in {action} entry for {remote}")
            validate_tx_relpath(stg_rel, transaction_dir)

        if entry.get("is_alias"):
            raw_link = entry.get("alias_raw_link")
            if not isinstance(raw_link, str) or not raw_link.strip():
                raise TransactionError(f"Missing or invalid alias_raw_link for {remote}")
            for fld in ("alias_dev", "alias_ino", "alias_mode", "alias_uid"):
                val = entry.get(fld)
                if type(val) is not int or isinstance(val, bool):
                    raise TransactionError(f"Missing or invalid {fld} for {remote}: {val!r}")


def get_canonical_target_mode(remote_path: str) -> int:
    """
    Derive canonical owned mode from destination path:
    - dashboard executable: 0700
    - gateway executable: 0700
    - only agy-runner-loop: 0755
    - ordinary scripts (usr/bin/, home/bin/, start-services.sh): 0700
    - Python helper modules (home/.local/lib/mobile-server/): 0600
    - web/config assets (home/.config/, web/, static/): 0600
    """
    norm = remote_path.replace("\\", "/")
    if norm.endswith("/usr/bin/dashboard"):
        return 0o700
    if norm.endswith("/home/bin/mobile-gateway"):
        return 0o700
    if norm.endswith("/home/bin/agy-runner-loop") or norm.endswith("/usr/local/bin/agy-runner-loop"):
        return 0o755
    if "/home/.local/lib/mobile-server/" in norm:
        return 0o600
    if "/usr/bin/" in norm or "/home/bin/" in norm or norm.endswith("/start-services.sh") or norm.endswith("/start_services.sh"):
        return 0o700
    if "/.config/" in norm or "/web/" in norm or "/static/" in norm:
        return 0o600
    return 0o600


def validate_tx_relpath(rel_path_str: str, transaction_dir: Path) -> Path:
    """
    Validate that an internal transaction path reference is relative and resolves
    strictly inside transaction_dir without symlinks or traversal escapes.
    """
    if not isinstance(rel_path_str, str) or not rel_path_str.strip():
        raise TransactionError(f"Invalid transaction path reference: {rel_path_str!r}")
    if os.path.isabs(rel_path_str) or "\\" in rel_path_str:
        raise TransactionError(f"Refused absolute or non-posix transaction path reference: {rel_path_str}")
    norm = os.path.normpath(rel_path_str)
    parts = Path(norm).parts
    if not parts or parts[0] == ".." or ".." in parts:
        raise TransactionError(f"Refused traversal in transaction path reference: {rel_path_str}")

    resolved_tx = transaction_dir.resolve()
    full_path = transaction_dir / norm

    curr = transaction_dir
    for part in parts[:-1]:
        curr = curr / part
        if curr.is_symlink():
            raise TransactionError(f"Refused symlink in transaction directory path component: {curr}")
        if curr.exists():
            if not curr.resolve().is_relative_to(resolved_tx):
                raise TransactionError(f"Transaction path component escapes transaction directory: {curr}")

    if full_path.is_symlink():
        raise TransactionError(f"Refused symlink in transaction path: {full_path}")
    if full_path.exists():
        if not full_path.resolve().is_relative_to(resolved_tx):
            raise TransactionError(f"Transaction file escapes transaction directory: {full_path}")

    return full_path


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
    Strictly validates UID types (non-negative integer, never bool/null/str).
    Detects and rejects duplicate entries and missing UIDs.
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
            top_uid = validate_uid(data["uid"], "top-level 'uid'")
        if "files" in data and isinstance(data["files"], list):
            files_list = data["files"]
        else:
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

        # Validate entry UID
        item_uid = item.get("uid")
        if item_uid is not None:
            entry_uid = validate_uid(item_uid, f"entry 'uid' for {norm_path}")
            if top_uid is not None and entry_uid != top_uid:
                raise PreconditionError(
                    f"Entry UID {entry_uid} for {norm_path} does not match top-level UID {top_uid}"
                )
        else:
            if top_uid is not None:
                item["uid"] = top_uid
            else:
                raise PreconditionError(
                    f"Entry {norm_path} is missing explicit 'uid' and no valid top-level 'uid' declared"
                )

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
        expected_uid = validate_uid(precond.get("uid", top_uid), f"precondition UID for {remote_path}")

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

            # Check alias lstat identity and target
            alias_st = alias_disk.lstat()
            if alias_st.st_uid != expected_uid:
                raise PreconditionError(
                    f"UID mismatch on boot alias symlink {remote_path}: expected UID {expected_uid}, live has {alias_st.st_uid}"
                )

            if "raw_link" in precond:
                live_raw_link = os.readlink(alias_disk)
                if live_raw_link != precond["raw_link"]:
                    raise PreconditionError(
                        f"Boot alias raw link drift on {remote_path}: expected '{precond['raw_link']}', live has '{live_raw_link}'"
                    )

            if "dev" in precond and alias_st.st_dev != precond["dev"]:
                raise PreconditionError(
                    f"Boot alias device drift on {remote_path}: expected dev {precond['dev']}, live has {alias_st.st_dev}"
                )

            if "ino" in precond and alias_st.st_ino != precond["ino"]:
                raise PreconditionError(
                    f"Boot alias inode drift on {remote_path}: expected ino {precond['ino']}, live has {alias_st.st_ino}"
                )

            if "mode" in precond:
                exp_mode = normalize_mode(precond["mode"])
                if stat.S_IMODE(alias_st.st_mode) != exp_mode:
                    raise PreconditionError(
                        f"Boot alias mode drift on {remote_path}: expected {mode_to_octal_str(exp_mode)}, live has {mode_to_octal_str(stat.S_IMODE(alias_st.st_mode))}"
                    )

            # Check target file
            if not target_disk.exists() or target_disk.is_symlink():
                raise PreconditionError(
                    f"Boot target {target_disk} for alias {remote_path} must exist as a regular file"
                )

            target_st = target_disk.stat()
            if target_st.st_uid != expected_uid:
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
            if st.st_uid != expected_uid:
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


def _resolve_and_validate_namespace(
    journal_namespace_str: Optional[str],
    override_namespace: Optional[Union[str, Path]],
) -> Path:
    """Validate and resolve namespace root, rejecting conflicting overrides."""
    if not journal_namespace_str:
        raise TransactionError("Namespace root not defined in journal")
    journal_ns = Path(journal_namespace_str).resolve()
    if override_namespace is not None:
        passed_ns = Path(override_namespace).resolve()
        if passed_ns != journal_ns:
            raise TransactionError(
                f"Supplied namespace root '{passed_ns}' does not match journal namespace root '{journal_ns}'"
            )
    return journal_ns


def verify_live_candidate_state(
    journal: Dict[str, Any],
    namespace_root: Path,
) -> None:
    """
    Verify that all candidate files in namespace strictly match candidate manifest.
    Used for idempotent apply, commit, and recover(COMMITTED).
    Fails closed if any live drift, mode change, UID change, or unknown edit is present.
    """
    entries = journal.get("entries", [])
    top_uid = journal.get("top_uid")

    for entry in entries:
        target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)
        expected_uid = entry.get("original_uid", top_uid)
        target_mode = normalize_mode(entry["target_mode"])

        if is_alias:
            if alias_disk is None or not alias_disk.is_symlink():
                raise TransactionError(f"Boot alias symlink missing or corrupted: {entry['remote']}")
            verify_boot_alias_identity(
                alias_disk=alias_disk,
                expected_raw_link=entry["alias_raw_link"],
                expected_dev=entry["alias_dev"],
                expected_ino=entry["alias_ino"],
                expected_uid=entry["alias_uid"],
                expected_mode=entry.get("alias_mode"),
            )
            if not target_disk.exists() or target_disk.is_symlink():
                raise TransactionError(f"Boot target missing or not a regular file: {target_disk}")
            st = target_disk.stat()
            if stat.S_IMODE(st.st_mode) != target_mode:
                raise TransactionError(f"Mode drift on boot target {target_disk}")
            if st.st_size != entry["candidate_bytes"]:
                raise TransactionError(f"Size drift on boot target {target_disk}")
            live_sha, _ = compute_file_sha256(target_disk)
            if live_sha.lower() != entry["candidate_sha256"].lower():
                raise TransactionError(f"Hash drift on boot target {target_disk}")
            if expected_uid is not None and st.st_uid != expected_uid:
                raise TransactionError(f"UID drift on boot target {target_disk}")
            continue

        if entry["action"] == ACTION_UNTOUCHED:
            if not path_lexists(target_disk) or not target_disk.is_file() or target_disk.is_symlink():
                raise TransactionError(f"Untouched file missing or not regular file: {entry['remote']}")
            st = target_disk.stat()
            orig_mode = normalize_mode(entry["original_mode"])
            if stat.S_IMODE(st.st_mode) != orig_mode:
                raise TransactionError(f"Mode drift on untouched file {entry['remote']}")
            if st.st_size != entry["original_bytes"]:
                raise TransactionError(f"Size drift on untouched file {entry['remote']}")
            live_sha, _ = compute_file_sha256(target_disk)
            if live_sha.lower() != entry["original_sha256"].lower():
                raise TransactionError(f"Hash drift on untouched file {entry['remote']}")
            if expected_uid is not None and st.st_uid != expected_uid:
                raise TransactionError(f"UID drift on untouched file {entry['remote']}")
            continue

        # ACTION_CREATE or ACTION_REPLACE: must strictly match candidate
        if not path_lexists(target_disk) or not target_disk.is_file() or target_disk.is_symlink():
            raise TransactionError(f"Candidate destination file missing on disk: {entry['remote']}")
        st = target_disk.stat()
        live_mode = stat.S_IMODE(st.st_mode)
        if live_mode != target_mode:
            raise TransactionError(
                f"Mode drift for candidate {entry['remote']}: expected {mode_to_octal_str(target_mode)}, got {mode_to_octal_str(live_mode)}"
            )
        if st.st_size != entry["candidate_bytes"]:
            raise TransactionError(
                f"Size drift for candidate {entry['remote']}: expected {entry['candidate_bytes']}, got {st.st_size}"
            )
        live_sha, _ = compute_file_sha256(target_disk)
        if live_sha.lower() != entry["candidate_sha256"].lower():
            raise TransactionError(
                f"Hash drift for candidate {entry['remote']}: expected {entry['candidate_sha256']}, got {live_sha}"
            )
        if expected_uid is not None and st.st_uid != expected_uid:
            raise TransactionError(f"UID drift for candidate {entry['remote']}: expected {expected_uid}, got {st.st_uid}")


def verify_live_preimage_state(
    journal: Dict[str, Any],
    namespace_root: Path,
) -> None:
    """
    Verify that all files in namespace strictly match original preimage or absence.
    Used for idempotent rollback() and recover(ROLLED_BACK).
    Fails closed if any live drift or unknown edit is present.
    """
    entries = journal.get("entries", [])
    top_uid = journal.get("top_uid")

    for entry in entries:
        target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)
        expected_uid = entry.get("original_uid", top_uid)

        if entry["action"] == ACTION_CREATE:
            if path_lexists(target_disk):
                raise TransactionError(f"Expected absent file exists on disk: {entry['remote']}")
            continue

        orig_mode = normalize_mode(entry["original_mode"])
        if is_alias:
            if alias_disk is None or not alias_disk.is_symlink():
                raise TransactionError(f"Boot alias symlink missing or corrupted: {entry['remote']}")
            verify_boot_alias_identity(
                alias_disk=alias_disk,
                expected_raw_link=entry["alias_raw_link"],
                expected_dev=entry["alias_dev"],
                expected_ino=entry["alias_ino"],
                expected_uid=entry["alias_uid"],
                expected_mode=entry.get("alias_mode"),
            )
            if not target_disk.exists() or target_disk.is_symlink():
                raise TransactionError(f"Boot target missing or not regular file: {target_disk}")
            st = target_disk.stat()
            if stat.S_IMODE(st.st_mode) != orig_mode:
                raise TransactionError(f"Mode drift on boot target {target_disk}")
            if st.st_size != entry["original_bytes"]:
                raise TransactionError(f"Size drift on boot target {target_disk}")
            live_sha, _ = compute_file_sha256(target_disk)
            if live_sha.lower() != entry["original_sha256"].lower():
                raise TransactionError(f"Hash drift on boot target {target_disk}")
            if expected_uid is not None and st.st_uid != expected_uid:
                raise TransactionError(f"UID drift on boot target {target_disk}")
            continue

        if not path_lexists(target_disk) or not target_disk.is_file() or target_disk.is_symlink():
            raise TransactionError(f"Preimage file missing on disk: {entry['remote']}")
        st = target_disk.stat()
        live_mode = stat.S_IMODE(st.st_mode)
        if live_mode != orig_mode:
            raise TransactionError(
                f"Mode drift for preimage {entry['remote']}: expected {mode_to_octal_str(orig_mode)}, got {mode_to_octal_str(live_mode)}"
            )
        if st.st_size != entry["original_bytes"]:
            raise TransactionError(
                f"Size drift for preimage {entry['remote']}: expected {entry['original_bytes']}, got {st.st_size}"
            )
        live_sha, _ = compute_file_sha256(target_disk)
        if live_sha.lower() != entry["original_sha256"].lower():
            raise TransactionError(
                f"Hash drift for preimage {entry['remote']}: expected {entry['original_sha256']}, got {live_sha}"
            )
        if expected_uid is not None and st.st_uid != expected_uid:
            raise TransactionError(f"UID drift for preimage {entry['remote']}: expected {expected_uid}, got {st.st_uid}")


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
                data = json.load(f)
        except Exception as exc:
            raise TransactionError(f"Failed to read journal {self.journal_path}: {exc}") from exc
        validate_journal(data, self.transaction_dir)
        return data

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
        # 1. Pre-read only to locate recorded namespace root
        initial_journal = self.load_journal()
        ns_root = _resolve_and_validate_namespace(initial_journal.get("namespace_root"), namespace_root)

        # 2. Acquire namespace lock
        with NamespaceLock(ns_root):
            # 3. Reload journal under lock and re-verify namespace binding
            journal = self.load_journal()
            raw_ns = journal.get("namespace_root")
            if not raw_ns:
                raise TransactionError("Namespace root not defined in journal")
            reloaded_ns = Path(raw_ns).resolve()
            if reloaded_ns != ns_root:
                raise TransactionError(
                    f"Namespace root changed under lock: was '{ns_root}', now '{reloaded_ns}'"
                )
            if namespace_root is not None and Path(namespace_root).resolve() != reloaded_ns:
                raise TransactionError(
                    f"Supplied namespace root '{Path(namespace_root).resolve()}' does not match journal namespace root '{reloaded_ns}'"
                )

            # Recheck runtime UID under lock
            current_uid = os.getuid()
            if journal.get("top_uid") != current_uid:
                raise ApplyError(
                    f"Transaction recorded UID {journal.get('top_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                )
            for entry in journal.get("entries", []):
                if entry.get("original_uid") != current_uid:
                    raise ApplyError(
                        f"Entry {entry.get('remote')} UID {entry.get('original_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                    )

            curr_state = journal.get("state")

            # 4. Idempotent check under lock: MUST verify live candidate files
            if curr_state in (STATE_COMMITTED, STATE_APPLIED):
                verify_live_candidate_state(journal, ns_root)
                return {"state": curr_state, "status": "idempotent_success"}

            if curr_state != STATE_PREPARED:
                raise ApplyError(f"Cannot apply transaction in state {curr_state}")

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

            # Verify boot alias exact identity before ANY live writes!
            entries = journal.get("entries", [])
            for entry in entries:
                if entry["is_alias"]:
                    _, alias_disk, _ = resolve_disk_path(entry["remote"], ns_root)
                    assert alias_disk is not None
                    verify_boot_alias_identity(
                        alias_disk=alias_disk,
                        expected_raw_link=entry["alias_raw_link"],
                        expected_dev=entry["alias_dev"],
                        expected_ino=entry["alias_ino"],
                        expected_uid=entry["alias_uid"],
                        expected_mode=entry.get("alias_mode"),
                    )

            # Verify integrity and containment of all staged files before writes
            for entry in entries:
                if entry["action"] != ACTION_UNTOUCHED:
                    staged_path = validate_tx_relpath(entry["staged_file"], self.transaction_dir)
                    if not staged_path.exists():
                        raise ApplyError(f"Staged file missing for {entry['remote']}: {staged_path}")
                    staged_sha, staged_bytes = compute_file_sha256(staged_path)
                    if staged_bytes != entry["candidate_bytes"] or staged_sha.lower() != entry["candidate_sha256"].lower():
                        raise ApplyError(f"Staged file tampering detected for {entry['remote']}")

            # Verify integrity and containment of all backups before writes
            for entry in entries:
                if entry["action"] == ACTION_REPLACE:
                    backup_rel = entry.get("backup_file")
                    if not backup_rel:
                        raise ApplyError(f"Missing backup record for replaced file {entry['remote']}")
                    backup_path = validate_tx_relpath(backup_rel, self.transaction_dir)
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
                    if is_alias:
                        assert alias_disk is not None
                        verify_boot_alias_identity(
                            alias_disk=alias_disk,
                            expected_raw_link=entry["alias_raw_link"],
                            expected_dev=entry["alias_dev"],
                            expected_ino=entry["alias_ino"],
                            expected_uid=entry["alias_uid"],
                            expected_mode=entry.get("alias_mode"),
                        )

                    staged_path = validate_tx_relpath(entry["staged_file"], self.transaction_dir)
                    with open(staged_path, "rb") as sf:
                        staged_bytes_data = sf.read()

                    target_mode = normalize_mode(entry["target_mode"])
                    write_atomic_file(
                        dest_path=target_disk,
                        content=staged_bytes_data,
                        mode=target_mode,
                        expected_bytes=entry["candidate_bytes"],
                        expected_sha256=entry["candidate_sha256"],
                        expected_uid=entry.get("original_uid", top_uid),
                        prefix=".tmp_apply_",
                    )

                    # Post-apply verification on disk
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
                    try:
                        self.save_journal(journal)
                    except Exception:
                        pass

                if rollback_success:
                    raise ApplyError(
                        f"Apply failed and all touched files rolled back cleanly: {apply_exc}"
                    ) from apply_exc
                else:
                    raise RollbackError(
                        f"Apply failed ({apply_exc}) and rollback also failed: {rb_error}"
                    ) from rb_error

    def commit(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Commit applied transaction after verifying live candidate state under lock."""
        initial_journal = self.load_journal()
        ns_root = _resolve_and_validate_namespace(initial_journal.get("namespace_root"), namespace_root)

        with NamespaceLock(ns_root):
            journal = self.load_journal()
            raw_ns = journal.get("namespace_root")
            if not raw_ns:
                raise TransactionError("Namespace root not defined in journal")
            reloaded_ns = Path(raw_ns).resolve()
            if reloaded_ns != ns_root:
                raise TransactionError(
                    f"Namespace root changed under lock: was '{ns_root}', now '{reloaded_ns}'"
                )
            if namespace_root is not None and Path(namespace_root).resolve() != reloaded_ns:
                raise TransactionError(
                    f"Supplied namespace root '{Path(namespace_root).resolve()}' does not match journal namespace root '{reloaded_ns}'"
                )

            current_uid = os.getuid()
            if journal.get("top_uid") != current_uid:
                raise RecoveryError(
                    f"Transaction recorded UID {journal.get('top_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                )
            for entry in journal.get("entries", []):
                if entry.get("original_uid") != current_uid:
                    raise RecoveryError(
                        f"Entry {entry.get('remote')} UID {entry.get('original_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                    )

            curr_state = journal.get("state")

            if curr_state == STATE_COMMITTED:
                verify_live_candidate_state(journal, ns_root)
                return {"state": STATE_COMMITTED, "status": "idempotent_success"}

            if curr_state != STATE_APPLIED:
                raise TransactionError(f"Cannot commit transaction in state {curr_state} (must be APPLIED)")

            # Verify all candidate files strictly match candidate manifest before committing
            verify_live_candidate_state(journal, ns_root)

            journal["state"] = STATE_COMMITTED
            self.save_journal(journal)
            return {"state": STATE_COMMITTED, "status": "committed"}

    def rollback(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """
        Explicitly rollback prepared, applied, or committed transaction to original preimage.
        Verifies original/candidate preflight and preserves idempotence.
        """
        initial_journal = self.load_journal()
        ns_root = _resolve_and_validate_namespace(initial_journal.get("namespace_root"), namespace_root)

        with NamespaceLock(ns_root):
            journal = self.load_journal()
            raw_ns = journal.get("namespace_root")
            if not raw_ns:
                raise TransactionError("Namespace root not defined in journal")
            reloaded_ns = Path(raw_ns).resolve()
            if reloaded_ns != ns_root:
                raise TransactionError(
                    f"Namespace root changed under lock: was '{ns_root}', now '{reloaded_ns}'"
                )
            if namespace_root is not None and Path(namespace_root).resolve() != reloaded_ns:
                raise TransactionError(
                    f"Supplied namespace root '{Path(namespace_root).resolve()}' does not match journal namespace root '{reloaded_ns}'"
                )

            current_uid = os.getuid()
            if journal.get("top_uid") != current_uid:
                raise RecoveryError(
                    f"Transaction recorded UID {journal.get('top_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                )
            for entry in journal.get("entries", []):
                if entry.get("original_uid") != current_uid:
                    raise RecoveryError(
                        f"Entry {entry.get('remote')} UID {entry.get('original_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                    )

            curr_state = journal.get("state")

            if curr_state == STATE_ROLLED_BACK:
                verify_live_preimage_state(journal, ns_root)
                return {"state": STATE_ROLLED_BACK, "status": "idempotent_success"}

            journal["state"] = STATE_ROLLING_BACK
            self.save_journal(journal)

            try:
                self._perform_rollback(journal, ns_root, touched_only=False)
                journal["state"] = STATE_ROLLED_BACK
                self.save_journal(journal)
                return {"state": STATE_ROLLED_BACK, "status": "rolled_back"}
            except Exception as exc:
                journal["state"] = STATE_RECOVERY_REQUIRED
                journal["error"] = f"Rollback error: {exc}"
                try:
                    self.save_journal(journal)
                except Exception:
                    pass
                raise RollbackError(f"Rollback failed: {exc}") from exc

    def recover(self, namespace_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """
        Independent bundle-free recovery of an interrupted, applying, or applied transaction.
        In COMMITTED state, verifies candidate state and returns no-op/committed (never reverts).
        In ROLLED_BACK state, verifies preimage and returns idempotent_success.
        In other states, validates backups and live decisions before restoring original preimage.
        """
        initial_journal = self.load_journal()
        ns_root = _resolve_and_validate_namespace(initial_journal.get("namespace_root"), namespace_root)

        with NamespaceLock(ns_root):
            journal = self.load_journal()
            raw_ns = journal.get("namespace_root")
            if not raw_ns:
                raise TransactionError("Namespace root not defined in journal")
            reloaded_ns = Path(raw_ns).resolve()
            if reloaded_ns != ns_root:
                raise TransactionError(
                    f"Namespace root changed under lock: was '{ns_root}', now '{reloaded_ns}'"
                )
            if namespace_root is not None and Path(namespace_root).resolve() != reloaded_ns:
                raise TransactionError(
                    f"Supplied namespace root '{Path(namespace_root).resolve()}' does not match journal namespace root '{reloaded_ns}'"
                )

            current_uid = os.getuid()
            if journal.get("top_uid") != current_uid:
                raise RecoveryError(
                    f"Transaction recorded UID {journal.get('top_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                )
            for entry in journal.get("entries", []):
                if entry.get("original_uid") != current_uid:
                    raise RecoveryError(
                        f"Entry {entry.get('remote')} UID {entry.get('original_uid')} does not match runtime UID {current_uid}: mode/UID drift"
                    )

            curr_state = journal.get("state")

            # Root policy: recover(COMMITTED) verifies candidate state and returns committed/no-op
            if curr_state == STATE_COMMITTED:
                verify_live_candidate_state(journal, ns_root)
                return {"state": STATE_COMMITTED, "status": "idempotent_success"}

            if curr_state == STATE_ROLLED_BACK:
                verify_live_preimage_state(journal, ns_root)
                return {"state": STATE_ROLLED_BACK, "status": "idempotent_success"}

            journal["state"] = STATE_ROLLING_BACK
            self.save_journal(journal)

            try:
                self._perform_rollback(journal, ns_root, touched_only=False)
                journal["state"] = STATE_ROLLED_BACK
                self.save_journal(journal)
                return {"state": STATE_ROLLED_BACK, "status": "recovered"}
            except Exception as exc:
                journal["state"] = STATE_RECOVERY_REQUIRED
                journal["error"] = f"Recovery error: {exc}"
                try:
                    self.save_journal(journal)
                except Exception:
                    pass
                raise RecoveryError(f"Recovery failed: {exc}") from exc

    def _perform_rollback(
        self,
        journal: Dict[str, Any],
        namespace_root: Path,
        touched_only: bool = False,
    ) -> None:
        """
        Internal rollback worker.
        1. Validate all backups (checksums, modes, path containment).
        2. Validate live files against expected candidate or original preimages;
           check hash, bytes, modes, AND UID. Refuse unknown concurrent edits/chmod/chown.
        3. Restore touched files in reverse order (entrypoints first, helpers/assets last).
        """
        entries = journal.get("entries", [])
        top_uid = journal.get("top_uid")

        # 1. Validate all backups
        for entry in entries:
            if entry["action"] == ACTION_REPLACE:
                backup_rel = entry.get("backup_file")
                if not backup_rel:
                    raise RecoveryError(f"Missing backup specification for {entry['remote']}")
                backup_path = validate_tx_relpath(backup_rel, self.transaction_dir)
                if not backup_path.exists():
                    raise RecoveryError(f"Corrupted or missing backup file for {entry['remote']}: {backup_path}")
                bk_sha, bk_bytes = compute_file_sha256(backup_path)
                if bk_bytes != entry["original_bytes"] or bk_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Corrupted backup checksum for {entry['remote']}")

        # 2. Validate live file states: must match candidate state or original state (hash, bytes, mode, UID)
        for entry in entries:
            target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)
            expected_uid = entry.get("original_uid", top_uid)

            if is_alias:
                # Ensure alias symlink itself is still intact and identity matches exactly
                if alias_disk is None or not alias_disk.is_symlink():
                    raise RecoveryError(f"Boot alias {entry['remote']} is missing or not a symlink; cannot safely recover fail-closed")
                verify_boot_alias_identity(
                    alias_disk=alias_disk,
                    expected_raw_link=entry["alias_raw_link"],
                    expected_dev=entry["alias_dev"],
                    expected_ino=entry["alias_ino"],
                    expected_uid=entry["alias_uid"],
                    expected_mode=entry.get("alias_mode"),
                )
                if not target_disk.exists() or target_disk.is_symlink():
                    raise RecoveryError(f"Boot target {target_disk} for alias {entry['remote']} is missing or not a regular file")

            if entry["action"] == ACTION_UNTOUCHED:
                if not path_lexists(target_disk) or not target_disk.is_file() or target_disk.is_symlink():
                    raise RecoveryError(f"Untouched file missing or corrupted: {entry['remote']}")
                cur_sha, cur_bytes = compute_file_sha256(target_disk)
                st = target_disk.stat()
                orig_mode = normalize_mode(entry["original_mode"])
                if cur_bytes != entry["original_bytes"] or cur_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Unknown concurrent edit on untouched file: {entry['remote']}")
                if stat.S_IMODE(st.st_mode) != orig_mode:
                    raise RecoveryError(f"Unknown mode drift on untouched file: {entry['remote']}")
                if expected_uid is not None and st.st_uid != expected_uid:
                    raise RecoveryError(f"Unknown UID drift on untouched file: {entry['remote']}")
                continue

            if entry["action"] == ACTION_CREATE:
                if path_lexists(target_disk):
                    cur_sha, cur_bytes = compute_file_sha256(target_disk)
                    st = target_disk.stat()
                    target_mode = normalize_mode(entry["target_mode"])
                    if (
                        cur_bytes != entry["candidate_bytes"]
                        or cur_sha.lower() != entry["candidate_sha256"].lower()
                        or stat.S_IMODE(st.st_mode) != target_mode
                        or (expected_uid is not None and st.st_uid != expected_uid)
                    ):
                        raise RecoveryError(
                            f"Unknown concurrent edit on new file {entry['remote']}: "
                            "content/mode/UID matches neither absence nor candidate"
                        )
                continue

            if entry["action"] == ACTION_REPLACE:
                if not path_lexists(target_disk) or not target_disk.is_file() or target_disk.is_symlink():
                    raise RecoveryError(f"Live file missing during recovery: {entry['remote']}")
                cur_sha, cur_bytes = compute_file_sha256(target_disk)
                st = target_disk.stat()
                cur_mode = stat.S_IMODE(st.st_mode)
                cur_uid = st.st_uid
                target_mode = normalize_mode(entry["target_mode"])
                orig_mode = normalize_mode(entry["original_mode"])

                matches_candidate = (
                    cur_bytes == entry["candidate_bytes"]
                    and cur_sha.lower() == entry["candidate_sha256"].lower()
                    and cur_mode == target_mode
                    and (expected_uid is None or cur_uid == expected_uid)
                )
                matches_original = (
                    cur_bytes == entry["original_bytes"]
                    and cur_sha.lower() == entry["original_sha256"].lower()
                    and cur_mode == orig_mode
                    and (expected_uid is None or cur_uid == expected_uid)
                )
                if not matches_candidate and not matches_original:
                    raise RecoveryError(
                        f"Unknown concurrent edit or mode/UID drift on {entry['remote']}: "
                        f"current state matches neither candidate nor original preimage"
                    )

        # 3. Restore files in reverse entrypoint order
        for entry in reversed(entries):
            if entry["action"] == ACTION_UNTOUCHED:
                continue

            if touched_only and entry.get("status") not in (STATUS_INTENT_WRITE, STATUS_WRITTEN):
                continue

            target_disk, alias_disk, is_alias = resolve_disk_path(entry["remote"], namespace_root)
            expected_uid = entry.get("original_uid", top_uid)

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
                    st = target_disk.stat()
                    cur_mode = stat.S_IMODE(st.st_mode)
                    cur_uid = st.st_uid
                    if (
                        cur_bytes == entry["original_bytes"]
                        and cur_sha.lower() == entry["original_sha256"].lower()
                        and cur_mode == orig_mode
                        and (expected_uid is None or cur_uid == expected_uid)
                    ):
                        entry["status"] = STATUS_ROLLED_BACK
                        continue

                backup_path = validate_tx_relpath(entry["backup_file"], self.transaction_dir)
                with open(backup_path, "rb") as bf:
                    backup_bytes = bf.read()

                try:
                    write_atomic_file(
                        dest_path=target_disk,
                        content=backup_bytes,
                        mode=orig_mode,
                        expected_bytes=entry["original_bytes"],
                        expected_sha256=entry["original_sha256"],
                        expected_uid=expected_uid,
                        prefix=".tmp_rb_",
                    )
                except Exception as restore_err:
                    raise RecoveryError(f"Pre-replace rollback verification or replacement failed for {entry['remote']}: {restore_err}") from restore_err

                # Verify restored bytes, mode, hash, and UID on target
                restored_st = target_disk.stat()
                live_mode = stat.S_IMODE(restored_st.st_mode)
                if live_mode != orig_mode:
                    raise RecoveryError(f"Restored mode mismatch for {entry['remote']}")
                if restored_st.st_size != entry["original_bytes"]:
                    raise RecoveryError(f"Restored byte count mismatch for {entry['remote']}")
                restored_sha, _ = compute_file_sha256(target_disk)
                if restored_sha.lower() != entry["original_sha256"].lower():
                    raise RecoveryError(f"Restored SHA-256 mismatch for {entry['remote']}")
                if expected_uid is not None and restored_st.st_uid != expected_uid:
                    raise RecoveryError(f"Restored UID mismatch for {entry['remote']}")

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
    2. Parses and verifies caller-provided reviewed preconditions.
    3. Validates canonical mode on all candidate manifest entries.
    4. Validates runtime UID identity against declared UIDs before any disk creation.
    5. Guards transaction directory: refuses non-empty transaction directories.
    6. Acquires NamespaceLock on namespace root.
    7. Stages changed candidate files and creates live backups.
    8. Saves standalone recovery copy of this script in transaction directory.
    9. Writes initial PREPARED atomic journal.
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

    # Validate canonical mode on all candidate manifest entries
    for entry in manifest_entries:
        remote_path = os.path.normpath(entry["remote"])
        canonical_mode = get_canonical_target_mode(remote_path)
        if "mode" in entry:
            cand_mode = normalize_mode(entry["mode"])
            if cand_mode != canonical_mode:
                raise PreconditionError(
                    f"Candidate manifest declares non-canonical mode {mode_to_octal_str(cand_mode)} "
                    f"for {remote_path} (canonical policy requires {mode_to_octal_str(canonical_mode)})"
                )

    # Enforce runtime UID consistency BEFORE any disk operations
    current_uid = os.getuid()
    current_euid = os.geteuid()
    if current_uid != current_euid:
        raise PreconditionError(f"Effective UID {current_euid} does not match real UID {current_uid}")

    if top_uid is not None and top_uid != current_uid:
        raise PreconditionError(
            f"UID mismatch: declared top-level UID {top_uid} does not match runtime UID {current_uid}"
        )

    for remote_path, precond in precond_map.items():
        entry_uid = precond["uid"]
        if entry_uid != current_uid:
            raise PreconditionError(
                f"UID mismatch: precondition entry UID {entry_uid} for {remote_path} does not match runtime UID {current_uid}"
            )

    # Guard transaction directory: refuse non-empty directory
    if tx_dir.exists():
        existing_items = list(tx_dir.iterdir())
        if len(existing_items) > 0:
            raise TransactionError(
                f"Transaction directory is not empty: {tx_dir}. Refusing to overwrite existing transaction."
            )

    # Acquire namespace lock for the entire preparation phase
    with NamespaceLock(ns_root):
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
            target_mode = get_canonical_target_mode(remote_path)

            kind = precond.get("kind", "file")

            action = ACTION_REPLACE
            orig_sha256 = None
            orig_bytes = None
            orig_mode = None
            orig_uid = precond["uid"]

            alias_raw_link = None
            alias_dev = None
            alias_ino = None
            alias_mode = None
            alias_uid = None

            if kind == "absent":
                action = ACTION_CREATE
            elif kind == "symlink":
                orig_sha256 = precond.get("target_sha256", AUDITED_BOOT_SCRIPT_SHA256)
                orig_bytes = precond.get("target_bytes", AUDITED_BOOT_SCRIPT_BYTES)
                orig_mode = mode_to_octal_str(normalize_mode(precond.get("target_mode", AUDITED_BOOT_SCRIPT_MODE)))
                if (
                    cand_sha256.lower() == orig_sha256.lower()
                    and cand_bytes == orig_bytes
                    and target_mode == normalize_mode(orig_mode)
                ):
                    action = ACTION_UNTOUCHED

                assert alias_disk is not None
                alias_st = alias_disk.lstat()
                alias_raw_link = os.readlink(alias_disk)
                alias_dev = alias_st.st_dev
                alias_ino = alias_st.st_ino
                alias_mode = stat.S_IMODE(alias_st.st_mode)
                alias_uid = alias_st.st_uid
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

            if is_alias:
                entry_record["alias_raw_link"] = alias_raw_link
                entry_record["alias_dev"] = alias_dev
                entry_record["alias_ino"] = alias_ino
                entry_record["alias_mode"] = alias_mode
                entry_record["alias_uid"] = alias_uid

            if action != ACTION_UNTOUCHED:
                rel_disk_path = target_disk.relative_to(ns_root)
                staged_file_path = staged_dir / rel_disk_path
                with open(src_file, "rb") as sf:
                    cand_content = sf.read()

                write_atomic_file(
                    dest_path=staged_file_path,
                    content=cand_content,
                    mode=target_mode,
                    expected_bytes=cand_bytes,
                    expected_sha256=cand_sha256,
                    prefix=".tmp_stage_",
                )
                entry_record["staged_file"] = str(staged_file_path.relative_to(tx_dir))

            if action == ACTION_REPLACE:
                rel_disk_path = target_disk.relative_to(ns_root)
                backup_file_path = backups_dir / rel_disk_path
                with open(target_disk, "rb") as tf:
                    live_content = tf.read()

                bk_mode = normalize_mode(orig_mode)
                write_atomic_file(
                    dest_path=backup_file_path,
                    content=live_content,
                    mode=bk_mode,
                    expected_bytes=orig_bytes,
                    expected_sha256=orig_sha256,
                    prefix=".tmp_bk_",
                )
                entry_record["backup_file"] = str(backup_file_path.relative_to(tx_dir))

            entries_data.append(entry_record)

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
