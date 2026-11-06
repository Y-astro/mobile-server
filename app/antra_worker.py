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

# Ensure local lib directory is on path for installed layout
MOBILE_LIB = os.path.expanduser("~/.local/lib/mobile-server")
if os.path.isdir(MOBILE_LIB) and MOBILE_LIB not in sys.path:
    sys.path.insert(0, MOBILE_LIB)
elif str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from antra_store import AntraStore, DEFAULT_OUTPUT_DIR
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

    # Check for real Termux filesystem
    if not os.path.isdir("/data/data/com.termux"):
        return False

    shim_path = Path(PINNED_LIBDNS_PATH)
    if not shim_path.is_file():
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


def verify_flac_24bit(file_path: Path) -> Tuple[bool, Optional[int], Optional[int], str]:
    """Inspect FLAC STREAMINFO header to verify exact 24-bit depth and sample rate.

    Returns:
        (is_valid_24bit, bits_per_sample, sample_rate, message)
    """
    try:
        with open(file_path, "rb") as f:
            header = f.read(42)
    except OSError as e:
        return False, None, None, f"Failed to read audio file: {e}"

    if len(header) < 42:
        return False, None, None, "File too small to be a valid FLAC stream"

    if header[:4] != b"fLaC":
        return False, None, None, "Invalid audio format: missing FLAC marker"

    # Block header check: block_type must be 0 (STREAMINFO)
    block_type = header[4] & 0x7F
    if block_type != 0:
        return False, None, None, f"Unexpected initial FLAC metadata block type: {block_type}"

    length = int.from_bytes(header[5:8], "big")
    if length < 34:
        return False, None, None, f"Corrupted FLAC STREAMINFO length: {length}"

    # STREAMINFO bytes 10-17 (8 bytes)
    info_bytes = int.from_bytes(header[18:26], "big")
    sample_rate = (info_bytes >> 44) & 0xFFFFF
    channels = ((info_bytes >> 41) & 0x7) + 1
    bits_per_sample = ((info_bytes >> 36) & 0x1F) + 1

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
        # Strip ASCII control characters
        s = "".join(c for c in seg if 31 < ord(c) < 127 or ord(c) >= 128)
        # Replace illegal filesystem characters
        for bad in ('<', '>', ':', '"', '|', '?', '*', '\\'):
            s = s.replace(bad, "_")
        # Strip trailing dots and spaces
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

    # Enforce strict containment
    if not dest_resolved.is_relative_to(base_resolved):
        raise ValueError("Path traversal escape rejected")

    return dest_resolved


