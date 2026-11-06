"""Regression checks for provider ambiguity, filesystem races, replay and strict audio."""
import io
import json
from pathlib import Path
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from tests import test_antra_worker as fixtures
from antra_client import AntraClient, AntraClientError, DailyLimitError, UncertainSubmissionError
from antra_store import InvalidTransitionError
import antra_worker as worker_module


class AntraSafetyTests(unittest.TestCase):
    setUp = fixtures.TestAntraWorker.setUp
    tearDown = fixtures.TestAntraWorker.tearDown
    _write_credentials = fixtures.TestAntraWorker._write_credentials

    def prepare_track(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/track/fixture")
        self.store.set_tracks(job["id"], [{"index": 0, "title": "Fixture"}])
        self.store.update_job(job["id"], status="downloading", upstream_job_id="known")
        audio = self.base_dir / "source.flac"
        fixtures.generate_real_flac(audio, bits=24, sample_rate=48000)
        self.client.download_track_stream = MagicMock(side_effect=lambda *args: io.BytesIO(audio.read_bytes()))
        self.client.ack_track = MagicMock(return_value=True)
        return job, audio

    def download(self, job):
        self.worker._download_and_publish_track(job["id"], self.output_roots["private"], "known", 0, 0, "Artist/Track.flac")

    def test_known_token_failure_does_not_submit_duplicate(self):
        job, _ = self.prepare_track()
        self.client.get_job_status = MagicMock(return_value={"status": "downloading"})
        self.client.fresh_ws_token = MagicMock(side_effect=AntraClientError("ws_token=DO_NOT_EXPOSE"))
        self.client.submit_job = MagicMock()
        self.worker.process_job(job)
        self.client.submit_job.assert_not_called()
        self.assertEqual(self.store.get_internal_job(job["id"])["upstream_job_id"], "known")
        self.assertNotIn("DO_NOT_EXPOSE", self.store.get_job(job["id"])["message"])
        self.assertIsNone(self.store.get_next_runnable_job())

    def test_failed_upstream_job_is_not_reconnected_or_resubmitted(self):
        job, _ = self.prepare_track()
        self.client.get_job_status = MagicMock(return_value={"status": "failed"})
        self.client.fresh_ws_token = MagicMock()
        self.client.submit_job = MagicMock()
        self.worker.process_job(job)
        self.assertEqual(self.store.get_job(job["id"])["status"], "failed")
        self.client.fresh_ws_token.assert_not_called()
        self.client.submit_job.assert_not_called()

    def test_rename_before_database_commit_recovers_without_downloading(self):
        job, _ = self.prepare_track()
        update = self.store.update_track
        def interrupted(*args, **kwargs):
            if kwargs.get("status") == "completed":
                raise RuntimeError("owned fixture commit interruption")
            return update(*args, **kwargs)
        with patch.object(self.store, "update_track", side_effect=interrupted):
            self.download(job)
        self.assertEqual(len(list(self.output_roots["private"].rglob("*.flac"))), 1)
        self.download(job)
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "completed")
        self.assertEqual(self.client.download_track_stream.call_count, 1)
        self.assertEqual(len(list(self.output_roots["private"].rglob("*.flac"))), 1)

    def test_unclaimed_temporary_file_is_preserved(self):
        job, _ = self.prepare_track()
        sentinel = self.output_roots["private"] / ".old.part"
        sentinel.write_bytes(b"USER_REPLACEMENT")
        self.store.update_track(job["id"], 0, temporary_path=sentinel.name)
        self.download(job)
        self.assertEqual(sentinel.read_bytes(), b"USER_REPLACEMENT")
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "completed")

    def test_atomic_publication_never_overwrites_late_collision(self):
        job, _ = self.prepare_track()
        publish = worker_module.publish_noreplace
        target = []
        def collision(source, destination):
            destination.write_bytes(b"USER_FILE_CREATED_DURING_DOWNLOAD")
            target.append(destination)
            publish(source, destination)
        with patch.object(worker_module, "publish_noreplace", side_effect=collision):
            self.download(job)
        self.assertEqual(target[0].read_bytes(), b"USER_FILE_CREATED_DURING_DOWNLOAD")
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "failed")
        self.client.ack_track.assert_not_called()
        self.assertEqual(list(self.output_roots["private"].rglob("*.part")), [])

    def test_android_directory_publication_and_replay(self):
        job, audio = self.prepare_track()
        with patch.object(worker_module, "publish_noreplace", side_effect=OSError(22, "unsupported mount")):
            self.download(job)
        record = self.store.get_tracks(job["id"])[0]
        final = self.output_roots["private"] / record["filename"]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(final.read_bytes(), audio.read_bytes())
        self.assertTrue(final.parent.name.endswith("-0"))
        self.download(job)
        self.assertEqual(self.client.download_track_stream.call_count, 1)
        self.assertEqual(list(self.output_roots["private"].rglob(".antra-commit-*")), [])

    def test_android_directory_publication_preserves_racing_user_data(self):
        job, _ = self.prepare_track()
        rename = worker_module.os.rename
        targets = []
        def racing(source, destination):
            source, destination = Path(source), Path(destination)
            if source.is_dir():
                destination.mkdir()
                sentinel = destination / "USER_MUSIC"
                sentinel.write_bytes(b"KEEP")
                targets.append(sentinel)
            return rename(source, destination)
        with patch.object(worker_module, "publish_noreplace", side_effect=OSError(22, "unsupported mount")), patch.object(worker_module.os, "rename", side_effect=racing):
            self.download(job)
        self.assertEqual(targets[0].read_bytes(), b"KEEP")
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "failed")
        self.client.ack_track.assert_not_called()

    def test_cancel_after_validation_does_not_publish(self):
        job, _ = self.prepare_track()
        validate = getattr(worker_module, "verify_flac", worker_module.verify_flac_24bit)
        def cancel(*args, **kwargs):
            result = validate(*args, **kwargs)
            self.store.cancel_job(job["id"])
            return result
        with patch.object(worker_module, "verify_flac", side_effect=cancel):
            self.download(job)
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "cancelled")
        self.assertEqual(list(self.output_roots["private"].rglob("*.flac")), [])
        self.assertEqual(list(self.output_roots["private"].rglob("*.part")), [])
        self.client.ack_track.assert_not_called()

    def test_interrupted_stream_is_closed_and_temporary_file_removed(self):
        job, _ = self.prepare_track()
        class BrokenStream(io.BytesIO):
            def read(self, *args):
                raise TimeoutError("PRIVATE_REMOTE_TOKEN")
        stream = BrokenStream(b"")
        self.client.download_track_stream = MagicMock(return_value=stream)
        self.download(job)
        self.assertTrue(stream.closed)
        self.assertEqual(list(self.output_roots["private"].rglob("*.part")), [])
        self.assertNotIn("PRIVATE_REMOTE_TOKEN", json.dumps(self.store.get_tracks(job["id"])))

    def test_unprovisioned_family_scope_blocks_private_writes(self):
        job, _ = self.prepare_track()
        home = self.base_dir / "phone-home"
        (home / "family_share").mkdir(parents=True)
        (home / "family_share/Music").symlink_to(self.base_dir / "Music")
        self.worker.is_test_env = False
        with patch.object(worker_module, "DEFAULT_OUTPUT_ROOTS", self.output_roots), patch.object(Path, "home", return_value=home):
            self.worker.process_job(job)
        self.assertEqual(self.store.get_job(job["id"])["status"], "blocked")
        self.client.download_track_stream.assert_not_called()

    def test_missing_audio_verifier_does_not_consume_provider_quota(self):
        job, _ = self.prepare_track()
        self.client.submit_job = MagicMock()
        with patch.object(worker_module.shutil, "which", return_value=None):
            self.worker.process_job(job)
        self.assertEqual(self.store.get_job(job["id"])["status"], "blocked")
        self.client.submit_job.assert_not_called()
        self.client.download_track_stream.assert_not_called()

    def test_daily_quota_becomes_runnable_only_when_due(self):
        self._write_credentials()
        job = self.store.create_job("https://tidal.com/playlist/fixture")
        self.client.resolve = MagicMock(side_effect=DailyLimitError("quota", retry_after=12))
        self.worker.process_job(job)
        self.assertEqual(self.store.get_job(job["id"])["status"], "blocked")
        self.assertIsNone(self.store.get_next_runnable_job())
        self.store.update_job(job["id"], concurrency_retry_after=time.time() - 1)
        self.assertEqual(self.store.get_next_runnable_job()["id"], job["id"])

    def test_uncertain_submission_requires_review_and_skips_stay_skipped(self):
        job, _ = self.prepare_track()
        self.store.update_job(job["id"], status="uncertain", submission_intent=1)
        with self.assertRaises(InvalidTransitionError):
            self.store.retry_job(job["id"])
        self.store.update_job(job["id"], status="partial", submission_intent=0)
        self.store.update_track(job["id"], 0, status="skipped", bit_depth=16)
        self.store.retry_job(job["id"])
        self.assertEqual(self.store.get_tracks(job["id"])[0]["status"], "skipped")

    def test_plausible_header_without_valid_audio_is_rejected(self):
        path = self.base_dir / "invalid.flac"
        path.write_bytes(fixtures.create_header_only_flac() + b"\xff\xf8" + b"\0" * 100)
        self.assertFalse(worker_module.verify_flac(path)[0])

    def test_large_metadata_does_not_require_loading_cover_art(self):
        _, audio = self.prepare_track()
        data = audio.read_bytes()
        self.assertEqual(data[4] & 0x80, 0)
        padding = b"\x01" + (300000).to_bytes(3, "big") + b"\0" * 300000
        audio.write_bytes(data[:42] + padding + data[42:])
        self.assertTrue(worker_module.verify_flac(audio)[0])

    def test_acknowledgement_retry_does_not_fetch_audio_again(self):
        job, _ = self.prepare_track()
        self.client.ack_track = MagicMock(return_value=False)
        self.download(job)
        self.assertEqual(self.store.get_internal_track(job["id"], 0)["ack_pending"], 1)
        self.store.update_job(job["id"], status="completed")
        self.store.update_track(job["id"], 0, ack_retry_after=0)
        self.client.ack_track.return_value = True
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.store.get_internal_track(job["id"], 0)["ack_pending"], 0)
        self.assertEqual(self.client.download_track_stream.call_count, 1)

    def test_real_http_authentication_redirect_refusal_and_ambiguous_submission(self):
        self._write_credentials()
        seen = {"login": 0, "trap": 0, "jobs": 0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, code, body, **headers):
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                payload = json.dumps(body).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_GET(self):
                if self.path == "/redirect":
                    self.reply(302, {}, Location="/trap")
                else:
                    seen["trap"] += 1
                    self.reply(200, {})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if self.path == "/api/auth/login":
                    seen["login"] += 1
                    self.reply(200, {}, **{"Set-Cookie": "session=fixture; Path=/; HttpOnly"})
                elif self.path == "/api/resolve":
                    if self.headers.get("Cookie") != "session=fixture":
                        self.reply(401, {})
                    else:
                        self.reply(200, {"tracks": [{"title": "Fixture"}], "release_name": "Owned fixture"})
                elif self.path == "/api/jobs":
                    seen["jobs"] += 1
                    self.reply(500, {})
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AntraClient(base_url=f"http://127.0.0.1:{server.server_port}", config_dir=self.config_dir)
            self.assertEqual(client.resolve("https://tidal.com/track/fixture")["total"], 1)
            self.assertEqual(seen["login"], 1)
            self.assertEqual((self.config_dir / "cookies.txt").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(AntraClientError):
                client._request("GET", "/redirect")
            self.assertEqual(seen["trap"], 0)
            with self.assertRaises(UncertainSubmissionError):
                client.submit_job("https://tidal.com/track/fixture", 0, 1)
            self.assertEqual(seen["jobs"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_submit_job_success_payload_and_validation(self):
        client = AntraClient(base_url="https://antra.fake.local", config_dir=self.config_dir)
        captured = {}
        def fake_request(method, endpoint, data=None, is_submission=False):
            captured["method"] = method
            captured["endpoint"] = endpoint
            captured["data"] = data
            captured["is_submission"] = is_submission
            return 200, {}, json.dumps({"job_id": "job_valid_123", "ws_token": "token_xyz"}).encode("utf-8")

        client._request = fake_request
        job_id, ws_token = client.submit_job("https://open.spotify.com/playlist/fake", 0, 5, format_pref="flac")
        self.assertEqual(job_id, "job_valid_123")
        self.assertEqual(ws_token, "token_xyz")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["endpoint"], "/api/jobs")
        self.assertTrue(captured["is_submission"])
        self.assertEqual(captured["data"]["url"], "https://open.spotify.com/playlist/fake")
        self.assertEqual(captured["data"]["start_index"], 0)
        self.assertEqual(captured["data"]["end_index"], 5)
        self.assertTrue(captured["data"]["client_packaging"])
        self.assertEqual(captured["data"]["format"], "flac")
