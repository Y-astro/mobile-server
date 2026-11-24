"""Bounded log store and capture companion for Suwayomi-related services.

Guarantees a strict aggregate 20,000,000-byte cap across active and rotated files
using a shared ring store (active + 9 archives @ 2,000,000 bytes).
All writes sanitize credentials before persistence.
"""

from datetime import datetime, timezone
import errno
import fcntl
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import sys
import time
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, TextIO, Tuple

TOTAL_BUDGET_BYTES = 20_000_000
SEGMENT_SIZE_BYTES = 2_000_000
MAX_ARCHIVES = 9
DEFAULT_LOG_DIR = Path("/storage/emulated/0/Suwayomi/logs")
LOG_FILENAME = "suwayomi.log"
LOCK_FILENAME = "suwayomi.lock"
MARKER_FILENAME = ".imported_legacy"
MAX_LOCK_WAIT_SECONDS = 5.0
MAX_RECORD_LINE_BYTES = 64 * 1024

# Redact incomplete quoted values too: capture may truncate an oversized record.
_SECRET_KEYS = r'password|passwd|pass|secret|client_secret|token|access_?token|refresh_?token|api_?key|auth'
RE_URL_USERINFO = re.compile(r'\b([a-zA-Z][a-zA-Z0-9+\-.]{0,31}://)[^/@\s]+@')
RE_HEADERS = re.compile(r'((?:proxy-authorization|authorization|set-cookie|cookie)\s*[:=]\s*)[^\r\n]*', re.I)
RE_ASSIGNMENT = re.compile(
    r"""(["']?\b(?:""" + _SECRET_KEYS + r""")["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\\r\n])*"?|'(?:\\.|[^'\\\r\n])*'?|[^\s,;&}\]\r\n]+)""", re.I)
RE_AUTH = re.compile(r'\b(Bearer|Basic)\s+[^\s,;\r\n]+', re.I)


def redact_sensitive_text(text: str) -> str:
    """Remove credentials before writing any retained server log."""
    result = RE_URL_USERINFO.sub(r'\1[REDACTED]@', text)
    for pattern in (RE_HEADERS, RE_ASSIGNMENT, RE_AUTH):
        result = pattern.sub(lambda match: match[1] + '[REDACTED]', result)
    return result


