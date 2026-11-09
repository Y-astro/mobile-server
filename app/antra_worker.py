"""Antra background download worker.

Supervised worker process for durable Antra music downloads.
Handles singleton execution lock, Termux DNS fix re-exec,
folder-mode continuation for playlists > 100 tracks, FLAC validation (16-bit and 24-bit),
streaming path traversal protection, atomic publication, and acknowledgements.
Nothing starts on import; main loop executes under __main__ or run_forever().
"""
import fcntl
import ctypes
import contextlib
import subprocess
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

MOBILE_LIB = os.path.expanduser("~/.local/lib/mobile-server")
if os.path.isdir(MOBILE_LIB) and MOBILE_LIB not in sys.path:
    sys.path.insert(0, MOBILE_LIB)
elif str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from antra_store import AntraStore, DEFAULT_OUTPUT_ROOTS, MAX_AUTOMATIC_RETRY_ROUNDS
from antra_client import (
    AntraClient,
    DailyLimitError,
    ConcurrencyLimitError,
    UncertainSubmissionError,
    MissingCredentialsError,
    MissingDependencyError,
    AntraClientError,
)

logger = logging.getLogger("antra_worker")

PINNED_LIBDNS_PATH = "/data/data/com.termux/files/home/suwayomi/libdns_fix.so"
PINNED_LIBDNS_SHA256 = "9e5dcc8e8f61be611556bf4a51f1b1c2b41e5bc5152bf571bc9427ea2633bc86"
DEFAULT_LOCK_FILE = Path(os.path.expanduser("~/.local/share/mobile-server/antra/worker.lock"))
MIN_FREE_SPACE_BYTES = 100 * 1024 * 1024  # 100 MiB


