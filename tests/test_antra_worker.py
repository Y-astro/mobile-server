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
    AntraClientError,
    DailyLimitError,
    ConcurrencyLimitError,
    UncertainSubmissionError,
    MissingCredentialsError,
)
from antra_worker import (
    AntraWorker,
    verify_flac,
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

    def test_flac_real_and_corruption_rejections(self):
        # 1. Real 24-bit FLAC audio generated via ffmpeg
        real_24 = self.base_dir / "real_24bit.flac"
        generate_real_flac(real_24, bits=24, sample_rate=48000)
        valid, bps, rate, msg = verify_flac(real_24)
        self.assertTrue(valid)
        self.assertEqual(bps, 24)
        self.assertEqual(rate, 48000)
        self.assertIn("Verified 24-bit FLAC", msg)

        # 2. Real 16-bit FLAC accepted as native quality
        real_16 = self.base_dir / "real_16bit.flac"
        generate_real_flac(real_16, bits=16, sample_rate=44100)
        valid_16, bps_16, rate_16, msg_16 = verify_flac(real_16)
        self.assertTrue(valid_16)
        self.assertEqual(bps_16, 16)
        self.assertEqual(rate_16, 44100)
        self.assertIn("Verified 16-bit FLAC", msg_16)

        # Alias verify_flac_24bit accepts both 16 and 24-bit
        self.assertTrue(verify_flac_24bit(real_24)[0])
        self.assertTrue(verify_flac_24bit(real_16)[0])

        # 3. Header-only FLAC stream rejected
        header_only = self.base_dir / "header_only.flac"
        header_only.write_bytes(create_header_only_flac(24, 96000))
        valid_hdr, _, _, msg_hdr = verify_flac(header_only)
        self.assertFalse(valid_hdr)
        self.assertIn("missing audio frames", msg_hdr)

        # 4. Zero sample rate rejected
        zero_rate = self.base_dir / "zero_rate.flac"
        zero_rate.write_bytes(create_header_only_flac(24, 0))
        valid_zero, _, _, msg_zero = verify_flac(zero_rate)
        self.assertFalse(valid_zero)
        self.assertIn("Invalid or zero sample rate", msg_zero)

        # 5. Content-Length mismatch rejected
        valid_cl, _, _, msg_cl = verify_flac(real_24, expected_length=999999)
        self.assertFalse(valid_cl)
        self.assertIn("does not match Content-Length", msg_cl)

        # 6. Corrupt audio frames rejected by decoder
        corrupt_flac = self.base_dir / "corrupt.flac"
        corrupt_data = bytearray(real_24.read_bytes())
        corrupt_data[-40:] = b"\x00" * 40
        corrupt_flac.write_bytes(bytes(corrupt_data))
        valid_corrupt, _, _, msg_corrupt = verify_flac(corrupt_flac)
        self.assertFalse(valid_corrupt)
        self.assertIn("decode verification failed", msg_corrupt)

    def test_real_16bit_flac_download_and_save_not_ack_delete(self):
        """16-bit fallback FLAC is saved to library as completed, not acked+deleted."""
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/sixteen", destination="private")
        job_id = job["id"]

        real_16 = self.base_dir / "incoming_16.flac"
        generate_real_flac(real_16, bits=16, sample_rate=44100)
        incoming_bytes = real_16.read_bytes()

        self.client.resolve = MagicMock(return_value={
            "title": "Album 16",
            "artist": "Artist 16",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track 16", "artist": "Artist 16"}],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_16", "tok_16"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist 16/Album 16/01 - Track 16.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(incoming_bytes))
        self.client.ack_track = MagicMock(return_value=True)

        self.worker.process_job(job)

        # DB shows completed with 16-bit and 44100 Hz
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[0]["bit_depth"], 16)
        self.assertEqual(trks[0]["sample_rate"], 44100)

        # Saved to disk, not deleted!
        saved_file = self.output_roots["private"] / trks[0]["filename"]
        self.assertTrue(saved_file.exists())
        self.assertEqual(saved_file.read_bytes(), incoming_bytes)

        # Ack called AFTER save
        self.client.ack_track.assert_called_once_with("upstream_16", 0)

    def test_corrupt_stream_marks_failed_without_upstream_ack(self):
        """Corrupt audio stream marks track failed and does not acknowledge upstream."""
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/corrupt", destination="private")
        job_id = job["id"]

        real_audio = self.base_dir / "valid_for_corrupt.flac"
        generate_real_flac(real_audio, bits=24, sample_rate=48000)
        corrupted_bytes = bytearray(real_audio.read_bytes())
        corrupted_bytes[-50:] = b"\x00" * 50

        self.client.resolve = MagicMock(return_value={
            "title": "Album Corrupt",
            "artist": "Artist Corrupt",
            "total": 1,
            "tracks": [{"index": 0, "title": "Track Corrupt", "artist": "Artist Corrupt"}],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_corrupt", "tok_corrupt"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist Corrupt/01 - Corrupt.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(bytes(corrupted_bytes)))
        self.client.ack_track = MagicMock(return_value=True)

        self.worker.process_job(job)

        # Track marked as failed
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "failed")
        self.assertIn("decode verification failed", trks[0]["message"])

        # Ack must NOT be called for failed track
        self.client.ack_track.assert_not_called()

        # No audio published
        self.assertEqual(list(self.output_roots["private"].rglob("*.flac")), [])

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
        # 429 concurrency limit does not exhaust transport retry budget
        self.assertEqual(job_state["retry_attempts"], 0)

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

    def test_reproduction_failed_first_then_healthy_track_and_new_job_with_ack_failure(self):
        """Reproduction proof:
        1. Failed first track does not abort healthy later track.
        2. Reconnect/ack failure on healthy track does not crash worker.
        3. Completed files remain untouched byte-for-byte.
        4. New queued job is not starved by prior failure/ack issue.
        5. Progress counters accurately reflect terminal states.
        """
        self._write_credentials()

        # Job 1: 2 tracks (Track 0 fails, Track 1 succeeds)
        j1 = self.store.create_job("https://tidal.com/album/job1", destination="private")
        j1_id = j1["id"]

        valid_audio = self.base_dir / "valid_sample.flac"
        generate_real_flac(valid_audio, bits=16, sample_rate=44100)
        valid_bytes = valid_audio.read_bytes()

        self.client.resolve = MagicMock(side_effect=lambda url: {
            "title": "Album 1" if "job1" in url else "Album 2",
            "artist": "Artist 1" if "job1" in url else "Artist 2",
            "total": 2 if "job1" in url else 1,
            "tracks": [
                {"index": 0, "title": "Track Failed", "artist": "Artist 1"},
                {"index": 1, "title": "Track Healthy", "artist": "Artist 1"},
            ] if "job1" in url else [
                {"index": 0, "title": "Track Job 2", "artist": "Artist 2"},
            ],
        })
        self.client.submit_job = MagicMock(return_value=("upstream_j1", "tok_j1"))

        # Upstream yields: Track 0 fails, Track 1 succeeds
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_failed", "index": 0, "error": "source corrupted"},
            {"type": "track_done", "index": 1, "path": "Artist 1/Album 1/02 - Track Healthy.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(valid_bytes))

        # Reconnect/ack failure: ack_track raises exception
        self.client.ack_track = MagicMock(side_effect=ConnectionResetError("provider reconnect failure"))

        # Process Job 1
        self.worker.process_job(j1)

        # 1. Verify Track 0 failed, Track 1 completed
        trks1 = self.store.get_tracks(j1_id)
        self.assertEqual(len(trks1), 2)
        self.assertEqual(trks1[0]["status"], "failed")
        self.assertEqual(trks1[1]["status"], "completed")

        # 2. Verify completed file on disk is intact
        published1 = self.output_roots["private"] / trks1[1]["filename"]
        self.assertTrue(published1.is_file())
        self.assertEqual(published1.read_bytes(), valid_bytes)

        # 3. Job 1 progress counters are accurate
        j1_final = self.store.get_job(j1_id)
        self.assertEqual(j1_final["total"], 2)
        self.assertEqual(j1_final["completed"], 1)
        self.assertEqual(j1_final["failed"], 1)

        # 4. Now queue Job 2 (healthy new job)
        j2 = self.store.create_job("https://tidal.com/album/job2", destination="private")
        j2_id = j2["id"]

        self.client.submit_job = MagicMock(return_value=("upstream_j2", "tok_j2"))
        self.client.connect_job_ws = MagicMock(return_value=[
            {"type": "track_done", "index": 0, "path": "Artist 2/Album 2/01 - Job 2.flac"},
            {"type": "complete"},
        ])
        self.client.download_track_stream = MagicMock(return_value=io.BytesIO(valid_bytes))
        self.client.ack_track = MagicMock(return_value=True)

        # Worker processes next runnable job via run_once()
        # Even with failed ack attempts on Job 1, run_once processes Job 2 without starvation
        processed = self.worker.run_once()
        self.assertTrue(processed)

        # Job 2 completed successfully
        j2_final = self.store.get_job(j2_id)
        self.assertEqual(j2_final["status"], "completed")
        self.assertEqual(j2_final["completed"], 1)
        self.assertEqual(j2_final["failed"], 0)

        # Job 1 completed file remains untouched
        self.assertEqual(published1.read_bytes(), valid_bytes)

    def test_upstream_rejected_or_expired_drains_queued_tracks(self):
        """Proof that provider status 400 rejection drains queued tracks without stranding."""
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/album/stranded-test", destination="private")
        job_id = job["id"]

        tracks = [
            {"index": 0, "title": "Track 1", "artist": "Artist"},
            {"index": 1, "title": "Track 2", "artist": "Artist"},
            {"index": 2, "title": "Track 3", "artist": "Artist"},
        ]
        self.store.set_tracks(job_id, tracks)
        self.store.update_track(job_id, 0, status="completed")
        # Track 1 and 2 are still queued

        # Provider marks job failed upstream -> status 400
        self.store.update_job(job_id, upstream_job_id="upstream_dead")
        self.client.get_job_status = MagicMock(return_value={"status": "failed"})

        self.worker.process_job(job)

        # Status is partial because 1 completed
        final_job = self.store.get_job(job_id)
        self.assertEqual(final_job["status"], "partial")
        self.assertEqual(final_job["completed"], 1)
        self.assertEqual(final_job["failed"], 2)
        self.assertEqual(final_job["total"], 3)

        # Both remaining tracks must be failed, NOT queued!
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[1]["status"], "failed")
        self.assertEqual(trks[2]["status"], "failed")

    def test_bounded_transport_retry_budget_prevents_indefinite_queue_starvation(self):
        """10 transport failures marks job terminal so queued subsequent jobs are not starved."""
        self._write_credentials()
        j1 = self.store.create_job("https://tidal.com/album/failing-job", destination="private")
        j1_id = j1["id"]
        self.store.set_tracks(j1_id, [{"index": 0, "title": "Track 1", "artist": "Artist"}])
        self.store.update_job(j1_id, retry_attempts=9, upstream_job_id="up_fail")

        j2 = self.store.create_job("https://tidal.com/album/waiting-job", destination="private")
        j2_id = j2["id"]

        # Job 1 encounters network failure on attempt 10
        self.client.get_job_status = MagicMock(return_value={"status": "downloading"})
        self.client.fresh_ws_token = MagicMock(return_value="token")
        self.client.connect_job_ws = MagicMock(side_effect=AntraClientError("Network drop"))

        self.worker.process_job(j1)

        # Job 1 must be terminal (failed), not left in queued or downloading loop
        j1_final = self.store.get_job(j1_id)
        self.assertEqual(j1_final["status"], "failed")
        self.assertIn("Connection failed after 10 retry attempts", j1_final["message"])

        # Next runnable job is now Job 2 (not starved!)
        next_job = self.store.get_next_runnable_job()
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], j2_id)