def legacy_candidates(home: Path) -> List[Path]:
    """Exact owned legacy names; never sweep unrelated service or user files."""
    result = []
    application = re.compile(r'application(?:\.log|_\d{4}-\d{2}-\d{2}_\d+\.log\.gz)')
    helper = re.compile(r'(?:graphql_filter|daily_update)\.log(?:\.[12])?')
    for directory in (home / 'suwayomi/logs', home / 'suwayomi/data/logs'):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if (application.fullmatch(path.name) or helper.fullmatch(path.name)) and path.is_file() and not path.is_symlink():
                result.append(path)
    directory = home / '.config/server_recovery'
    for service in ('suwayomi', 'suwayomi_filter', 'suwayomi_scheduler'):
        for suffix in ('', '.1'):
            path = directory / (service + '.log' + suffix)
            if path.is_file() and not path.is_symlink():
                result.append(path)
    return sorted(result, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def truncate_utf8_to_bytes(s: str, max_bytes: int) -> str:
    """Truncate string so its UTF-8 encoding is at most max_bytes, strictly valid."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    sliced = encoded[:max_bytes]
    # Decode with ignore drops trailing incomplete code unit cleanly
    return sliced.decode("utf-8", errors="ignore")


class BoundedLogStore:
    def __init__(
        self,
        log_dir: os.PathLike | str = DEFAULT_LOG_DIR,
        segment_size: int = SEGMENT_SIZE_BYTES,
        total_budget: int = TOTAL_BUDGET_BYTES,
        max_archives: int = MAX_ARCHIVES,
    ):
        if segment_size < 64:
            raise ValueError(f"segment_size must be positive, got {segment_size}")
        if total_budget < segment_size:
            raise ValueError(f"total_budget ({total_budget}) cannot be smaller than segment_size ({segment_size})")
        if max_archives < 0:
            raise ValueError(f"max_archives must be >= 0, got {max_archives}")

        self.log_dir = Path(log_dir)
        self.segment_size = segment_size
        self.total_budget = total_budget
        self.max_archives = min(max_archives, total_budget // segment_size - 1)
        self._prepared = False
        self.active_file = self.log_dir / LOG_FILENAME
        canonical = self.log_dir.resolve()
        if canonical.is_relative_to(Path('/storage/emulated')):
            # Android shared storage rejects flock (ENOSYS); coordinate privately.
            key = hashlib.sha256(str(canonical).encode()).hexdigest()
            self.lock_file = Path.home() / '.cache/suwayomi-log-locks' / (key + '.lock')
        else:
            self.lock_file = self.log_dir / LOCK_FILENAME
        self.marker_file = self.log_dir / MARKER_FILENAME
        self._ensure_dir()
        self._validate_not_symlink(self.active_file)
        self._validate_not_symlink(self.lock_file)

    def _ensure_dir(self) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _validate_not_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"Security violation: path is a symlink: {path}")

    def _lock(self, lock_fd: int, timeout: float = MAX_LOCK_WAIT_SECONDS) -> bool:
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, OSError) as e:
                if isinstance(e, BlockingIOError) or getattr(e, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                    if time.monotonic() - start >= timeout:
                        return False
                    time.sleep(0.02)
                else:
                    return False

    def _unlock(self, lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def total_disk_bytes(self) -> int:
        """Calculate total bytes of active and archive log files."""
        total = 0
        if not self.log_dir.exists():
            return 0
        for entry in self.log_dir.glob("suwayomi.log*"):
            if entry.is_file() and not entry.is_symlink():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    def _rotate_if_needed(self, incoming_bytes_len: int) -> bool:
        """Fail closed when a required prune or rename cannot be completed."""
        owned = [self.active_file] + [self.log_dir / f'{LOG_FILENAME}.{i}' for i in range(1, self.max_archives + 1)]
        for path in owned:
            self._validate_not_symlink(path)
            if path.exists() and not path.is_file():
                raise OSError('Log path is not a regular file')
        if not self._prepared:
            for path in owned:
                if path.exists() and path.stat().st_size > self.segment_size:
                    with path.open('rb') as handle:
                        handle.seek(-self.segment_size, os.SEEK_END)
                        tail = handle.read(self.segment_size)
                    # The partial first record may lack its original credential key.
                    end = tail.find(b'\n')
                    path.write_bytes(tail[end + 1:] if end >= 0 else b'')
            self._prepared = True
        size = self.active_file.stat().st_size if self.active_file.exists() else 0
        if size + incoming_bytes_len > self.segment_size:
            owned[-1].unlink(missing_ok=True)
            for i in range(len(owned) - 2, -1, -1):
                if owned[i].exists():
                    owned[i].replace(owned[i + 1])
        # An unknown old log must not cause the writer to exceed the aggregate cap.
        for path in reversed(owned[1:]):
            if self.total_disk_bytes() + incoming_bytes_len <= self.total_budget:
                break
            path.unlink(missing_ok=True)
        return self.total_disk_bytes() + incoming_bytes_len <= self.total_budget

    def _append_unlocked(self, source: str, text: str) -> None:
        if not text:
            return
        clipped = len(text) > MAX_RECORD_LINE_BYTES
        redacted = redact_sensitive_text(text[:MAX_RECORD_LINE_BYTES])
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        source = re.sub(r'[^a-zA-Z0-9_-]', '_', source)[:64]
        prefix = f'{now} [{source}] '
        marker = ' ... [TRUNCATED]'
        limit = min(self.segment_size, MAX_RECORD_LINE_BYTES)
        line = prefix + redacted.rstrip('\r\n')
        if clipped or len(line.encode('utf-8')) + 1 > limit:
            line = truncate_utf8_to_bytes(line, limit - len(marker) - 1) + marker
        payload = (line + '\n').encode('utf-8')
        if not self._rotate_if_needed(len(payload)):
            raise OSError('Unable to maintain the aggregate log budget')
        with self.active_file.open('ab', buffering=0) as handle:
            handle.write(payload)

    def append(self, source: str, text: str) -> bool:
        """Thread-safe and process-safe append; failed storage never stops capture."""
        self._ensure_dir()
        try:
            self._validate_not_symlink(self.lock_file)
            with self.lock_file.open('a+b') as handle:
                if not self._lock(handle.fileno(), timeout=0.05):
                    return False
                try:
                    self._append_unlocked(source, text)
                    return True
                finally:
                    self._unlock(handle.fileno())
        except (OSError, ValueError):
            return False

    def read_active(self) -> str:
        """Read current content of the active log file, ensuring valid UTF-8."""
        if not self.active_file.exists() or self.active_file.is_symlink():
            return ""
        try:
            return self.active_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def import_legacy_logs_offline(self, candidates: Sequence[os.PathLike | str], *, home: Optional[Path] = None) -> int:
        """Import only exact owned files while the controlled release has stopped producers.

        Failed writes keep originals. Failed deletion keeps migration retryable.
        """
        home = Path.home() if home is None else Path(home)
        allowed = {path.absolute() for path in legacy_candidates(home)}
        requested = [Path(path).absolute() for path in candidates]
        if any(path.exists() and path not in allowed for path in requested):
            raise RuntimeError('Refusing an unowned legacy log path')
        self._validate_not_symlink(self.lock_file)
        self._validate_not_symlink(self.marker_file)
        count = 0
        with self.lock_file.open('a+b') as handle:
            if not self._lock(handle.fileno()):
                raise RuntimeError('Timed out acquiring the migration log lock')
            try:
                if self.marker_file.exists():
                    if not any(path.exists() for path in requested):
                        return 0
                    self.marker_file.unlink()
                imported = []
                for path in requested:
                    if not path.exists():
                        continue
                    opener = gzip.open if path.suffix == '.gz' else open
                    batch = []
                    size = 0
                    with opener(path, 'rb') as source:
                        for line, clipped in read_bounded_records(source):
                            record = line + (' ... [TRUNCATED]' if clipped else '') + '\n'
                            cost = len(record.encode('utf-8'))
                            if batch and size + cost > 60000:
                                self._append_unlocked('legacy', ''.join(batch))
                                batch, size = [], 0
                            batch.append(record)
                            size += cost
                            count += 1
                    if batch:
                        self._append_unlocked('legacy', ''.join(batch))
                    imported.append(path)
                for path in self.log_dir.glob('suwayomi.log*'):
                    if path.is_file() and not path.is_symlink():
                        with path.open('rb') as retained:
                            os.fsync(retained.fileno())
                for path in imported:
                    path.unlink()
                # Empty marker carries no log bytes outside the aggregate budget.
                self.marker_file.touch()
            finally:
                self._unlock(handle.fileno())
        return count


def read_bounded_records(stream: BinaryIO, max_line_bytes: int = MAX_RECORD_LINE_BYTES) -> Iterable[Tuple[str, bool]]:
    """Yield lines bounded at max_line_bytes without unbounded memory allocation on missing newlines."""
    buf = bytearray()
    discarding = False
    while True:
        try:
            chunk = (stream.read1 if hasattr(stream, 'read1') else stream.read)(8192)
        except (OSError, ValueError):
            break
        if not chunk:
            if buf:
                text = buf.decode("utf-8", errors="replace")
                yield text, discarding
            break

        start = 0
        while start < len(chunk):
            nl_pos = chunk.find(b"\n", start)
            if nl_pos != -1:
                part = chunk[start:nl_pos]
                start = nl_pos + 1
                if discarding:
                    discarding = False
                    buf.clear()
                else:
                    needed = max_line_bytes - len(buf)
                    if len(part) > needed:
                        buf.extend(part[:needed])
                        yield buf.decode("utf-8", errors="replace"), True
                    else:
                        buf.extend(part)
                        yield buf.decode("utf-8", errors="replace"), False
                    buf.clear()
            else:
                part = chunk[start:]
                start = len(chunk)
                if not discarding:
                    needed = max_line_bytes - len(buf)
                    if len(part) >= needed:
                        buf.extend(part[:needed])
                        yield buf.decode("utf-8", errors="replace"), True
                        buf.clear()
                        discarding = True
                    else:
                        buf.extend(part)


def capture_stream(stream: BinaryIO | TextIO, store: BoundedLogStore, service_id: str) -> None:
    """Read a process output stream chunk by chunk and append to BoundedLogStore until EOF."""
    raw_stream = stream.buffer if hasattr(stream, "buffer") else stream
    try:
        for record, truncated in read_bounded_records(raw_stream):
            if not record:
                continue
            if truncated:
                record = record + " ... [TRUNCATED]"
            store.append(service_id, record)
    except (OSError, ValueError):
        pass


def main() -> None:
    """CLI for detached capture and release-controlled offline migration."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('service')
    capture.add_argument('log_dir', nargs='?', default=os.environ.get('SUWAYOMI_LOG_DIR', str(DEFAULT_LOG_DIR)))
    migrate = commands.add_parser('migrate')
    migrate.add_argument('--home', type=Path, default=Path.home())
    migrate.add_argument('--log-dir', type=Path, default=DEFAULT_LOG_DIR)
    args = parser.parse_args()
    if args.command == 'capture':
        capture_stream(sys.stdin.buffer, BoundedLogStore(args.log_dir), args.service)
    else:
        store = BoundedLogStore(args.log_dir)
        count = store.import_legacy_logs_offline(legacy_candidates(args.home), home=args.home)
        print(f'Migrated {count} entries.')


if __name__ == '__main__':
    main()
