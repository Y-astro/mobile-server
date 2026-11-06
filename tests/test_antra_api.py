"""Tests for Antra HTTP API dispatcher, authorization, HEAD semantics, and same-origin protections."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from antra_store import AntraStore
from antra_client import AntraClient
from antra_http import dispatch_antra
from auth import hash_password, save_users
import dashboard


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


import io
from typing import Optional


class TestAntraApiUnit(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "queue.sqlite3"
        self.output_dir = self.base_dir / "Music" / "Antra"
        self.store = AntraStore(db_path=self.db_path, output_dir=self.output_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_anonymous_request_refused_before_state_access(self):
        handler = MockHandler(
            command="GET",
            path="/api/antra/jobs",
            user=None,
        )
        dispatch_antra(handler, store=self.store)
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
        dispatch_antra(handler, user="family_user", store=self.store)
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
        dispatch_antra(handler, user="admin_user", store=self.store)
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.response_headers.get("Content-Type"), "application/json")
        self.assertEqual(handler.response_headers.get("Cache-Control"), "private, no-store")
        self.assertIn("Content-Length", handler.response_headers)
        self.assertGreater(int(handler.response_headers["Content-Length"]), 0)
        # Empty body for HEAD
        self.assertEqual(len(handler.get_response_body()), 0)

    def test_same_origin_post_protection(self):
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
        dispatch_antra(handler_cross, user="admin_user", store=self.store)
        self.assertEqual(handler_cross.status_code, 403)
        self.assertIn("Cross-origin", handler_cross.get_json_body().get("message", ""))

        # 2. Origin mismatch rejected
        handler_evil_origin = MockHandler(
            command="POST",
            path="/api/antra/jobs",
            headers={
                "Host": "localhost:8001",
                "Origin": "https://evil-attacker.com",
                "Content-Length": str(len(body_bytes)),
            },
            body=body_bytes,
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler_evil_origin, user="admin_user", store=self.store)
        self.assertEqual(handler_evil_origin.status_code, 403)

        # 3. Same-origin allowed
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
        dispatch_antra(handler_same, user="admin_user", store=self.store)
        self.assertEqual(handler_same.status_code, 202)
        resp = handler_same.get_json_body()
        self.assertIn("job", resp)
        self.assertEqual(resp["job"]["status"], "queued")

    def test_url_allowlist_validation(self):
        # Arbitrary / non-allowlist URLs must be rejected with 400
        disallowed = [
            "http://open.spotify.com/album/test",  # plain HTTP
            "https://evil.com/spotify.com",        # attacker domain
            "https://localhost/foo",               # local SSRF attempt
            "https://127.0.0.1/admin",             # IP SSRF attempt
            "ftp://files.com/song.mp3",            # non-http scheme
            "not-a-url",
            "",
        ]
        for bad_url in disallowed:
            payload = json.dumps({"url": bad_url}).encode()
            handler = MockHandler(
                command="POST",
                path="/api/antra/jobs",
                headers={
                    "Host": "localhost:8001",
                    "Sec-Fetch-Site": "same-origin",
                    "Content-Length": str(len(payload)),
                },
                body=payload,
                user="admin_user",
                role="admin",
            )
            dispatch_antra(handler, user="admin_user", store=self.store)
            self.assertEqual(handler.status_code, 400, f"Expected 400 for {bad_url}")

    def test_job_cancel_and_retry_api_transitions(self):
        job = self.store.create_job("https://tidal.com/album/12345")
        job_id = job["id"]

        # Cancel job -> 202
        cancel_handler = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/cancel",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(cancel_handler, user="admin_user", store=self.store)
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
        dispatch_antra(cancel_handler2, user="admin_user", store=self.store)
        self.assertEqual(cancel_handler2.status_code, 409)

        # Retry cancelled job -> 202
        retry_handler = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/retry",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(retry_handler, user="admin_user", store=self.store)
        self.assertEqual(retry_handler.status_code, 202)
        resp2 = retry_handler.get_json_body()
        self.assertEqual(resp2["job"]["status"], "queued")

        # Retry queued job -> 409 Conflict
        retry_handler2 = MockHandler(
            command="POST",
            path=f"/api/antra/jobs/{job_id}/retry",
            headers={"Host": "localhost:8001", "Sec-Fetch-Site": "same-origin"},
            user="admin_user",
            role="admin",
        )
        dispatch_antra(retry_handler2, user="admin_user", store=self.store)
        self.assertEqual(retry_handler2.status_code, 409)

    def test_secret_redaction(self):
        job = self.store.create_job("https://tidal.com/album/sample")
        job_id = job["id"]
        # Populate tracks and internal fields
        self.store.update_job(job_id, upstream_job_id="secret_upstream_id_12345")
        self.store.set_tracks(job_id, [{"index": 0, "title": "Track 1"}])

        handler = MockHandler(
            command="GET",
            path=f"/api/antra/jobs/{job_id}",
            user="admin_user",
            role="admin",
        )
        dispatch_antra(handler, user="admin_user", store=self.store)
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

        self.orig_users_file = dashboard.USER_STORE.path
        dashboard.USER_STORE.path = self.users_file
        dashboard.USER_STORE.invalidate()

        # Spin up Dashboard on free port
        self.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        dashboard.USER_STORE.path = self.orig_users_file
        self.tmp_dir.cleanup()

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps({"username": username, "password": password})
        conn.request("POST", "/api/login", body=payload, headers={"Content-Type": "application/json", "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie", "")
        conn.close()
        return cookie

    def test_dashboard_anonymous_401(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/antra/jobs")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.getheader("Cache-Control"), "private, no-store")

    def test_dashboard_family_403(self):
        cookie = self._login("family", "family_pass")
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/antra/jobs", headers={"Cookie": cookie})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.getheader("Cache-Control"), "private, no-store")

    def test_dashboard_admin_200_and_post(self):
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
        self.assertEqual(body["settings"]["quality"], "24-bit FLAC")

        # 2. POST /api/antra/jobs
        conn2 = http.client.HTTPConnection("127.0.0.1", self.port)
        post_payload = json.dumps({"url": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj"}).encode()
        conn2.request(
            "POST",
            "/api/antra/jobs",
            body=post_payload,
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "Content-Length": str(len(post_payload)),
                "Sec-Fetch-Site": "same-origin",
            },
        )
        resp2 = conn2.getresponse()
        body2 = json.loads(resp2.read().decode())
        conn2.close()
        self.assertEqual(resp2.status, 202)
        self.assertIn("job", body2)
        job_id = body2["job"]["id"]

        # 3. GET /api/antra/jobs/<id>
        conn3 = http.client.HTTPConnection("127.0.0.1", self.port)
        conn3.request("GET", f"/api/antra/jobs/{job_id}", headers={"Cookie": cookie})
        resp3 = conn3.getresponse()
        body3 = json.loads(resp3.read().decode())
        conn3.close()
        self.assertEqual(resp3.status, 200)
        self.assertEqual(body3["job"]["id"], job_id)
        self.assertIn("tracks", body3)


if __name__ == "__main__":
    unittest.main()