class TestAntraAutomaticRetries(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.hashlib = hashlib
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "antra.db"
        self.output_root = Path(self.temp_dir.name) / "music"
        self.output_root.mkdir()
        self.store = AntraStore(self.db_path, output_roots={"private": self.output_root})
        self.client = MagicMock()
        self.client.has_credentials.return_value = True
        self.client.ack_track.return_value = True
        self.client.resolve.side_effect = lambda url: {"tracks": self.store.get_tracks(self.store.list_jobs()[0]["id"])}
        self.worker = AntraWorker(self.store, self.client, output_roots={"private": self.output_root}, lock_file=Path(self.temp_dir.name)/"lock", is_test_env=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_zero_failure_completion_does_not_schedule_retry(self):
        job = self.store.create_job("https://example.com/album", destination="private")
        tracks = [{"title": "T1", "artist": "A1", "index": 0}]
        self.store.set_tracks(job["id"], tracks)
        self.client.submit_job.return_value = ("upstream_1", "token_1")
        self.client.connect_job_ws.return_value = [
            {"type": "track_done", "index": 0, "path": "A1/01.flac"},
            {"type": "complete"}
        ]
        with patch.object(self.worker, "_download_and_publish_track", side_effect=lambda *args: self.store.update_track(job["id"], 0, status="completed")) as mock_dl:
            self.worker.process_job(job)
            mock_dl.assert_called_once()
        final_job = self.store.get_job(job["id"])
        self.assertEqual(final_job["status"], "completed")
        self.assertEqual(final_job["automatic_retry_rounds"], 0)

    def test_automatic_retry_up_to_max_passes_then_terminal_partial(self):
        job = self.store.create_job("https://example.com/playlist", destination="private")
        tracks = [
            {"title": "T1", "artist": "A1", "index": 0},
            {"title": "T2", "artist": "A1", "index": 1},
        ]
        self.store.set_tracks(job["id"], tracks)
        self.store.update_track(job["id"], 0, status="completed")
        # T2 remains failed across passes
        self.client.submit_job.return_value = ("up_1", "tok_1")
        self.client.connect_job_ws.return_value = [
            {"type": "track_failed", "index": 1, "error": "bad source"},
            {"type": "complete"}
        ]

        # Pass 1
        with patch.object(self.worker, "_download_and_publish_track"):
            self.worker.process_job(self.store.get_job(job["id"]))
        j1 = self.store.get_job(job["id"])
        self.assertEqual(j1["status"], "queued")
        self.assertEqual(j1["automatic_retry_rounds"], 1)

        # Pass 2
        with patch.object(self.worker, "_download_and_publish_track"):
            self.worker.process_job(self.store.get_job(job["id"]))
        j2 = self.store.get_job(job["id"])
        self.assertEqual(j2["status"], "queued")
        self.assertEqual(j2["automatic_retry_rounds"], 2)

        # Pass 3
        with patch.object(self.worker, "_download_and_publish_track"):
            self.worker.process_job(self.store.get_job(job["id"]))
        j3 = self.store.get_job(job["id"])
        self.assertEqual(j3["status"], "queued")
        self.assertEqual(j3["automatic_retry_rounds"], 3)

        # Pass 4: exceeds max 3, must not schedule pass 4
        with patch.object(self.worker, "_download_and_publish_track"):
            self.worker.process_job(self.store.get_job(job["id"]))
        j4 = self.store.get_job(job["id"])
        self.assertEqual(j4["status"], "partial")
        self.assertEqual(j4["automatic_retry_rounds"], 3)
        self.assertIn("Manual retry is available", j4["message"])

    def test_manual_retry_resets_automatic_round_budget(self):
        job = self.store.create_job("https://example.com/playlist", destination="private")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Failed"}])
        for round_number in range(1, 4):
            self.store.update_job(job["id"], status="downloading", upstream_job_id="finished")
            self.store.update_track(job["id"], 0, status="failed")
            self.assertTrue(self.store.schedule_automatic_retry(job["id"], delay=0, round_number=round_number))
        self.store.update_job(job["id"], status="partial")
        retried = self.store.retry_job(job["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["automatic_retry_rounds"], 0)

    def test_cancelled_job_not_scheduled_by_retry(self):
        job = self.store.create_job("https://example.com/playlist", destination="private")
        self.store.cancel_job(job["id"])
        scheduled = self.store.schedule_automatic_retry(job["id"], delay=0, round_number=1)
        self.assertFalse(scheduled)
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")

    def test_429_does_not_burn_automatic_retry_round(self):
        job = self.store.create_job("https://example.com/playlist", destination="private")
        tracks = [{"title": "T1", "artist": "A1", "index": 0}]
        self.store.set_tracks(job["id"], tracks)
        self.client.submit_job.side_effect = ConcurrencyLimitError("busy", retry_after=10)
        self.worker.process_job(job)
        j = self.store.get_internal_job(job["id"])
        self.assertEqual(j["automatic_retry_rounds"], 0)
        # 429 concurrency wait does not increment retry_attempts
        self.assertEqual(j["retry_attempts"], 0)
        self.assertGreater(j["concurrency_retry_after"], time.time())

    def test_mixed_replay_downloads_only_failed_tracks(self):
        job = self.store.create_job("https://example.com/playlist", destination="private")
        tracks = [
            {"title": "T1", "artist": "A1", "index": 0},
            {"title": "T2", "artist": "A1", "index": 1},
            {"title": "T3", "artist": "A1", "index": 2},
        ]
        self.store.set_tracks(job["id"], tracks)
        # T1 and T2 are already completed on disk with checksums
        f1 = self.output_root / "t1.flac"
        f2 = self.output_root / "t2.flac"
        f1.write_bytes(b"MOCK_AUDIO_1")
        f2.write_bytes(b"MOCK_AUDIO_2")
        h1 = self.hashlib.sha256(b"MOCK_AUDIO_1").hexdigest()
        h2 = self.hashlib.sha256(b"MOCK_AUDIO_2").hexdigest()
        self.store.update_track(job["id"], 0, status="completed", filename="t1.flac", publication_path="t1.flac", file_sha256=h1)
        self.store.update_track(job["id"], 1, status="completed", filename="t2.flac", publication_path="t2.flac", file_sha256=h2)
        # T3 failed previously and is queued for retry
        self.store.update_track(job["id"], 2, status="queued")

        self.client.submit_job.return_value = ("up_retry", "token_retry")
        self.client.connect_job_ws.return_value = [
            {"type": "track_done", "index": 0, "path": "t1.flac"},
            {"type": "track_done", "index": 1, "path": "t2.flac"},
            {"type": "track_done", "index": 2, "path": "t3.flac"},
            {"type": "complete"}
        ]
        incoming = Path(self.temp_dir.name) / "incoming.flac"
        generate_real_flac(incoming, bits=16, sample_rate=44100)
        response = io.BytesIO(incoming.read_bytes())
        response.headers = {"Content-Length": str(incoming.stat().st_size)}
        self.client.download_track_stream.return_value = response
        self.worker.process_job(self.store.get_job(job["id"]))
        self.client.download_track_stream.assert_called_once_with("up_retry", 2)
        self.assertEqual(f1.read_bytes(), b"MOCK_AUDIO_1")
        self.assertEqual(f2.read_bytes(), b"MOCK_AUDIO_2")
        self.assertEqual(self.store.get_job(job["id"])["completed"], 3)

        # All 3 tracks acknowledged to upstream
        self.assertEqual(self.client.ack_track.call_count, 3)

    def test_retry_round_and_delay_survive_store_restart(self):
        job = self.store.create_job("https://example.com/playlist")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Failed"}])
        self.store.update_track(job["id"], 0, status="failed")
        self.store.update_job(job["id"], status="downloading", upstream_job_id="finished")
        self.assertTrue(self.store.schedule_automatic_retry(job["id"], delay=60, round_number=1))
        restarted = AntraStore(self.db_path)
        self.assertEqual(restarted.get_internal_job(job["id"])["automatic_retry_rounds"], 1)
        self.assertIsNone(restarted.get_next_runnable_job())
        self.assertFalse(restarted.schedule_automatic_retry(job["id"], delay=0, round_number=2))

    def test_uncertain_submission_and_cancelled_finalization_are_preserved(self):
        job = self.store.create_job("https://example.com/playlist")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Failed"}])
        self.store.update_track(job["id"], 0, status="failed")
        self.store.update_job(job["id"], status="uncertain", submission_intent=1)
        self.assertFalse(self.store.schedule_automatic_retry(job["id"], delay=0, round_number=1))
        self.assertEqual(self.store.get_internal_job(job["id"])["submission_intent"], 1)
        self.store.cancel_job(job["id"])
        self.store.finish_job(job["id"], "partial", "stale completion")
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")

    def test_retry_reconnect_keeps_confirmed_provider_id(self):
        job = self.store.create_job("https://example.com/playlist")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Failed"}])
        self.store.update_track(job["id"], 0, status="failed")
        self.store.update_job(job["id"], status="downloading", upstream_job_id="finished")
        self.store.schedule_automatic_retry(job["id"], delay=0, round_number=1)
        self.store.update_job(job["id"], status="downloading", upstream_job_id="confirmed_retry")
        self.client.get_job_status.return_value = {"status": "downloading"}
        self.client.fresh_ws_token.return_value = "new-token"
        self.client.connect_job_ws.side_effect = AntraClientError("Interrupted")
        self.worker.process_job(self.store.get_job(job["id"]))
        self.client.submit_job.assert_not_called()
        current = self.store.get_internal_job(job["id"])
        self.assertEqual(current["upstream_job_id"], "confirmed_retry")
        self.assertEqual(current["automatic_retry_rounds"], 1)

    def test_changed_playlist_is_not_submitted_as_original_indices(self):
        job = self.store.create_job("https://example.com/playlist")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Original"}, {"index": 1, "title": "Failed"}])
        self.store.update_track(job["id"], 0, status="completed")
        self.client.resolve.side_effect = None
        self.client.resolve.return_value = {"tracks": [{"title": "Changed", "artist": ""}]}
        self.worker.process_job(job)
        self.client.submit_job.assert_not_called()
        self.assertIn("Playlist contents changed", self.store.get_job(job["id"])["message"])
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "completed")

    def test_concurrent_startup_migrates_legacy_database_once(self):
        from concurrent.futures import ThreadPoolExecutor
        job = self.store.create_job("https://example.com/playlist")
        with self.store._connect() as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN automatic_retry_rounds")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: AntraStore(self.db_path).get_job(job["id"]), range(2)))
        self.assertEqual([row["automatic_retry_rounds"] for row in results], [0, 0])

    def test_reproduction_duplicate_tracks_block_download_until_continue(self):
        """Reproduction 3: Overlapping playlist sets status='blocked' and does NOT call submit_job;

        after continue_job, submit_job proceeds.
        """
        # Seed saved track in library
        j_old = self.store.create_job("https://tidal.com/album/old", destination="private")
        self.store.set_tracks(j_old["id"], [{"index": 0, "title": "Saved Song", "artist": "Artist A"}])
        self.store.update_track(j_old["id"], 0, status="completed")

        # Now queue new playlist containing the same song
        job = self.store.create_job("https://tidal.com/playlist/new-with-dup", destination="private")
        self.client.resolve = MagicMock(return_value={
            "title": "New Playlist",
            "tracks": [
                {"index": 0, "title": "Saved Song", "artist": "Artist A"},
                {"index": 1, "title": "New Unique Song", "artist": "Artist B"},
            ]
        })
        self.client.submit_job = MagicMock(return_value=("upstream-123", "token-abc"))

        # Process job without duplicate_confirmed
        self.worker.process_job(job)

        # Must hold in 'blocked' status, and submit_job must NOT be called
        j_state = self.store.get_job(job["id"])
        self.assertEqual(j_state["status"], "blocked")
        self.assertIn("Blocked: playlist contains 1 duplicate track", j_state["message"])
        self.client.submit_job.assert_not_called()

        # User continues download anyway
        self.store.continue_job(job["id"])
        j_continued = self.store.get_job(job["id"])
        self.assertEqual(j_continued["status"], "queued")
        self.assertEqual(self.store.get_internal_job(job["id"])["duplicate_confirmed"], 1)

        # Process job again now that duplicate is confirmed -> submit_job IS called
        self.client.connect_job_ws = MagicMock(return_value=[{"type": "complete"}])
        self.worker.process_job(j_continued)
        self.client.submit_job.assert_called_once()

    def test_reproduction_concurrency_429_does_not_exhaust_transport_budget(self):
        """Reproduction 2: retry_attempts=9 with 429 does NOT fail terminal and does NOT increment attempts."""
        job = self.store.create_job("https://tidal.com/album/busy-concur", destination="private")
        self.store.update_job(job["id"], retry_attempts=9)

        self.client.resolve = MagicMock(
            side_effect=ConcurrencyLimitError("busy", retry_after=30)
        )
        self.worker.process_job(job)

        j_state = self.store.get_internal_job(job["id"])
        self.assertNotEqual(j_state["status"], "failed")
        self.assertEqual(j_state["retry_attempts"], 9)
        self.assertGreater(j_state["concurrency_retry_after"], time.time())


if __name__ == "__main__":
    unittest.main()
