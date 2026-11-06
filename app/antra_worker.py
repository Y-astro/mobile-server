"""Antra background download worker.

Supervised worker process for durable Antra music downloads.
Handles singleton execution lock, Termux DNS fix re-exec,
batching/continuation for playlists > 100 tracks, FLAC 24-bit validation,
streaming path traversal protection, atomic publication, and acknowledgements.
Nothing starts on import; main loop executes under __main__ or run_forever().
"""
import fcntl
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

from antra_store import AntraStore, DEFAULT_OUTPUT_ROOTS
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


def verify_flac_24bit(
    file_path: Path,
    expected_length: Optional[int] = None,
) -> Tuple[bool, Optional[int], Optional[int], str]:
    """Inspect FLAC STREAMINFO header to verify exact 24-bit depth, sensible sample rate,

    and ensure actual audio frames exist past metadata.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError as e:
        return False, None, None, f"Failed to inspect file size: {e}"

    if file_size < 42:
        return False, None, None, "File too small to be a valid FLAC stream"

    if expected_length is not None and file_size != expected_length:
        return False, None, None, f"Downloaded size {file_size} does not match Content-Length {expected_length}"

    try:
        with open(file_path, "rb") as f:
            data = f.read(min(file_size, 256 * 1024))
    except OSError as e:
        return False, None, None, f"Failed to read audio file: {e}"

    if len(data) < 42 or data[:4] != b"fLaC":
        return False, None, None, "Invalid audio format: missing FLAC marker"

    offset = 4
    is_last = False
    streaminfo = None

    while offset + 4 <= len(data) and not is_last:
        header = data[offset:offset + 4]
        is_last = bool(header[0] & 0x80)
        block_type = header[0] & 0x7F
        block_len = int.from_bytes(header[1:4], "big")
        offset += 4

        if offset + block_len > len(data):
            return False, None, None, "Truncated FLAC metadata block"

        block_data = data[offset:offset + block_len]
        offset += block_len

        if block_type == 0:
            if block_len != 34:
                return False, None, None, f"Corrupted FLAC STREAMINFO length: {block_len} != 34"
            streaminfo = block_data

    if streaminfo is None:
        return False, None, None, "Missing FLAC STREAMINFO metadata block"

    info_bytes = int.from_bytes(streaminfo[10:18], "big")
    sample_rate = (info_bytes >> 44) & 0xFFFFF
    channels = ((info_bytes >> 41) & 0x7) + 1
    bits_per_sample = ((info_bytes >> 36) & 0x1F) + 1

    if sample_rate == 0 or sample_rate < 8000 or sample_rate > 384000:
        return False, bits_per_sample, sample_rate, f"Invalid or zero sample rate: {sample_rate} Hz"

    # Audio frames must exist past the last metadata block
    if file_size <= offset or (file_size - offset) < 16:
        return False, None, None, "Header-only FLAC stream: missing audio frames"

    if bits_per_sample != 24:
        return (
            False,
            bits_per_sample,
            sample_rate,
            f"Skipped: {bits_per_sample}-bit audio ({sample_rate} Hz, {channels} ch); 24-bit FLAC required",
        )

    return (
        True,
        24,
        sample_rate,
        f"Verified 24-bit FLAC ({sample_rate} Hz, {channels} ch)",
    )


def sanitize_track_path(raw_rel_path: str, base_dir: Path) -> Path:
    """Sanitize remote path segments and enforce containment strictly inside base_dir."""
    base_resolved = base_dir.resolve()
    clean = str(raw_rel_path).strip().replace("\\", "/")
    segments = [s.strip() for s in clean.split("/") if s.strip()]

    safe_segments = []
    for seg in segments:
        s = "".join(c for c in seg if 31 < ord(c) < 127 or ord(c) >= 128)
        for bad in ('<', '>', ':', '"', '|', '?', '*', '\\'):
            s = s.replace(bad, "_")
        s = s.rstrip(". ")
        if not s or s in (".", ".."):
            s = "_"
        safe_segments.append(s[:180])

    if not safe_segments:
        safe_segments = [f"track_{uuid.uuid4().hex[:8]}.flac"]

    dest = base_resolved
    for part in safe_segments:
        dest = dest / part

    try:
        dest_resolved = dest.resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"Invalid path traversal: {e}")

    if not dest_resolved.is_relative_to(base_resolved):
        raise ValueError("Path traversal escape rejected")

    return dest_resolved


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
        try:
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except (OSError, BlockingIOError):
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
        """Verify pre-provisioned root exists (fail closed in live layout) and has free space."""
        root = self.output_roots.get(destination, self.output_roots["private"])

        if not self.is_test_env:
            # Live runtime: MUST fail closed if pre-provisioned root is missing
            if not root.is_dir() or root.is_symlink():
                return False, f"Pre-provisioned destination directory missing: {destination} ({root})"
        else:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return False, f"Destination directory not writable: {e}"

        try:
            usage = shutil.disk_usage(root)
            if usage.free < MIN_FREE_SPACE_BYTES:
                return False, f"Insufficient disk space: {usage.free // (1024 * 1024)} MiB free"
        except OSError as e:
            return False, f"Cannot check disk space: {e}"

        return True, "Storage OK"

    def process_job(self, job: Dict[str, Any]) -> None:
        """Execute one job through resolution, chunking, downloading, and verification."""
        job_id = job["id"]
        url = job["url"]
        destination = job.get("destination", "private")

        # Check for unconfirmed submission intent from interrupted previous run
        if job.get("submission_intent") and not job.get("upstream_job_id"):
            self.store.update_job(
                job_id,
                status="uncertain",
                submission_intent=0,
                message="Submission interrupted without confirmed upstream ID; manual decision required.",
            )
            return

        # Ensure credentials exist before starting network work
        if not self.client.has_credentials():
            self.store.set_worker_status("needs_setup", "Missing credentials.json")
            self.store.update_job(job_id, status="blocked", message="Missing Antra credentials")
            return

        ok_storage, storage_msg = self.check_output_storage(destination)
        if not ok_storage:
            self.store.set_worker_status("blocked", storage_msg)
            self.store.update_job(job_id, status="blocked", message=storage_msg)
            return

        self.store.set_worker_status("running", f"Processing job {job_id[:8]}")

        # 1. Resolve tracks if not already populated
        tracks = self.store.get_tracks(job_id)
        if not tracks:
            self.store.update_job(job_id, status="resolving")
            try:
                resolved = self.client.resolve(url)
                self.store.set_tracks(job_id, resolved["tracks"])
                self.store.update_job(
                    job_id,
                    title=resolved["title"],
                    total=resolved["total"],
                )
                tracks = self.store.get_tracks(job_id)
            except DailyLimitError as e:
                self.store.set_worker_status("blocked", "Daily limit reached")
                self.store.update_job(job_id, status="blocked", message=str(e))
                return
            except ConcurrencyLimitError as e:
                attempts = job.get("retry_attempts", 0) + 1
                delay = min(60.0, 5.0 * (1.5 ** attempts))
                self.store.update_job(
                    job_id,
                    status="queued",
                    concurrency_retry_after=time.time() + delay,
                    retry_attempts=attempts,
                    message=str(e),
                )
                return
            except Exception as e:
                self.store.update_job(job_id, status="failed", message=f"Resolve failed: {e}")
                return

        total_tracks = len(tracks)
        if total_tracks == 0:
            self.store.update_job(job_id, status="completed", message="Empty playlist")
            return

        self.store.update_job(job_id, status="downloading")

        # 2. Process in chunks of 100 tracks (0-based start, exclusive end)
        chunk_size = 100
        for chunk_start in range(0, total_tracks, chunk_size):
            chunk_end = min(chunk_start + chunk_size, total_tracks)
            chunk_tracks = [t for t in tracks if chunk_start <= t["index"] < chunk_end]
            if all(t["status"] == "completed" for t in chunk_tracks):
                continue

            upstream_job_id = None
            ws_token = None

            # Check if this job already has a confirmed upstream job ID for this chunk
            if job.get("upstream_job_id") and job.get("chunk_start") == chunk_start:
                upstream_job_id = job["upstream_job_id"]
                ws_token = self.client.fresh_ws_token(upstream_job_id)

            if not upstream_job_id or not ws_token:
                # Persist submission intent BEFORE POST
                self.store.update_job(
                    job_id,
                    chunk_start=chunk_start,
                    chunk_end=chunk_end,
                    submission_intent=1,
                )
                try:
                    upstream_job_id, ws_token = self.client.submit_job(
                        music_url=url,
                        start_index=chunk_start,
                        end_index=chunk_end,
                    )
                    self.store.update_job(
                        job_id,
                        upstream_job_id=upstream_job_id,
                        submission_intent=0,
                        retry_attempts=0,
                        concurrency_retry_after=0,
                    )
                except UncertainSubmissionError as e:
                    self.store.update_job(
                        job_id,
                        status="uncertain",
                        submission_intent=0,
                        message="Submission timed out without confirmed upstream ID; manual decision required.",
                    )
                    return
                except DailyLimitError as e:
                    self.store.set_worker_status("blocked", "Daily limit reached")
                    self.store.update_job(job_id, status="blocked", submission_intent=0, message=str(e))
                    return
                except ConcurrencyLimitError as e:
                    attempts = job.get("retry_attempts", 0) + 1
                    delay = min(60.0, 5.0 * (1.5 ** attempts))
                    self.store.update_job(
                        job_id,
                        status="queued",
                        submission_intent=0,
                        concurrency_retry_after=time.time() + delay,
                        retry_attempts=attempts,
                        message=str(e),
                    )
                    return
                except Exception as e:
                    self.store.update_job(job_id, status="failed", submission_intent=0, message=f"Submission failed: {e}")
                    return

            dest_root = self.output_roots.get(destination, self.output_roots["private"])

            # Listen to events and process tracks
            try:
                for evt in self.client.connect_job_ws(upstream_job_id, ws_token):
                    if self.store.is_job_cancelled(job_id):
                        return

                    evt_type = evt.get("type")
                    if evt_type == "track_done":
                        raw_idx = evt.get("index", 0)
                        if type(raw_idx) is not int or isinstance(raw_idx, bool):
                            continue
                        global_idx = raw_idx if raw_idx >= chunk_start else chunk_start + raw_idx
                        if global_idx >= total_tracks:
                            continue

                        rel_path = evt.get("path") or f"track_{global_idx + 1:02d}.flac"
                        self._download_and_publish_track(
                            job_id=job_id,
                            destination_root=dest_root,
                            upstream_job_id=upstream_job_id,
                            upstream_index=raw_idx,
                            global_index=global_idx,
                            rel_path=rel_path,
                        )
                    elif evt_type == "track_failed":
                        raw_idx = evt.get("index", 0)
                        if type(raw_idx) is int and not isinstance(raw_idx, bool):
                            global_idx = raw_idx if raw_idx >= chunk_start else chunk_start + raw_idx
                            err_msg = str(evt.get("error", "Track download failed"))
                            self.store.update_track(job_id, global_idx, status="failed", message=err_msg)
            except MissingDependencyError as e:
                self.store.set_worker_status("needs_setup", str(e))
                self.store.update_job(job_id, status="blocked", message=str(e))
                return
            except Exception as e:
                self.store.update_job(job_id, message=f"Event stream interrupted: {e}")
                return

            tracks = self.store.get_tracks(job_id)

        # 3. Finalize job status
        latest = self.store.get_job(job_id)
        if latest and latest["status"] not in ("cancelled", "uncertain", "blocked"):
            all_tracks = self.store.get_tracks(job_id)
            comp = sum(1 for t in all_tracks if t["status"] == "completed")
            fail = sum(1 for t in all_tracks if t["status"] == "failed")
            skip = sum(1 for t in all_tracks if t["status"] == "skipped")
            tot = len(all_tracks)

            if comp == tot:
                fin_status = "completed"
            elif comp > 0:
                fin_status = "partial"
            elif fail > 0:
                fin_status = "failed"
            elif skip == tot and tot > 0:
                fin_status = "partial"
            else:
                fin_status = "failed"

            self.store.update_job(job_id, status=fin_status)

    def _download_and_publish_track(
        self,
        job_id: str,
        destination_root: Path,
        upstream_job_id: str,
        upstream_index: int,
        global_index: int,
        rel_path: str,
    ) -> None:
        """Download track, verify 24-bit FLAC, atomically publish with no-clobber, and acknowledge."""
        self.store.update_track(job_id, global_index, status="downloading")

        try:
            dest_file = sanitize_track_path(rel_path, destination_root)
            dest_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        except ValueError as e:
            self.store.update_track(
                job_id,
                global_index,
                status="failed",
                message=f"Invalid track destination path: {e}",
            )
            return

        # Collision-safe no-clobber: if dest_file already exists on disk
        if dest_file.exists():
            existing_tracks = self.store.get_tracks(job_id)
            match_trk = next((t for t in existing_tracks if t["index"] == global_index), None)
            if match_trk and match_trk["status"] == "completed" and match_trk.get("filename") == str(dest_file.relative_to(destination_root.resolve())):
                # Already verified completed track for this job
                self.client.ack_track(upstream_job_id, upstream_index)
                return
            # Collision with unowned file: do NOT overwrite! Use collision-safe name
            stem = dest_file.stem
            suffix = dest_file.suffix
            dest_file = dest_file.parent / f"{stem}_c{uuid.uuid4().hex[:6]}{suffix}"

        tmp_file = dest_file.parent / f".tmp_{uuid.uuid4().hex}.flac"

        try:
            stream_resp = self.client.download_track_stream(upstream_job_id, upstream_index)
            content_length = None
            if hasattr(stream_resp, "headers") and stream_resp.headers:
                cl_header = stream_resp.headers.get("Content-Length")
                if cl_header and cl_header.isdigit():
                    content_length = int(cl_header)

            bytes_written = 0
            with open(tmp_file, "wb") as out_f:
                while True:
                    if self.store.is_job_cancelled(job_id):
                        out_f.close()
                        tmp_file.unlink(missing_ok=True)
                        return

                    chunk = stream_resp.read(64 * 1024)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    bytes_written += len(chunk)

                out_f.flush()
                os.fsync(out_f.fileno())

            if hasattr(stream_resp, "close"):
                stream_resp.close()
        except Exception as e:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            self.store.update_track(
                job_id,
                global_index,
                status="failed",
                message=f"Download failed: {e}",
            )
            return

        # FLAC 24-bit verification
        valid, bit_depth, sample_rate, msg = verify_flac_24bit(tmp_file, expected_length=content_length)
        if not valid:
            tmp_file.unlink(missing_ok=True)
            new_status = "skipped" if bit_depth == 16 else "failed"
            self.store.update_track(
                job_id,
                global_index,
                status=new_status,
                bit_depth=bit_depth,
                sample_rate=sample_rate,
                message=msg,
            )
            self.client.ack_track(upstream_job_id, upstream_index)
            return

        # Atomic no-clobber rename + directory fsync
        try:
            os.replace(tmp_file, dest_file)
            try:
                os.chmod(dest_file, 0o644)
            except OSError:
                pass

            parent_fd = os.open(str(dest_file.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as e:
            tmp_file.unlink(missing_ok=True)
            self.store.update_track(
                job_id,
                global_index,
                status="failed",
                message=f"Failed to publish track file: {e}",
            )
            return

        calc_rel = str(dest_file.relative_to(destination_root.resolve()))
        self.store.update_track(
            job_id,
            global_index,
            status="completed",
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            filename=calc_rel,
            message="",
        )

        # Acknowledge to upstream; mark ack_pending if failed
        acked = self.client.ack_track(upstream_job_id, upstream_index)
        if not acked:
            self.store.update_track(job_id, global_index, ack_pending=1)

    def run_once(self) -> bool:
        """Process one runnable job if available."""
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