class AntraWorker:
    """Background download worker orchestrating jobs, batches, and files."""

    def __init__(
        self,
        store: Optional[AntraStore] = None,
        client: Optional[AntraClient] = None,
        output_dir: Optional[Path] = None,
        lock_file: Optional[Path] = None,
    ):
        self.store = store or AntraStore()
        self.client = client or AntraClient()
        self.output_dir = Path(output_dir) if output_dir is not None else self.store.output_dir
        self.lock_file = Path(lock_file) if lock_file is not None else DEFAULT_LOCK_FILE
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

    def check_output_storage(self) -> Tuple[bool, str]:
        """Check if output directory exists or parent is writable, with sufficient free space."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"Output directory not writable: {e}"

        try:
            usage = shutil.disk_usage(self.output_dir)
            if usage.free < MIN_FREE_SPACE_BYTES:
                return False, f"Insufficient disk space: {usage.free // (1024 * 1024)} MiB free"
        except OSError as e:
            return False, f"Cannot check disk space: {e}"

        return True, "Storage OK"

    def process_job(self, job: Dict[str, Any]) -> None:
        """Execute one job through resolution, chunking, downloading, and verification."""
        job_id = job["id"]
        url = job["url"]

        # Ensure credentials exist before starting network work
        if not self.client.has_credentials():
            self.store.set_worker_status("needs_setup", "Missing credentials.json")
            self.store.update_job(job_id, status="blocked", message="Missing Antra credentials")
            return

        ok_storage, storage_msg = self.check_output_storage()
        if not ok_storage:
            self.store.set_worker_status("blocked", storage_msg)
            self.store.update_job(job_id, status="blocked", message=storage_msg)
            return

        self.store.set_worker_status("running", f"Processing job {job_id[:8]}")
        self.store.update_job(job_id, status="resolving")

        # 1. Resolve tracks if not already populated
        tracks = self.store.get_tracks(job_id)
        if not tracks:
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
                self.store.update_job(job_id, status="queued", message=str(e))
                return
            except Exception as e:
                self.store.update_job(job_id, status="failed", message=f"Resolve failed: {e}")
                return

        total_tracks = len(tracks)
        if total_tracks == 0:
            self.store.update_job(job_id, status="completed", message="Empty playlist")
            return

        self.store.update_job(job_id, status="downloading")

        # 2. Process in chunks of 100 tracks (preserving indexing past 100)
        chunk_size = 100
        for chunk_start in range(0, total_tracks, chunk_size):
            chunk_end = min(chunk_start + chunk_size, total_tracks)
            # Check if all tracks in this chunk are already completed
            chunk_tracks = [t for t in tracks if chunk_start <= t["index"] < chunk_end]
            if all(t["status"] == "completed" for t in chunk_tracks):
                continue

            # Submit chunk (1-based indices to upstream)
            start_idx = chunk_start + 1
            end_idx = chunk_end
            try:
                upstream_job_id, ws_token = self.client.submit_job(
                    music_url=url,
                    start_index=start_idx,
                    end_index=end_idx,
                )
                self.store.update_job(job_id, upstream_job_id=upstream_job_id)
            except UncertainSubmissionError as e:
                # Ambiguous submission with no confirmed job_id: DO NOT duplicate/auto-retry!
                self.store.update_job(
                    job_id,
                    status="uncertain",
                    message="Submission timed out without confirmed upstream ID; manual decision required.",
                )
                return
            except DailyLimitError as e:
                self.store.set_worker_status("blocked", "Daily limit reached")
                self.store.update_job(job_id, status="blocked", message=str(e))
                return
            except ConcurrencyLimitError as e:
                # Keep queued for next run
                self.store.update_job(job_id, status="queued", message=str(e))
                return
            except Exception as e:
                self.store.update_job(job_id, status="failed", message=f"Submission failed: {e}")
                return

            # Listen to events and process tracks
            try:
                for evt in self.client.connect_job_ws(upstream_job_id, ws_token):
                    # Check for cancellation
                    curr = self.store.get_job(job_id)
                    if curr and curr["status"] == "cancelled":
                        return

                    evt_type = evt.get("type")
                    if evt_type == "track_done":
                        raw_idx = evt.get("index", 0)
                        # Normalize global track index
                        if raw_idx >= chunk_start:
                            global_idx = raw_idx
                        else:
                            global_idx = chunk_start + raw_idx

                        rel_path = evt.get("path") or f"track_{global_idx + 1:02d}.flac"
                        self._download_and_publish_track(
                            job_id=job_id,
                            upstream_job_id=upstream_job_id,
                            upstream_index=raw_idx,
                            global_index=global_idx,
                            rel_path=rel_path,
                        )
                    elif evt_type == "track_failed":
                        raw_idx = evt.get("index", 0)
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

            # Re-read tracks for progress check
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
        upstream_job_id: str,
        upstream_index: int,
        global_index: int,
        rel_path: str,
    ) -> None:
        """Download track, verify 24-bit FLAC, atomically publish, and acknowledge."""
        self.store.update_track(job_id, global_index, status="downloading")

        try:
            dest_file = sanitize_track_path(rel_path, self.output_dir)
            dest_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        except ValueError as e:
            self.store.update_track(
                job_id,
                global_index,
                status="failed",
                message=f"Invalid track destination path: {e}",
            )
            return

        tmp_file = dest_file.parent / f".tmp_{uuid.uuid4().hex}.flac"

        try:
            stream_resp = self.client.download_track_stream(upstream_job_id, upstream_index)
            with open(tmp_file, "wb") as out_f:
                while True:
                    chunk = stream_resp.read(64 * 1024)
                    if not chunk:
                        break
                    out_f.write(chunk)
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
        valid, bit_depth, sample_rate, msg = verify_flac_24bit(tmp_file)
        if not valid:
            tmp_file.unlink(missing_ok=True)
            self.store.update_track(
                job_id,
                global_index,
                status="skipped",
                bit_depth=bit_depth,
                sample_rate=sample_rate,
                message=msg,
            )
            # Acknowledge reclaimed space upstream even for skipped tracks
            self.client.ack_track(upstream_job_id, upstream_index)
            return

        # Atomically publish to final destination
        try:
            os.replace(tmp_file, dest_file)
            try:
                os.chmod(dest_file, 0o644)
            except OSError:
                pass
        except OSError as e:
            tmp_file.unlink(missing_ok=True)
            self.store.update_track(
                job_id,
                global_index,
                status="failed",
                message=f"Failed to publish track file: {e}",
            )
            return

        # Track completed durably
        calc_rel = str(dest_file.relative_to(self.output_dir.resolve()))
        self.store.update_track(
            job_id,
            global_index,
            status="completed",
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            filename=calc_rel,
            message="",
        )

        # Durable write completed: acknowledge to upstream (fire-and-forget)
        self.client.ack_track(upstream_job_id, upstream_index)

    def run_once(self) -> bool:
        """Process one pending job if available."""
        job = self.store.get_next_queued_job()
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
    # 1. Termux DNS fix re-exec if running under Termux
    maybe_reexec_dns_fix()

    # 2. Start worker loop
    worker = AntraWorker()
    worker.run_forever()


if __name__ == "__main__":
    main()