def maybe_reexec_dns_fix() -> bool:
    """Re-exec process with LD_PRELOAD libdns_fix.so if in real Termux and not already loaded."""
    if os.environ.get("ANTRA_DNS_FIXED") == "1":
        return False

    if not os.path.isdir("/data/data/com.termux"):
        return False

    shim_path = Path(PINNED_LIBDNS_PATH)
    if not shim_path.is_file() or shim_path.is_symlink():
        return False

    try:
        with open(shim_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != PINNED_LIBDNS_SHA256:
            return False
    except OSError:
        return False

    new_env = dict(os.environ)
    new_env["LD_PRELOAD"] = str(shim_path)
    new_env["ANTRA_DNS_FIXED"] = "1"
    try:
        os.execve(sys.executable, [sys.executable] + sys.argv, new_env)
    except OSError:
        return False
    return True


def verify_flac(
    file_path: Path,
    expected_length: Optional[int] = None,
) -> Tuple[bool, Optional[int], Optional[int], str]:
    """Inspect FLAC STREAMINFO header to verify valid native FLAC bit depth (16-bit or 24-bit), sensible sample rate,

    and ensure actual audio frames exist past metadata.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError as e:
        return False, None, None, "Failed to inspect audio file"

    if file_size < 42:
        return False, None, None, "File too small to be a valid FLAC stream"

    if expected_length is not None and file_size != expected_length:
        return False, None, None, f"Downloaded size {file_size} does not match Content-Length {expected_length}"

    try:
        with open(file_path, "rb") as stream:
            if stream.read(4) != b"fLaC":
                return False, None, None, "Invalid audio format: missing FLAC marker"
            streaminfo = None
            while True:
                header = stream.read(4)
                if len(header) != 4:
                    return False, None, None, "Truncated FLAC metadata block"
                block_type = header[0] & 0x7F
                block_len = int.from_bytes(header[1:], "big")
                if stream.tell() + block_len > file_size:
                    return False, None, None, "Truncated FLAC metadata block"
                if streaminfo is None:
                    if block_type != 0 or block_len != 34:
                        return False, None, None, "Corrupted FLAC STREAMINFO length or ordering"
                    streaminfo = stream.read(34)
                elif block_type == 0 or block_type == 127:
                    return False, None, None, "Invalid FLAC metadata block"
                else:
                    stream.seek(block_len, os.SEEK_CUR)
                if header[0] & 0x80:
                    offset = stream.tell()
                    break
    except OSError:
        return False, None, None, "Failed to read audio file"

    info_bytes = int.from_bytes(streaminfo[10:18], "big")
    sample_rate = (info_bytes >> 44) & 0xFFFFF
    channels = ((info_bytes >> 41) & 0x7) + 1
    bits_per_sample = ((info_bytes >> 36) & 0x1F) + 1

    if sample_rate == 0 or sample_rate < 8000 or sample_rate > 384000:
        return False, bits_per_sample, sample_rate, f"Invalid or zero sample rate: {sample_rate} Hz"

    # Audio frames must exist past the last metadata block
    if file_size <= offset or (file_size - offset) < 16:
        return False, None, None, "Header-only FLAC stream: missing audio frames"

    if bits_per_sample not in (16, 24):
        return (
            False,
            bits_per_sample,
            sample_rate,
            f"Unsupported FLAC bit depth: {bits_per_sample}-bit ({sample_rate} Hz, {channels} ch)",
        )

    decoder = shutil.which("ffmpeg")
    if not decoder:
        return False, bits_per_sample, sample_rate, "Audio verifier unavailable"
    try:
        verified = subprocess.run(
            [decoder, "-v", "error", "-xerror", "-f", "flac", "-i", str(file_path),
             "-map", "0:a:0", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, bits_per_sample, sample_rate, "FLAC decode verification failed"
    if verified.returncode:
        return False, bits_per_sample, sample_rate, "FLAC decode verification failed"
    return (
        True,
        bits_per_sample,
        sample_rate,
        f"Verified {bits_per_sample}-bit FLAC ({sample_rate} Hz, {channels} ch)",
    )


verify_flac_24bit = verify_flac


MAX_TRACK_BYTES = 8 * 1024 * 1024 * 1024


def file_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def sync_track_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            # Android emulated storage may reject directory fsync; SQLite checkpoints remain on private durable storage.
            if error.errno not in (22, 95):
                raise
    finally:
        os.close(descriptor)


def publish_noreplace(source, destination):
    """Use Linux's atomic RENAME_NOREPLACE; unsupported mounts fail without overwriting."""
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameat2", None)
    arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if operation is not None:
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = operation(*arguments)
    else:
        number = {"aarch64": 276, "x86_64": 316}.get(os.uname().machine)
        if number is None:
            raise OSError("Atomic no-clobber publication is unsupported")
        result = library.syscall(ctypes.c_long(number), ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(source)),
                                 ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(destination)), ctypes.c_uint(1))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def ensure_track_parent(path, root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Music root is unavailable")
    current = root
    for part in path.relative_to(root.resolve()).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Track parent must not be a symlink")
        current.mkdir(mode=0o755, exist_ok=True)
        if not current.is_dir():
            raise ValueError("Track parent is not a directory")


def sanitize_track_path(raw_rel_path: str, base_dir: Path) -> Path:
    if not isinstance(raw_rel_path, str) or not raw_rel_path or len(raw_rel_path) > 4096:
        raise ValueError("Invalid track path")
    if base_dir.is_symlink():
        raise ValueError("Music root must not be a symlink")
    value = raw_rel_path.replace("\\", "/")
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Unsafe track path")
    pieces = []
    for part in value.split("/"):
        cleaned = "".join(character if character.isprintable() and character not in ':*?"<>|' else "_" for character in part).rstrip(". ")
        if not cleaned:
            raise ValueError("Invalid track filename")
        while len(os.fsencode(cleaned)) > 240:
            cleaned = cleaned[:-1]
        pieces.append(cleaned)
    path = base_dir.resolve().joinpath(*pieces)
    current = base_dir.resolve()
    for part in pieces:
        current = current / part
        if current.is_symlink():
            raise ValueError("Track paths must not contain symlinks")
    return path


class AntraWorker:
    """Background download worker orchestrating jobs, batches, and files."""

    def __init__(
        self,
        store: Optional[AntraStore] = None,
        client: Optional[AntraClient] = None,
        output_roots: Optional[Dict[str, Path]] = None,
        lock_file: Optional[Path] = None,
        is_test_env: bool = False,
    ):
        self.store = store or AntraStore()
        self.client = client or AntraClient()
        self.output_roots = (
            {k: Path(v) for k, v in output_roots.items()}
            if output_roots is not None
            else dict(DEFAULT_OUTPUT_ROOTS)
        )
        self.lock_file = Path(lock_file) if lock_file is not None else DEFAULT_LOCK_FILE
        self.is_test_env = is_test_env
        self._lock_fd: Optional[int] = None

    def acquire_lock(self) -> bool:
        """Acquire singleton process execution lock."""
        self.lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = None
        try:
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except (OSError, BlockingIOError):
            if fd is not None:
                os.close(fd)
            return False

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def check_output_storage(self, destination: str = "private") -> Tuple[bool, str]:
        """Validate fixed output roots and the narrowed live family scope."""
        if destination not in self.output_roots:
            return False, "Invalid music destination."
        if not shutil.which("ffmpeg"):
            return False, "Audio verifier is not installed."
        root = self.output_roots[destination]
        if root.is_symlink() or not root.is_dir():
            return False, "Music destination has not been provisioned."
        if not self.is_test_env:
            expected = DEFAULT_OUTPUT_ROOTS[destination]
            family_link = Path.home() / "family_share/Music"
            if root != expected or root.parent.is_symlink():
                return False, "Music destination does not match the reviewed layout."
            if not family_link.is_symlink() or family_link.resolve() != DEFAULT_OUTPUT_ROOTS["family"].resolve():
                return False, "Family music isolation is not configured."
            if any(path.is_symlink() or not path.is_dir() for path in self.output_roots.values()):
                return False, "Music isolation roots are unavailable."
        try:
            if shutil.disk_usage(root).free < MIN_FREE_SPACE_BYTES:
                return False, "Insufficient free storage."
        except OSError:
            return False, "Music storage is unavailable."
        return True, "Storage OK"

    def _backoff(self, job_id, error, *, daily=False):
        job = self.store.get_internal_job(job_id)
        if not job or job.get("status") == "cancelled" or self.store.is_job_cancelled(job_id):
            return

        is_429 = isinstance(error, ConcurrencyLimitError)
        is_daily = daily or isinstance(error, DailyLimitError)
        attempts = int(job.get("retry_attempts", 0))

        if not (is_429 or is_daily):
            attempts += 1
            if attempts >= 10:
                records = self.store.get_tracks(job_id)
                completed = sum(t["status"] == "completed" for t in records)
                skipped = sum(t["status"] == "skipped" for t in records)
                state = "partial" if (completed or skipped) else "failed"
                final_msg = f"Connection failed after {attempts} retry attempts. Manual retry is available."
                self.store.finish_job(job_id, status=state, message=final_msg)
                return

        delay = getattr(error, "retry_after", None) or (86402 - time.time() % 86400 if is_daily else (30 if is_429 else min(60, 5 * attempts)))
        msg = ("Daily quota reached; waiting for reset." if is_daily
               else ("Provider busy; waiting for capacity." if is_429
                     else "Connection interrupted; retrying the existing job."))

        self.store.update_job(
            job_id, status="blocked" if is_daily else ("downloading" if job.get("upstream_job_id") else "queued"),
            concurrency_retry_after=time.time() + max(2, min(float(delay), 86400)),
            retry_attempts=attempts,
            message=msg,
        )

    def process_job(self, job: Dict[str, Any]) -> None:
        """Keep one folder-mode upstream job for the whole playlist, including after restart."""
        job_id = job["id"]
        job = self.store.get_internal_job(job_id)
        if not job or job["status"] == "cancelled" or job.get("removed"):
            return
        if job.get("duplicate_count") and not job.get("duplicate_confirmed"):
            return
        if job.get("submission_intent") and not job.get("upstream_job_id"):
            self.store.update_job(job_id, status="uncertain", message="Submission was not confirmed. Automatic resubmission is disabled.")
            return
        if not self.client.has_credentials():
            self.store.update_job(job_id, status="blocked", message="Antra account is not configured.")
            self.store.set_worker_status("needs_setup", "Antra account is not configured.")
            return
        destination = job["destination"]
        ready, message = self.check_output_storage(destination)
        if not ready:
            self.store.update_job(job_id, status="blocked", message=message)
            return
        self.store.set_worker_status("running", "Downloading music")
        try:
            tracks = self.store.get_tracks(job_id)
            if not tracks:
                self.store.update_job(job_id, status="resolving")
                resolved = self.client.resolve(job["url"])
                if not resolved["tracks"]:
                    self.store.finish_job(job_id, status="failed", message="The provider returned no tracks.")
                    return
                if self.store.is_job_cancelled(job_id):
                    return
                tracks_with_dups = self.store.check_track_duplicates(job_id, destination, resolved["tracks"])
                self.store.set_tracks(job_id, tracks_with_dups)
                dup_count = sum(1 for t in tracks_with_dups if "duplicate" in t.get("message", "").lower())
                job_msg = f"Detected {dup_count} duplicate track(s) in {destination} destination." if dup_count else ""
                self.store.update_job(job_id, title=resolved["title"], message=job_msg, duplicate_count=dup_count)
                tracks = self.store.get_tracks(job_id)
                if dup_count and not job.get("duplicate_confirmed"):
                    self.store.update_job(
                        job_id,
                        status="blocked",
                        concurrency_retry_after=0,
                        message=f"Blocked: playlist contains {dup_count} duplicate track(s). Continue to download anyway.",
                    )
                    return
            if tracks and all(track["status"] in ("completed", "skipped") for track in tracks):
                self.store.finish_job(job_id, status="completed" if all(track["status"] == "completed" for track in tracks) else "partial", message="")
                return
            upstream = job.get("upstream_job_id")
            if upstream:
                upstream_status = self.client.get_job_status(upstream)
                if isinstance(upstream_status, dict) and upstream_status.get("status") in ("failed", "cancelled"):
                    raise AntraClientError("Provider ended this job.", status=400)
                token = self.client.fresh_ws_token(upstream)
            else:
                if self.store.is_job_cancelled(job_id):
                    return
                if job.get("automatic_retry_rounds", 0) or any(t["status"] in ("completed", "skipped") for t in tracks):
                    refreshed = self.client.resolve(job["url"])
                    expected = [(t["title"], t["artist"]) for t in tracks]
                    actual = [(t["title"], t["artist"]) for t in refreshed["tracks"]]
                    if actual != expected:
                        self.store.finish_job(job_id, "failed", "Playlist contents changed. Queue a new download; completed files were preserved.")
                        return
                self.store.update_job(job_id, submission_intent=1, chunk_start=0, chunk_end=len(tracks))
                upstream, token = self.client.submit_job(job["url"], start_index=0, end_index=len(tracks))
                self.store.update_job(job_id, upstream_job_id=upstream, submission_intent=0)
                if not token:
                    token = self.client.fresh_ws_token(upstream)
            if self.store.is_job_cancelled(job_id):
                return
            self.store.update_job(job_id, status="downloading", concurrency_retry_after=0)
            complete = False
            for event in self.client.connect_job_ws(upstream, token):
                if self.store.is_job_cancelled(job_id):
                    return
                if not isinstance(event, dict):
                    continue
                kind, index = event.get("type"), event.get("index")
                if kind == "complete":
                    complete = True
                    break
                if kind == "error":
                    raise AntraClientError("Provider reported a download error.")
                if kind not in ("track_done", "track_failed"):
                    continue
                if type(index) is not int or not 0 <= index < len(tracks):
                    raise AntraClientError("Provider returned an invalid track index.")
                track = self.store.get_internal_track(job_id, index)
                if kind == "track_done":
                    self._download_and_publish_track(
                        job_id, self.output_roots[destination], upstream, index, index,
                        event.get("path") or f"track_{index + 1:03d}.flac",
                    )
                elif track["status"] not in ("completed", "skipped"):
                    self.store.update_track(job_id, index, status="failed", message="Provider could not download this track.")
            if not complete:
                raise AntraClientError("Download event stream ended before completion.")
            records = self.store.get_tracks(job_id)
            for record in records:
                if record["status"] in ("queued", "downloading"):
                    self.store.update_track(job_id, record["index"], status="failed", message="Provider completed without delivering this track.")
            records = self.store.get_tracks(job_id)
            completed = sum(t["status"] == "completed" for t in records)
            skipped = sum(t["status"] == "skipped" for t in records)
            failed = len(records) - completed - skipped

            if self.store.is_job_cancelled(job_id):
                return

            if failed == 0:
                state = "completed" if completed == len(records) else "partial"
                self.store.finish_job(job_id, status=state, message="")
                return

            current = self.store.get_internal_job(job_id)
            if not current or current.get("status") == "cancelled" or self.store.is_job_cancelled(job_id):
                return

            rounds = int(current.get("automatic_retry_rounds", 0))
            if rounds < MAX_AUTOMATIC_RETRY_ROUNDS:
                next_round = rounds + 1
                delay = min(60.0, 10.0 * next_round)
                msg = f"Retrying {failed} failed track{'s' if failed != 1 else ''} (pass {next_round} of {MAX_AUTOMATIC_RETRY_ROUNDS})."
                if self.store.schedule_automatic_retry(job_id, delay=delay, round_number=next_round, message=msg):
                    return

            state = "partial" if (completed or skipped) else "failed"
            final_msg = f"Completed with {failed} failed track{'s' if failed != 1 else ''} after {MAX_AUTOMATIC_RETRY_ROUNDS} retry passes. Manual retry is available."
            self.store.finish_job(job_id, status=state, message=final_msg)
        except UncertainSubmissionError:
            self.store.update_job(job_id, status="uncertain", message="Submission was not confirmed. Automatic resubmission is disabled.")
        except DailyLimitError as error:
            self.store.update_job(job_id, submission_intent=0)
            self._backoff(job_id, error, daily=True)
        except ConcurrencyLimitError as error:
            self.store.update_job(job_id, submission_intent=0)
            self._backoff(job_id, error)
        except MissingDependencyError:
            self.store.update_job(job_id, status="blocked", message="Download transport is not installed.")
        except AntraClientError as error:
            if getattr(error, "status", None) in (400, 401, 403, 404):
                records = self.store.get_tracks(job_id)
                completed = sum(t["status"] == "completed" for t in records)
                skipped = sum(t["status"] == "skipped" for t in records)
                state = "partial" if (completed or skipped) else "failed"
                self.store.finish_job(
                    job_id,
                    status=state,
                    message="Provider rejected or expired this job. Completed files were preserved.",
                )
                self.store.update_job(job_id, submission_intent=0)
            else:
                self._backoff(job_id, error)
        except Exception:
            current = self.store.get_internal_job(job_id)
            if current.get("submission_intent") and not current.get("upstream_job_id"):
                self.store.update_job(job_id, status="uncertain", message="Submission was not confirmed. Automatic resubmission is disabled.")
            else:
                self._backoff(job_id, AntraClientError("Download interrupted."))

    def _acknowledge(self, job_id, index, upstream):
        if self.store.is_job_cancelled(job_id):
            self.store.update_track(job_id, index, ack_pending=0)
            return
        try:
            acknowledged = self.client.ack_track(upstream, index)
        except Exception:
            acknowledged = False
        self.store.update_track(job_id, index, ack_pending=0 if acknowledged else 1, ack_retry_after=time.time() + 30)

    def _download_and_publish_track(self, job_id, destination_root, upstream_job_id, upstream_index, global_index, rel_path):
        """Publish only verified audio; durable checkpoints recover rename-before-commit interruptions."""
        record = self.store.get_internal_track(job_id, global_index)
        if not record or self.store.is_job_cancelled(job_id):
            return
        if record["status"] == "skipped":
            self._acknowledge(job_id, global_index, upstream_job_id)
            return
        temporary = None
        temporary_identity = None
        publication_directory = None
        directory_publication = False
        try:
            if not self.check_output_storage(self.store.get_job(job_id)["destination"])[0]:
                raise ValueError("Music isolation is unavailable")
            if record["status"] == "completed" and not record.get("publication_path"):
                record["publication_path"] = record.get("filename")
                if not record["publication_path"]:
                    raise ValueError("Completed track has no owned file checkpoint")
            if record.get("publication_path"):
                final = sanitize_track_path(record["publication_path"], destination_root)
                ensure_track_parent(final, destination_root)
                if final.exists() or final.is_symlink():
                    if not record.get("file_sha256") or final.is_symlink() or file_digest(final) != record["file_sha256"]:
                        raise ValueError("Existing file differs from the owned checkpoint")
                    if self.store.is_job_cancelled(job_id):
                        return
                    self.store.update_track(job_id, global_index, status="completed", filename=record["publication_path"], ack_pending=1)
                    self._acknowledge(job_id, global_index, upstream_job_id)
                    return
            supplied = sanitize_track_path(rel_path, destination_root)
            stem = os.fsencode(supplied.stem)[:120].decode("utf-8", errors="ignore")
            final = supplied.with_name(f"{stem}--{job_id[:12]}-{global_index}.flac")
            ensure_track_parent(final, destination_root)
            while final.exists() or final.is_symlink():
                final = final.with_name(f"{stem}--{uuid.uuid4().hex}.flac")
            self.store.update_track(job_id, global_index, publication_path=str(final.relative_to(destination_root.resolve())))
            if record.get("temporary_path"):
                temporary = sanitize_track_path(record["temporary_path"], destination_root)
                if temporary.exists() or temporary.is_symlink():
                    identity = temporary.lstat()
                    if not temporary.is_symlink() and (identity.st_dev, identity.st_ino) == (record.get("temporary_dev"), record.get("temporary_ino")):
                        temporary.unlink()
                    # shortcut: a crash before inode journaling may leave an unclaimed .part; remove only after ownership review.
            temporary = final.parent / f".antra-{uuid.uuid4().hex}.part"
            self.store.update_track(job_id, global_index, status="downloading", temporary_path=str(temporary.relative_to(destination_root.resolve())))
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            written = 0
            identity = os.fstat(descriptor)
            temporary_identity = (identity.st_dev, identity.st_ino)
            with os.fdopen(descriptor, "wb") as output, contextlib.closing(self.client.download_track_stream(upstream_job_id, upstream_index)) as response:
                self.store.update_track(job_id, global_index, temporary_dev=identity.st_dev, temporary_ino=identity.st_ino)
                raw_length = getattr(response, "headers", {}).get("Content-Length")
                length = int(raw_length) if raw_length is not None else None
                if length is not None and (length < 0 or length > MAX_TRACK_BYTES):
                    raise ValueError("Audio exceeds the supported track size")
                while True:
                    if self.store.is_job_cancelled(job_id):
                        return
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_TRACK_BYTES:
                        raise ValueError("Audio exceeds the supported track size")
                    if written % (4 * 1024 * 1024) < len(chunk) and shutil.disk_usage(destination_root).free < MIN_FREE_SPACE_BYTES + len(chunk):
                        raise ValueError("Insufficient free storage")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            valid, bits, rate, message = verify_flac(temporary, expected_length=length)
            if not valid:
                self.store.update_track(job_id, global_index, status="failed", bit_depth=bits, sample_rate=rate, message=message, ack_pending=0)
                return
            self.store.update_track(job_id, global_index, file_sha256=digest.hexdigest(), file_size=written, bit_depth=bits, sample_rate=rate)
            if self.store.is_job_cancelled(job_id):
                return
            ensure_track_parent(final, destination_root)
            try:
                publish_noreplace(temporary, final)
            except OSError as error:
                if error.errno not in (22, 38, 95):
                    raise
                # Android FUSE lacks RENAME_NOREPLACE. A nonempty directory rename cannot overwrite existing files.
                publication_directory = temporary.parent / f".antra-commit-{uuid.uuid4().hex}"
                publication_directory.mkdir(mode=0o700)
                target_directory = final.with_suffix("")
                while os.path.lexists(target_directory):
                    target_directory = final.parent / f"{final.stem}--{uuid.uuid4().hex}"
                staged_file = publication_directory / final.name
                target_file = target_directory / final.name
                self.store.update_track(
                    job_id, global_index,
                    publication_path=str(target_file.relative_to(destination_root.resolve())),
                    temporary_path=str(staged_file.relative_to(destination_root.resolve())),
                )
                os.rename(temporary, staged_file)
                temporary = staged_file
                os.rename(publication_directory, target_directory)
                final = target_file
                directory_publication = True
                sync_track_directory(target_directory.parent)
            temporary = None
            sync_track_directory(final.parent)
            if self.store.is_job_cancelled(job_id):
                identity = final.lstat()
                if (identity.st_dev, identity.st_ino) == temporary_identity and not final.is_symlink():
                    final.unlink()
                    if directory_publication:
                        with contextlib.suppress(OSError):
                            final.parent.rmdir()
                return
            existing_msg = record.get("message") or ""
            self.store.update_track(job_id, global_index, status="completed", filename=str(final.relative_to(destination_root.resolve())), temporary_path=None, message=existing_msg, ack_pending=1)
            self._acknowledge(job_id, global_index, upstream_job_id)
        except Exception:
            if not self.store.is_job_cancelled(job_id):
                self.store.update_track(job_id, global_index, status="failed", message="Track could not be verified or saved. Existing files were preserved.")
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    identity = temporary.lstat()
                    if not temporary.is_symlink() and (identity.st_dev, identity.st_ino) == temporary_identity:
                        temporary.unlink()
            if publication_directory is not None:
                with contextlib.suppress(OSError):
                    publication_directory.rmdir()

    def run_once(self) -> bool:
        """Process one runnable job if available."""
        for record in self.store.pending_acknowledgements():
            try:
                self._acknowledge(record["job_id"], record["track_index"], record["upstream_job_id"])
            except Exception:
                logger.warning("Ack failed for job %s track %s", record["job_id"], record["track_index"])
        job = self.store.get_next_runnable_job()
        if not job:
            return False
        self.process_job(job)
        return True

    def run_forever(self, poll_interval: float = 2.0) -> None:
        """Main loop executed by supervised worker process."""
        if not self.acquire_lock():
            logger.info("Another Antra worker is already active; exiting.")
            return

        try:
            while True:
                if not self.client.has_credentials():
                    self.store.set_worker_status("needs_setup", "Missing credentials.json")
                    time.sleep(poll_interval * 2)
                    continue

                processed = self.run_once()
                if not processed:
                    self.store.set_worker_status("idle", "Waiting for jobs")
                time.sleep(poll_interval)
        finally:
            self.release_lock()


def main() -> None:
    maybe_reexec_dns_fix()
    worker = AntraWorker()
    worker.run_forever()


if __name__ == "__main__":
    main()
