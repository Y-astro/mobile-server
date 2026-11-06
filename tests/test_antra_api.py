"""Tests for Antra HTTP API dispatcher, authorization, HEAD semantics, and same-origin protections."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Dict, Optional
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from antra_store import AntraStore, LOGICAL_OUTPUT_DIRECTORIES
from antra_client import AntraClient
from antra_http import dispatch_antra
from auth import hash_password, save_users


class MockHandler:
    """Mock HTTP request handler simulating BaseHTTPRequestHandler."""

    def __init__(
        self,
        command: str = "GET",
        path: str = "/api/antra/jobs",
        headers: Optional[dict] = None,
        body: bytes = b"",
        user: Optional[str] = None,
        role: str = "admin",
    ):
        self.command = command
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status_code = None
        self.response_headers = {}
        self.user = user
        self.role = role

    def send_response(self, code, message=None):
        self.status_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = str(value)

    def end_headers(self):
        pass

    def get_session_user(self):
        return self.user

    def is_admin(self, username):
        return self.role == "admin" and username == self.user

    def get_response_body(self) -> bytes:
        return self.wfile.getvalue()

    def get_json_body(self) -> dict:
        data = self.get_response_body()
        return json.loads(data.decode("utf-8")) if data else {}


class TestAntraApiUnit(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "queue.sqlite3"
        self.output_roots = {
            "private": self.base_dir / "Music" / "Astro",
            "family": self.base_dir / "Music" / "Family",
        }
        self.store = AntraStore(db_path=self.db_path, output_roots=self.output_roots)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_anonymous_request_refused_before_state_access(self):
        handler = MockHandler(
            command="GET",
            path="/api/antra/jobs",
            user=None,
        )
        dispatch_antra(handler, store=self.store, is_admin=lambda u: False)
        self.assertEqual(handler.status_code, 401)
        self.assertEqual(handler.response_headers.get("Cache-Control"), "private, no-store")
        body = handler.get_json_body()
        self.assertEqual(body.get("status"), "error")
        self.assertIn("Authentication", body.get("message"))

    def test_family_role_refused_with_403(self):
        handler = MockHandler(
            command="GET",
            path="/api/antra/jobs",
            user="family_user",
            role="family",
        )
        dispatch_antra(handler, user="family_user", store=self.store, is_admin=lambda u: False)
        self.assertEqual(handler.status_code, 403)
        self.assertEqual(handler.response_headers.get("Cache-Control"), "private, no-store")
        body = handler.get_json_body()
        self.assertEqual(body.get("status"), "error")
        self.assertIn("Administrator", body.get("message"))

    def test_head_semantics_returns_headers_and_no_body(self):
        handler = MockHandler(
            command="HEAD",
            path="/api/antra/jobs",
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.response_headers.get("Content-Type"), "application/json")
        self.assertEqual(handler.response_headers.get("Cache-Control"), "private, no-store")
        self.assertIn("Content-Length", handler.response_headers)
        self.assertGreater(int(handler.response_headers["Content-Length"]), 0)
        self.assertEqual(len(handler.get_response_body()), 0)

    def test_full_effective_same_origin_post_protection(self):
        body_bytes = json.dumps({"url": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj"}).encode()

        # 1. Cross-site Sec-Fetch-Site rejected
        handler_cross = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={
                "Host": "localhost:8001",
                "Sec-Fetch-Site": "cross-site",
                "Content-Length": str(len(body_bytes)),
            },
            body=body_bytes,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_cross, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_cross.status_code, 403)
        self.assertIn("Cross-origin", handler_cross.get_json_body().get("message", ""))

        # 2. Origin scheme mismatch rejected
        handler_bad_scheme = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={
                "Host": "localhost:8001",
                "Origin": "https://localhost:8001",  # Request is http, Origin claims https
                "Content-Length": str(len(body_bytes)),
            },
            body=body_bytes,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_bad_scheme, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_bad_scheme.status_code, 403)

        # 3. Origin port mismatch rejected
        handler_bad_port = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={
                "Host": "localhost:8001",
                "Origin": "http://localhost:9000",
                "Content-Length": str(len(body_bytes)),
            },
            body=body_bytes,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_bad_port, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_bad_port.status_code, 403)

        # 4. Same-origin allowed
        handler_same = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={
                "Host": "localhost:8001",
                "Sec-Fetch-Site": "same-origin",
                "Origin": "http://localhost:8001",
                "Content-Length": str(len(body_bytes)),
            },
            body=body_bytes,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_same, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_same.status_code, 202)
        resp = handler_same.get_json_body()
        self.assertIn("job", resp)
        self.assertEqual(resp["job"]["status"], "queued")
        self.assertEqual(resp["job"]["destination"], "private")

    def test_url_allowlist_and_destinations(self):
        # 1. Invalid destination rejected with 400
        bad_dest_payload = json.dumps({
            "url": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            "destination": "invalid_dest",
        }).encode()
        handler_bad_dest = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin", "Content-Length": str(len(bad_dest_payload))},
            body=bad_dest_payload,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_bad_dest, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_bad_dest.status_code, 400)
        self.assertIn("Invalid destination", handler_bad_dest.get_json_body().get("message", ""))

        # 2. Family destination accepted
        fam_payload = json.dumps({
            "url": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            "destination": "family",
        }).encode()
        handler_fam = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin", "Content-Length": str(len(fam_payload))},
            body=fam_payload,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_fam, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_fam.status_code, 202)
        self.assertEqual(handler_fam.get_json_body()["job"]["destination"], "family")

        # 3. Settings exposes only logical labels (no physical paths)
        handler_settings = MockHandler(
            command="GET",
            path="/api/antra/jobs",
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_settings, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler_settings.status_code, 200)
        settings = handler_settings.get_json_body()["settings"]
        self.assertEqual(settings["output_directories"], LOGICAL_OUTPUT_DIRECTORIES)
        self.assertEqual(settings["output_directory"], "Music/Astro")
        self.assertEqual(settings["quality"], "Best Available FLAC")
        # Physical paths must NOT be present
        self.assertNotIn("/storage", json.dumps(settings))

    def test_invalid_destination_types_and_oversized_length_are_refused(self):
        for destination in (None, [], {}, 1):
            body = json.dumps({"url": "https://tidal.com/track/fixture", "destination": destination}).encode()
            handler = MockHandler(command="POST", path="/api/antra/jobs", user="admin", body=body,
                                  headers={"Host": "localhost", "Content-Length": str(len(body))})
            dispatch_antra(handler, user="admin", store=self.store, is_admin=lambda user: True)
            self.assertEqual(handler.status_code, 400)
        handler = MockHandler(command="POST", path="/api/antra/jobs", user="admin",
                              headers={"Host": "localhost", "Content-Length": "9" * 5000})
        dispatch_antra(handler, user="admin", store=self.store, is_admin=lambda user: True)
        self.assertEqual(handler.status_code, 400)

    def test_job_cancel_and_retry_api_transitions(self):
        job = self.store.create_job("https://tidal.com/album/12345", destination="family")
        job_id = job["id"]

        # Cancel job -> 202
        cancel_handler = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/cancel",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(cancel_handler, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(cancel_handler.status_code, 202)
        resp = cancel_handler.get_json_body()
        self.assertEqual(resp["job"]["status"], "cancelled")

        # Second cancel -> 409 Conflict
        cancel_handler2 = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/cancel",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(cancel_handler2, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(cancel_handler2.status_code, 409)

        # Retry cancelled job -> 202
        retry_handler = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/retry",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(retry_handler, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(retry_handler.status_code, 202)
        resp2 = retry_handler.get_json_body()
        self.assertEqual(resp2["job"]["status"], "queued")
        self.assertEqual(resp2["job"]["destination"], "family")

    def test_secret_redaction(self):
        job = self.store.create_job("https://tidal.com/album/sample")
        job_id = job["id"]
        self.store.update_job(job_id, upstream_job_id="secret_upstream_id_12345")
        self.store.set_tracks(job_id, [{"index": 0, "title": "Track 1"}])

        handler = MockHandler(
            command="GET",
            path=f"/api/antra/jobs/{job_id}",
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler, user="admin_user", store=self.store, is_admin=lambda u: True)
        self.assertEqual(handler.status_code, 200)

        raw_resp = handler.get_response_body().decode("utf-8")
        self.assertNotIn("secret_upstream_id_12345", raw_resp)
        self.assertNotIn("upstream_job_id", raw_resp)
        self.assertNotIn("ws_token", raw_resp)
        self.assertNotIn("cookie", raw_resp.lower())
        self.assertNotIn("password", raw_resp.lower())


class TestDashboardAntraIntegration(unittest.TestCase):
    """Integrated tests spinning up real Dashboard HTTP server exercising /api/antra/."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.config_dir = self.base_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.users_file = self.config_dir / "users.json"
        save_users(
            self.users_file,
            {
                "admin": {"password_hash": hash_password("admin_pass"), "role": "admin"},
                "family": {"password_hash": hash_password("family_pass"), "role": "family"},
            },
        )

        import importlib.util
        import telemetry
        for scoped in (
            patch.dict(os.environ, {"HOME": str(self.base_dir)}),
            patch.object(telemetry.Telemetry, "start", return_value=None),
            patch("antra_store.DEFAULT_DB_PATH", self.base_dir / "antra/queue.sqlite3"),
        ):
            scoped.start()
            self.addCleanup(scoped.stop)
        spec = importlib.util.spec_from_file_location("isolated_antra_dashboard", Path(__file__).resolve().parents[1] / "app/dashboard.py")
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)
        self.dashboard_mod = dashboard
        dashboard.USER_STORE.path = self.users_file
        dashboard.USER_STORE.invalidate()
        dashboard.get_filebrowser_jwt = MagicMock(return_value=None)
        self.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp_dir.cleanup()

    def _login(self, username, password):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps({"username": username, "password": password})
        conn.request("POST", "/api/login", body=payload, headers={"Content-Type": "application/json", "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie", "")
        conn.close()
        return cookie

    def test_dashboard_anonymous_401(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/antra/jobs")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.getheader("Cache-Control"), "private, no-store")

    def test_dashboard_family_403(self):
        import http.client
        cookie = self._login("family", "family_pass")
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/antra/jobs", headers={"Cookie": cookie})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.getheader("Cache-Control"), "private, no-store")

    def test_dashboard_admin_200_and_post(self):
        import http.client
        cookie = self._login("admin", "admin_pass")

        # 1. GET /api/antra/jobs
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/antra/jobs", headers={"Cookie": cookie})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("jobs", body)
        self.assertIn("worker", body)
        self.assertIn("settings", body)
        self.assertEqual(body["settings"]["quality"], "Best Available FLAC")

        # 2. POST /api/antra/jobs with family destination
        conn2 = http.client.HTTPConnection("127.0.0.1", self.port)
        post_payload = json.dumps({
            "url": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
            "destination": "family",
        }).encode()
        conn2.request(
            "POST",
            "/api/antra/jobs",
            body=post_payload,
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "Content-Length": str(len(post_payload)),
                "Sec-Fetch-Site": "same-origin",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        resp2 = conn2.getresponse()
        body2 = json.loads(resp2.read().decode())
        conn2.close()
        self.assertEqual(resp2.status, 202)
        self.assertIn("job", body2)
        self.assertEqual(body2["job"]["destination"], "family")
        job_id = body2["job"]["id"]

        # 3. GET /api/antra/jobs/<id>
        conn3 = http.client.HTTPConnection("127.0.0.1", self.port)
        conn3.request("GET", f"/api/antra/jobs/{job_id}", headers={"Cookie": cookie})
        resp3 = conn3.getresponse()
        body3 = json.loads(resp3.read().decode())
        conn3.close()
        self.assertEqual(resp3.status, 200)
        self.assertEqual(body3["job"]["id"], job_id)
        self.assertEqual(body3["job"]["destination"], "family")
        self.assertIn("tracks", body3)


if __name__ == "__main__":
    unittest.main()
