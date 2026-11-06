"""Tests for Antra SQLite store operations, state transitions, and schemas."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from app.antra_store import (
    AntraStore,
    InvalidTransitionError,
    JobNotFoundError,
)


class TestAntraStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "state" / "queue.sqlite3"
        self.output_dir = self.base_dir / "Music" / "Antra"
        self.store = AntraStore(db_path=self.db_path, output_dir=self.output_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_database_init_permissions(self):
        self.assertTrue(self.db_path.parent.is_dir())
        self.assertTrue(self.db_path.is_file())
        stat_dir = os.stat(self.db_path.parent)
        stat_file = os.stat(self.db_path)
        self.assertEqual(stat_dir.st_mode & 0o777, 0o700)
        self.assertEqual(stat_file.st_mode & 0o777, 0o600)

    def test_worker_status_lifecycle(self):
        status = self.store.get_worker_status()
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["updated_at"])

        self.store.set_worker_status("running", "Processing queue")
        updated = self.store.get_worker_status()
        self.assertEqual(updated["state"], "running")
        self.assertEqual(updated["message"], "Processing queue")
        self.assertIsNotNone(updated["updated_at"])

    def test_create_and_get_job(self):
        job = self.store.create_job(
            url="https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            title="Sample Album",
        )
        self.assertIn("id", job)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["total"], 0)
        self.assertEqual(job["completed"], 0)

        fetched = self.store.get_job(job["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], job["id"])
        self.assertEqual(fetched["url"], job["url"])

        # Private fields not leaked in public job dict
        self.assertNotIn("upstream_job_id", fetched)
        self.assertNotIn("format", fetched)

    def test_tracks_lifecycle_and_counts(self):
        job = self.store.create_job(url="https://open.spotify.com/album/sample")
        job_id = job["id"]

        tracks = [
            {"index": 0, "title": "Track 1", "artist": "Artist A"},
            {"index": 1, "title": "Track 2", "artist": "Artist A"},
            {"index": 2, "title": "Track 3", "artist": "Artist A"},
        ]
        self.store.set_tracks(job_id, tracks)
        job_updated = self.store.get_job(job_id)
        self.assertEqual(job_updated["total"], 3)
        self.assertEqual(job_updated["completed"], 0)

        # Update track 0 to completed
        self.store.update_track(
            job_id,
            0,
            status="completed",
            bit_depth=24,
            sample_rate=96000,
            filename="Artist A/Track 1.flac",
        )
        # Update track 1 to skipped
        self.store.update_track(
            job_id,
            1,
            status="skipped",
            bit_depth=16,
            sample_rate=44100,
            message="Skipped: 16-bit FLAC",
        )
        # Update track 2 to failed
        self.store.update_track(
            job_id,
            2,
            status="failed",
            message="Connection dropped",
        )

        job_counts = self.store.get_job(job_id)
        self.assertEqual(job_counts["completed"], 1)
        self.assertEqual(job_counts["skipped"], 1)
        self.assertEqual(job_counts["failed"], 1)

        track_records = self.store.get_tracks(job_id)
        self.assertEqual(len(track_records), 3)
        self.assertEqual(track_records[0]["status"], "completed")
        self.assertEqual(track_records[0]["bit_depth"], 24)
        self.assertEqual(track_records[0]["sample_rate"], 96000)
        self.assertEqual(track_records[1]["status"], "skipped")
        self.assertEqual(track_records[2]["status"], "failed")

    def test_cancel_job_transitions(self):
        job = self.store.create_job(url="https://open.spotify.com/album/sample")
        job_id = job["id"]

        tracks = [
            {"index": 0, "title": "T1", "status": "completed"},
            {"index": 1, "title": "T2", "status": "downloading"},
            {"index": 2, "title": "T3", "status": "queued"},
        ]
        self.store.set_tracks(job_id, tracks)
        self.store.update_track(job_id, 0, status="completed")
        self.store.update_track(job_id, 1, status="downloading")

        cancelled = self.store.cancel_job(job_id)
        self.assertEqual(cancelled["status"], "cancelled")

        # In-flight and queued tracks become cancelled, completed tracks preserved
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[1]["status"], "cancelled")
        self.assertEqual(trks[2]["status"], "cancelled")

        # Cannot cancel an already cancelled job
        with self.assertRaises(InvalidTransitionError):
            self.store.cancel_job(job_id)

    def test_retry_job_transitions(self):
        job = self.store.create_job(url="https://open.spotify.com/album/sample")
        job_id = job["id"]

        # Cannot retry a queued job
        with self.assertRaises(InvalidTransitionError):
            self.store.retry_job(job_id)

        tracks = [
            {"index": 0, "title": "T1"},
            {"index": 1, "title": "T2"},
        ]
        self.store.set_tracks(job_id, tracks)
        self.store.update_track(job_id, 0, status="completed")
        self.store.update_track(job_id, 1, status="failed", message="Network error")
        self.store.update_job(job_id, status="failed", message="Failed tracks")

        # Retry failed job
        retried = self.store.retry_job(job_id)
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["message"], "")

        # Completed track preserved, failed track reset to queued
        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[1]["status"], "queued")
        self.assertEqual(trks[1]["message"], "")

    def test_next_queued_job_selection(self):
        self.assertIsNone(self.store.get_next_queued_job())

        j1 = self.store.create_job("https://tidal.com/album/1")
        time.sleep(0.01)
        j2 = self.store.create_job("https://tidal.com/album/2")

        next_job = self.store.get_next_queued_job()
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["id"], j1["id"])

        self.store.update_job(j1["id"], status="downloading")
        next_job2 = self.store.get_next_queued_job()
        self.assertIsNotNone(next_job2)
        self.assertEqual(next_job2["id"], j2["id"])


if __name__ == "__main__":
    unittest.main()
