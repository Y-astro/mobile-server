"""Tests for Antra background download worker, FLAC validation, and batching."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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


def create_flac_header(bits_per_sample: int = 24, sample_rate: int = 96000, channels: int = 2) -> bytes:
    """Generate a synthetic FLAC header with valid STREAMINFO block."""
    info_int = (sample_rate << 44) | ((channels - 1) << 41) | ((bits_per_sample - 1) << 36) | 1000
    streaminfo_body = b"\x00" * 10 + info_int.to_bytes(8, "big") + b"\x00" * 16
    header = b"fLaC" + bytes([0, 0, 0, len(streaminfo_body)]) + streaminfo_body
    return header


class TestAntraWorker(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "queue.sqlite3"
        self.output_dir = self.base_dir / "Music" / "Antra"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.base_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.base_dir / "worker.lock"

        self.store = AntraStore(db_path=self.db_path, output_dir=self.output_dir)
        self.client = AntraClient(base_url="http://127.0.0.1:9999", config_dir=self.config_dir)
        self.worker = AntraWorker(
            store=self.store,
            client=self.client,
            output_dir=self.output_dir,
            lock_file=self.lock_file,
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
        # Second worker cannot acquire same lock
        worker2 = AntraWorker(
            store=self.store,
            client=self.client,
            output_dir=self.output_dir,
            lock_file=self.lock_file,
        )
        self.assertFalse(worker2.acquire_lock())
        self.worker.release_lock()
        # Now worker2 can acquire
        self.assertTrue(worker2.acquire_lock())
        worker2.release_lock()

    def test_missing_credentials_heartbeat(self):
        # No credentials file written
        job = self.store.create_job("https://tidal.com/album/12345")
        self.worker.process_job(job)

        status = self.store.get_worker_status()
        self.assertEqual(status["state"], "needs_setup")
        self.assertIn("credentials.json", status["message"])

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "blocked")

    def test_termux_dns_fix_safety(self):
        # On standard Linux (not Termux)
        self.assertFalse(maybe_reexec_dns_fix())

        # If ANTRA_DNS_FIXED=1
        with patch.dict(os.environ, {"ANTRA_DNS_FIXED": "1"}):
            self.assertFalse(maybe_reexec_dns_fix())

    def test_flac_24bit_verification(self):
        # 1. Valid 24-bit FLAC headers
        for rate in (44100, 48000, 96000, 192000):
            flac_path = self.base_dir / f"test_{rate}.flac"
            flac_path.write_bytes(create_flac_header(bits_per_sample=24, sample_rate=rate))
            valid, bps, parsed_rate, msg = verify_flac_24bit(flac_path)
            self.assertTrue(valid)
            self.assertEqual(bps, 24)
            self.assertEqual(parsed_rate, rate)

        # 2. 16-bit FLAC rejected
        flac_16 = self.base_dir / "test_16.flac"
        flac_16.write_bytes(create_flac_header(bits_per_sample=16, sample_rate=44100))
        valid, bps, parsed_rate, msg = verify_flac_24bit(flac_16)
        self.assertFalse(valid)
        self.assertEqual(bps, 16)
        self.assertIn("24-bit FLAC required", msg)

        # 3. Non-FLAC (e.g. MP3 / ID3) rejected
        mp3_file = self.base_dir / "test.mp3"
        mp3_file.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00" + b"\x00" * 50)
        valid, bps, _, msg = verify_flac_24bit(mp3_file)
        self.assertFalse(valid)
        self.assertIn("missing FLAC marker", msg)

        # 4. Truncated file rejected
        trunc_file = self.base_dir / "trunc.flac"
        trunc_file.write_bytes(b"fLaC\x00\x00")
        valid, _, _, msg = verify_flac_24bit(trunc_file)
        self.assertFalse(valid)

    def test_path_sanitization_and_containment(self):
        # Normal path
        p1 = sanitize_track_path("Artist/Album/01 - Song.flac", self.output_dir)
        self.assertEqual(p1, self.output_dir.resolve() / "Artist" / "Album" / "01 - Song.flac")

        # Traversal attempts sanitized or contained
        p2 = sanitize_track_path("../../etc/passwd", self.output_dir)
        self.assertTrue(p2.is_relative_to(self.output_dir.resolve()))
        self.assertNotIn("..", p2.parts)

        # Windows backslashes and special characters
        p3 = sanitize_track_path("Artist:Name\\Album?*\\Track<>|.flac", self.output_dir)
        self.assertTrue(p3.is_relative_to(self.output_dir.resolve()))
        self.assertNotIn(":", p3.name)
        self.assertNotIn("?", str(p3))

    def test_unknown_submission_enters_uncertain_no_auto_retry(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/12345")

        self.client.resolve = MagicMock(return_value={
            "title": "Album X",
            "artist": "Artist X",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track 1", "artist": "Artist X"}],
        })
        # Simulate timeout on submit_job
        self.client.submit_job = MagicMock(
            side_effect=UncertainSubmissionError("Timed out without confirmed job_id")
        )

        self.worker.process_job(job)

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "uncertain")
        self.assertIn("manual decision required", job_state["message"])

        # When worker runs again, uncertain job is NOT auto-retried
        queued = self.store.get_next_queued_job()
        self.assertIsNone(queued)

    def test_quota_daily_limit_blocks_job_and_worker(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/12345")

        self.client.resolve = MagicMock(
            side_effect=DailyLimitError("Daily limit reached")
        )
        self.worker.process_job(job)

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "blocked")

        status = self.store.get_worker_status()
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["message"], "Daily limit reached")

    def test_concurrency_429_backs_off_and_stays_queued(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/12345")

        self.client.resolve = MagicMock(
            side_effect=ConcurrencyLimitError("Concurrency limit; retry shortly")
        )
        self.worker.process_job(job)

        job_state = self.store.get_job(job["id"])
        self.assertEqual(job_state["status"], "queued")

    def test_failed_ack_does_not_fail_track(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/single")
        job_id = job["id"]

        self.client.resolve = MagicMock(return_value={
            "title": "Single",
            "artist": "Artist",
            "total": 1,
            "tracks": [{"index": 0, "title": "S1", "artist": "Artist"}],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_123", "token_abc"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist/Single/01 - S1.flac"},
            {"type": "complete"},
        ])
        flac_data = create_flac_header(bits_per_sample=24, sample_rate=96000)
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(flac_data))
        # Simulate ack failure (e.g. network blip or 500)
        self.client.ack_track = MagicMock(return_value=False)

        self.worker.process_job(job)

        # Track must still be completed!
        tracks = self.store.get_tracks(job_id)
        self.assertEqual(tracks[0]["status"], "completed")
        self.assertEqual(tracks[0]["bit_depth"], 24)
        self.assertEqual(tracks[0]["sample_rate"], 96000)

        # Job completed
        job_state = self.store.get_job(job_id)
        self.assertEqual(job_state["status"], "completed")

    def test_playlist_250_tracks_chunking_and_restart(self):
        """Exercise 250 tracks across chunk 1 (1-100), chunk 2 (101-200), chunk 3 (201-250),

        including restart halfway and continuing to completion.
        """
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/playlist/large250")
        job_id = job["id"]

        # 250 tracks
        mock_tracks = [
            {"index": i, "title": f"Song {i+1}", "artist": "Big Artist"}
            for i in range(250)
        ]
        self.client.resolve = MagicMock(return_value={
            "title": "Mega Playlist",
            "artist": "Big Artist",
            "total": 250,
            "tracks": mock_tracks,
        })

        flac_data = create_flac_header(bits_per_sample=24, sample_rate=48000)

        # Simulate first run downloading only chunk 1 (0..99) then interrupting
        def submit_batch_run1(music_url, start_index, end_index):
            return f"upstream_{start_index}", f"token_{start_index}"

        def ws_events_run1(upstream_job_id, ws_token):
            if upstream_job_id == "upstream_1":
                for idx in range(100):
                    yield {"type": "track_done", "index": idx, "path": f"Big/Mega/{idx+1:03d}.flac"}
                yield {"type": "complete"}
            elif upstream_job_id == "upstream_101":
                # Simulate interruption at track 105
                for idx in range(100, 105):
                    yield {"type": "track_done", "index": idx, "path": f"Big/Mega/{idx+1:03d}.flac"}
                raise ConnectionResetError("Simulated worker restart/crash")

        self.client.submit_job = MagicMock(side_effect=submit_batch_run1)
        self.client.connect_job_ws = MagicMock(side_effect=ws_events_run1)
        self.client.download_track_stream = MagicMock(side_effect=lambda *args: io.BytesIO(flac_data))
        self.client.ack_track = MagicMock(return_value=True)

        self.worker.process_job(job)

        # Check state after partial run: tracks 0..104 completed, 105..249 queued
        trks = self.store.get_tracks(job_id)
        self.assertEqual(len(trks), 250)
        completed_count = sum(1 for t in trks if t["status"] == "completed")
        self.assertEqual(completed_count, 105)

        # RESTART: Simulate second run resuming the same job
        def ws_events_run2(upstream_job_id, ws_token):
            if upstream_job_id == "upstream_101":
                # Upstream sends all tracks in batch 101..200 (indices 100..199)
                for idx in range(100, 200):
                    yield {"type": "track_done", "index": idx, "path": f"Big/Mega/{idx+1:03d}.flac"}
                yield {"type": "complete"}
            elif upstream_job_id == "upstream_201":
                for idx in range(200, 250):
                    yield {"type": "track_done", "index": idx, "path": f"Big/Mega/{idx+1:03d}.flac"}
                yield {"type": "complete"}

        self.client.connect_job_ws = MagicMock(side_effect=ws_events_run2)
        # Re-run worker on the same job
        self.worker.process_job(self.store.get_job(job_id))

        # After restart, all 250 tracks must be completed!
        final_trks = self.store.get_tracks(job_id)
        self.assertEqual(len(final_trks), 250)
        final_completed = sum(1 for t in final_trks if t["status"] == "completed")
        self.assertEqual(final_completed, 250)

        final_job = self.store.get_job(job_id)
        self.assertEqual(final_job["status"], "completed")
        self.assertEqual(final_job["completed"], 250)
        self.assertEqual(final_job["total"], 250)


if __name__ == "__main__":
    unittest.main()
