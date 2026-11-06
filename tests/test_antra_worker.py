"""Tests for Antra background download worker, FLAC validation, batching, and no-clobber files."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from antra_store import AntraStore
from antra_client import (
    AntraClient,
    DailyLimitError,
    ConcurrencyLimitError,
    UncertainSubmissionError,
    MissingCredentialsError,
)
from antra_worker import (
    AntraWorker,
    verify_flac_24bit,
    sanitize_track_path,
    maybe_reexec_dns_fix,
)


def generate_real_flac(out_path: Path, bits: int = 24, sample_rate: int = 48000) -> None:
    """Generate a tiny real FLAC audio file with audio frames using ffmpeg."""
    sample_fmt = "s32" if bits == 24 else "s16"
    cmd = [
        "/usr/bin/ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=1000:sample_rate={sample_rate}:duration=0.05",
        "-c:a", "flac",
        "-sample_fmt", sample_fmt,
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_header_only_flac(bits_per_sample: int = 24, sample_rate: int = 96000, channels: int = 2) -> bytes:
    """Generate a header-only FLAC stream with STREAMINFO but NO audio frames."""
    info_int = (sample_rate << 44) | ((channels - 1) << 41) | ((bits_per_sample - 1) << 36) | 1000
    streaminfo_body = b"\x00" * 10 + info_int.to_bytes(8, "big") + b"\x00" * 16
    # 0x80 means last metadata block
    header = b"fLaC" + bytes([0x80, 0, 0, len(streaminfo_body)]) + streaminfo_body
    return header


class TestAntraWorker(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "queue.sqlite3"
        self.output_roots = {
            "private": self.base_dir / "Music" / "Astro",
            "family": self.base_dir / "Music" / "Family",
        }
        for r in self.output_roots.values():
            r.mkdir(parents=True, exist_ok=True)

        self.config_dir = self.base_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.base_dir / "worker.lock"

        self.store = AntraStore(db_path=self.db_path, output_roots=self.output_roots)
        self.client = AntraClient(base_url="http://127.0.0.1:9999", config_dir=self.config_dir)
        self.worker = AntraWorker(
            store=self.store,
            client=self.client,
            output_roots=self.output_roots,
            lock_file=self.lock_file,
            is_test_env=True,
        )

    def tearDown(self):
        self.worker.release_lock()
        self.tmp_dir.cleanup()

    def _write_credentials(self, username="user", password="secret_password"):
        creds_file = self.config_dir / "credentials.json"
        with open(creds_file, "w", encoding="utf-8") as f:
            json.dump({"username": username, "password": password}, f)

    def test_singleton_lock(self):
        self.assertTrue(self.worker.acquire_lock())
        worker2 = AntraWorker(
            store=self.store,
            client=self.client,
            output_roots=self.output_roots,
            lock_file=self.lock_file,
            is_test_env=True,
        )
        self.assertFalse(worker2.acquire_lock())
        self.worker.release_lock()
        self.assertTrue(worker2.acquire_lock())
        worker2.release_lock()

    def test_missing_credentials_heartbeat(self):
        job = self.store.create_job("https://tidal.com/album/12345")
        self.worker.process_job(job)

        status = self.store.get_worker_status()
        self.assertEqual(status["state"], "needs_setup")
        self.assertIn("not configured", status["message"])

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "blocked")

    def test_termux_dns_fix_safety(self):
        self.assertFalse(maybe_reexec_dns_fix())
        with patch.dict(os.environ, {"ANTRA_DNS_FIXED": "1"}):
            self.assertFalse(maybe_reexec_dns_fix())

    def test_flac_24bit_real_and_corruption_rejections(self):
        # 1. Real 24-bit FLAC audio generated via ffmpeg
        real_24 = self.base_dir / "real_24bit.flac"
        generate_real_flac(real_24, bits=24, sample_rate=48000)
        valid, bps, rate, msg = verify_flac_24bit(real_24)
        self.assertTrue(valid)
        self.assertEqual(bps, 24)
        self.assertEqual(rate, 48000)
        self.assertIn("Verified 24-bit FLAC", msg)

        # 2. Real 16-bit FLAC rejected as skipped
        real_16 = self.base_dir / "real_16bit.flac"
        generate_real_flac(real_16, bits=16, sample_rate=44100)
        valid_16, bps_16, rate_16, msg_16 = verify_flac_24bit(real_16)
        self.assertFalse(valid_16)
        self.assertEqual(bps_16, 16)
        self.assertIn("24-bit FLAC required", msg_16)

        # 3. Header-only FLAC stream rejected
        header_only = self.base_dir / "header_only.flac"
        header_only.write_bytes(create_header_only_flac(24, 96000))
        valid_hdr, _, _, msg_hdr = verify_flac_24bit(header_only)
        self.assertFalse(valid_hdr)
        self.assertIn("missing audio frames", msg_hdr)

        # 4. Zero sample rate rejected
        zero_rate = self.base_dir / "zero_rate.flac"
        zero_rate.write_bytes(create_header_only_flac(24, 0))
        valid_zero, _, _, msg_zero = verify_flac_24bit(zero_rate)
        self.assertFalse(valid_zero)
        self.assertIn("Invalid or zero sample rate", msg_zero)

        # 5. Content-Length mismatch rejected
        valid_cl, _, _, msg_cl = verify_flac_24bit(real_24, expected_length=999999)
        self.assertFalse(valid_cl)
        self.assertIn("does not match Content-Length", msg_cl)

    def test_existing_file_preservation_and_collision_safe_publish(self):
        """Root defect reproduction: verify existing user music is NEVER overwritten."""
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/single", destination="private")
        job_id = job["id"]

        # Pre-create existing user music at destination
        dest_dir = self.output_roots["private"] / "Artist" / "Album"
        dest_dir.mkdir(parents=True, exist_ok=True)
        user_file = dest_dir / "01 - Track.flac"
        original_user_content = b"ORIGINAL_USER_MUSIC_DO_NOT_OVERWRITE" * 20
        user_file.write_bytes(original_user_content)

        # Generate real new audio for download
        real_audio_path = self.base_dir / "incoming.flac"
        generate_real_flac(real_audio_path, bits=24, sample_rate=48000)
        incoming_bytes = real_audio_path.read_bytes()

        self.client.resolve = MagicMock(return_value={
            "title": "Album",
            "artist": "Artist",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track", "artist": "Artist"}],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_1", "tok_1"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist/Album/01 - Track.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(incoming_bytes))
        self.client.ack_track = MagicMock(return_value=True)

        self.worker.process_job(job)

        # 1. Verify user file was preserved byte-for-byte!
        self.assertEqual(user_file.read_bytes(), original_user_content)

        # 2. Verify new track published to collision-safe filename
        flac_files = list(dest_dir.glob("*.flac"))
        self.assertEqual(len(flac_files), 2)
        collision_files = [f for f in flac_files if f != user_file]
        self.assertEqual(len(collision_files), 1)
        self.assertEqual(collision_files[0].read_bytes(), incoming_bytes)

        # 3. Track in DB points to collision file
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[0]["filename"], str(collision_files[0].relative_to(self.output_roots["private"].resolve())))

    def test_fail_closed_on_missing_live_destination(self):
        """Worker in live mode fails closed if pre-provisioned root is missing."""
        live_roots = {
            "private": Path("/nonexistent/storage/Music/Astro"),
            "family": Path("/nonexistent/storage/Music/Family"),
        }
        live_worker = AntraWorker(
            store=self.store,
            client=self.client,
            output_roots=live_roots,
            is_test_env=False,
        )
        ok, msg = live_worker.check_output_storage("private")
        self.assertFalse(ok)
        self.assertIn("not been provisioned", msg)
        self.assertFalse(live_roots["private"].exists())

    def test_cancellation_during_byte_stream(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/stream_cancel")
        job_id = job["id"]

        real_audio_path = self.base_dir / "incoming.flac"
        generate_real_flac(real_audio_path, bits=24, sample_rate=48000)
        incoming_bytes = real_audio_path.read_bytes()

        class CancellingStream(io.BytesIO):
            def __init__(stream_self, data, store, jid):
                super().__init__(data)
                stream_self.store = store
                stream_self.jid = jid

            def read(stream_self, size=-1):
                # Cancel job while stream is in progress
                stream_self.store.cancel_job(stream_self.jid)
                return super().read(size)

        self.client.resolve = MagicMock(return_value={
            "title": "Album",
            "artist": "Artist",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track", "artist": "Artist"}],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_cancel", "tok_cancel"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist/Album/01 - Track.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(
            return_value=CancellingStream(incoming_bytes, self.store, job_id)
        )

        self.worker.process_job(job)

        job_state = self.store.get_job(job_id)
        self.assertEqual(job_state["status"], "cancelled")
        # No temp files left behind
        temp_files = list(self.output_roots["private"].glob("**/*.part"))
        self.assertEqual(len(temp_files), 0)

    def test_unknown_submission_uncertainty_and_malformed_response(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/unknown")

        self.client.resolve = MagicMock(return_value={
            "title": "Album X",
            "artist": "Artist X",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track 1", "artist": "Artist X"}],
        })
        self.client.submit_job = MagicMock(
            side_effect=UncertainSubmissionError("Timed out without confirmed job_id")
        )

        self.worker.process_job(job)

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "uncertain")
        self.assertIn("Automatic resubmission is disabled", job_state["message"])

        # Not runnable on restart
        self.assertIsNone(self.store.get_next_runnable_job())

    def test_concurrency_429_backoff_and_no_hot_loop(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/concur")

        self.client.resolve = MagicMock(
            side_effect=ConcurrencyLimitError("Concurrency limit; retry shortly")
        )
        self.worker.process_job(job)

        job_state = self.store.get_internal_job(job["id"])
        self.assertEqual(job_state["status"], "queued")
        self.assertGreater(job_state["concurrency_retry_after"], time.time())
        self.assertEqual(job_state["retry_attempts"], 1)

        # Not runnable immediately (no hot loop)
        self.assertIsNone(self.store.get_next_runnable_job())

    def test_restart_recovery_one_upstream_job_for_250_tracks(self):
        """A replayed 250-track folder job resumes without resubmission or redownloading completed tracks."""
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/playlist/large250", destination="family")
        job_id = job["id"]
        self.client.resolve = MagicMock(return_value={
            "title": "Mega Playlist", "total": 250,
            "tracks": [{"index": i, "title": f"Song {i+1}"} for i in range(250)],
        })
        audio = self.base_dir / "incoming.flac"
        generate_real_flac(audio, bits=24, sample_rate=48000)
        payload = audio.read_bytes()
        self.client.submit_job = MagicMock(return_value=("one_upstream", "initial_token"))
        self.client.get_job_status = MagicMock(return_value={"status": "downloading"})
        self.client.fresh_ws_token = MagicMock(return_value="fresh_token")
        def interrupted(*args):
            for index in range(105):
                yield {"type": "track_done", "index": index, "path": f"Artist/{index+1}.flac"}
            raise ConnectionResetError("owned fixture disconnect")
        def replay(*args):
            for index in range(250):
                yield {"type": "track_done", "index": index, "path": f"Artist/{index+1}.flac"}
            yield {"type": "complete"}
        self.client.connect_job_ws = MagicMock(side_effect=interrupted)
        self.client.download_track_stream = MagicMock(side_effect=lambda *args: io.BytesIO(payload))
        self.client.ack_track = MagicMock(return_value=True)
        self.worker.process_job(job)
        self.assertEqual(self.store.get_job(job_id)["completed"], 105)
        self.assertIsNone(self.store.get_next_runnable_job())
        self.store.update_job(job_id, concurrency_retry_after=0)
        self.client.connect_job_ws = MagicMock(side_effect=replay)
        restarted = AntraWorker(store=self.store, client=self.client, output_roots=self.output_roots, lock_file=self.lock_file, is_test_env=True)
        self.assertTrue(restarted.run_once())
        self.assertEqual(self.store.get_job(job_id)["completed"], 250)
        self.assertEqual(self.store.get_job(job_id)["status"], "completed")
        self.client.submit_job.assert_called_once_with(job["url"], start_index=0, end_index=250)
        self.client.fresh_ws_token.assert_called_once_with("one_upstream")
        self.assertEqual(self.client.download_track_stream.call_count, 250)
        self.assertEqual(len(list(self.output_roots["family"].rglob("*.flac"))), 250)


if __name__ == "__main__":
    unittest.main()
