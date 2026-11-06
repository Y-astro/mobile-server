"""Tests for Antra SQLite store operations, state transitions, destinations, and schemas."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from app.antra_store import (
    AntraStore,
    InvalidTransitionError,
    JobNotFoundError,
    LOGICAL_OUTPUT_DIRECTORIES,
)


class TestAntraStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "state" / "queue.sqlite3"
        self.output_roots = {
            "private": self.base_dir / "Music" / "Astro",
            "family": self.base_dir / "Music" / "Family",
        }
        self.store = AntraStore(db_path=self.db_path, output_roots=self.output_roots)

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

    def test_create_job_destinations(self):
        # Default destination is private
        job_def = self.store.create_job(
            url="https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            title="Private Album",
        )
        self.assertEqual(job_def["destination"], "private")

        # Family destination
        job_fam = self.store.create_job(
            url="https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            title="Family Album",
            destination="family",
        )
        self.assertEqual(job_fam["destination"], "family")

        # Invalid destination defaults to private
        job_inv = self.store.create_job(
            url="https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            destination="unknown_dest",
        )
        self.assertEqual(job_inv["destination"], "private")

        # Public fields check: no leaked internal fields
        fetched = self.store.get_job(job_def["id"])
        self.assertNotIn("upstream_job_id", fetched)
        self.assertNotIn("format", fetched)
        self.assertNotIn("submission_intent", fetched)

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

        self.store.update_track(
            job_id,
            0,
            status="completed",
            bit_depth=24,
            sample_rate=96000,
            filename="Artist A/Track 1.flac",
        )
        self.store.update_track(
            job_id,
            1,
            status="skipped",
            bit_depth=16,
            sample_rate=44100,
            message="Skipped: 16-bit FLAC",
        )
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

        self.assertFalse(self.store.is_job_cancelled(job_id))
        cancelled = self.store.cancel_job(job_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(self.store.is_job_cancelled(job_id))

        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[1]["status"], "cancelled")
        self.assertEqual(trks[2]["status"], "cancelled")

        with self.assertRaises(InvalidTransitionError):
            self.store.cancel_job(job_id)

    def test_retry_job_preserves_destination(self):
        job = self.store.create_job(
            url="https://open.spotify.com/album/sample",
            destination="family",
        )
        job_id = job["id"]

        tracks = [
            {"index": 0, "title": "T1"},
            {"index": 1, "title": "T2"},
        ]
        self.store.set_tracks(job_id, tracks)
        self.store.update_track(job_id, 0, status="completed")
        self.store.update_track(job_id, 1, status="failed", message="Network error")
        self.store.update_job(
            job_id,
            status="failed",
            message="Failed tracks",
            submission_intent=1,
            concurrency_retry_after=time.time() + 100,
            retry_attempts=3,
        )

        retried = self.store.retry_job(job_id)
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["destination"], "family")
        self.assertEqual(retried["message"], "")

        # Internal fields reset
        internal = self.store.get_internal_job(job_id)
        self.assertEqual(internal["submission_intent"], 0)
        self.assertEqual(internal["concurrency_retry_after"], 0)
        self.assertEqual(internal["retry_attempts"], 0)

        trks = self.store.get_tracks(job_id)
        self.assertEqual(trks[0]["status"], "completed")
        self.assertEqual(trks[1]["status"], "queued")

    def test_get_next_runnable_job_handles_restart(self):
        self.assertIsNone(self.store.get_next_runnable_job())

        # 1. Normal queued job
        j1 = self.store.create_job("https://tidal.com/album/1")
        time.sleep(0.01)

        # 2. Interrupted downloading job from previous run
        j2 = self.store.create_job("https://tidal.com/album/2")
        self.store.update_job(j2["id"], status="downloading", upstream_job_id="up_2")

        # 3. Job with active concurrency backoff in future
        j3 = self.store.create_job("https://tidal.com/album/3")
        self.store.update_job(j3["id"], concurrency_retry_after=time.time() + 3600)

        # First runnable is j1 (oldest queued)
        first = self.store.get_next_runnable_job()
        self.assertEqual(first["id"], j1["id"])

        self.store.update_job(j1["id"], status="completed")

        # Second runnable is j2 (interrupted downloading job recovered across restart!)
        second = self.store.get_next_runnable_job()
        self.assertEqual(second["id"], j2["id"])

        self.store.update_job(j2["id"], status="completed")

        # j3 is in future backoff, so not runnable yet
        self.assertIsNone(self.store.get_next_runnable_job())


if __name__ == "__main__":
    unittest.main()
