#!/usr/bin/env python3
"""Comprehensive behavioral and contract tests for Navidrome Music SSO and boundary enforcement."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "app"))
sys.path.insert(0, str(REPO_ROOT))

import app.dashboard as dashboard
import app.auth as auth
from tests.test_caddy_routes import adapt_caddyfile


class TestMusicAuthCheckContract(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.users_file = Path(self.temp_dir.name) / "users.json"
        self.pw_hash = auth.hash_password("correct-horse-battery-staple", iterations=100_000)
        self.store_data = {
            "version": 1,
            "users": {
                "astro": {
                    "role": "admin",
                    "password_hash": self.pw_hash,
                    "disabled": False,
                },
                "family": {
                    "role": "family",
                    "password_hash": self.pw_hash,
                    "disabled": False,
                },
                "disabled_astro": {
                    "role": "admin",
                    "password_hash": self.pw_hash,
                    "disabled": True,
                },
            },
        }
        self.users_file.write_text(json.dumps(self.store_data))
        self.store = auth.CredentialStore(path=str(self.users_file))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _call_handler(self, path="/api/music-auth-check", cookie=None, fwd_uri="/music/app/"):
        handler = MagicMock()
        handler.path = path
        handler.headers = {"X-Forwarded-Uri": fwd_uri}
        if cookie:
            handler.headers["Cookie"] = cookie
        handler.get_session_user = lambda: dashboard.DashboardHandler.get_session_user(handler)

        headers_sent = {}
        status_sent = []
        body_written = []

        handler.send_response.side_effect = lambda code: status_sent.append(code)
        handler.send_header.side_effect = lambda k, v: headers_sent.update({k: v})
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = lambda b: body_written.append(b)

        with patch.object(dashboard, "USER_STORE", self.store):
            dashboard.DashboardHandler.do_GET(handler)

        return (
            status_sent[0] if status_sent else None,
            headers_sent,
            b"".join(body_written),
        )

    def test_real_admin_token_maps_to_astro_web(self):
        token = dashboard.sign_session_token("astro")
        status, headers, _ = self._call_handler(
            cookie=f"session_token={token}", fwd_uri="/music/app/"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Music-User"), "astro-web")
        self.assertEqual(headers.get("X-Music-Role"), "admin")
        self.assertEqual(headers.get("X-Forwarded-User"), "astro")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")

    def test_real_family_token_maps_to_family_music(self):
        token = dashboard.sign_session_token("family")
        status, headers, _ = self._call_handler(
            cookie=f"session_token={token}", fwd_uri="/music/app/"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Music-User"), "family-music")
        self.assertEqual(headers.get("X-Music-Role"), "family")
        self.assertEqual(headers.get("X-Forwarded-User"), "family")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")

    def test_disabled_user_token_rejected_immediately(self):
        token = dashboard.sign_session_token("disabled_astro")
        status, headers, _ = self._call_handler(
            cookie=f"session_token={token}", fwd_uri="/music/app/"
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertNotIn("X-Music-User", headers)

    def test_tampered_or_expired_token_rejected(self):
        # Expired token
        exp = int(time.time()) - 3600
        data = f"astro:{exp}"
        import hashlib, hmac
        sig = hmac.new(dashboard.SECRET_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()
        expired_token = f"{data}:{sig}"

        status, headers, _ = self._call_handler(
            cookie=f"session_token={expired_token}", fwd_uri="/music/app/"
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertNotIn("X-Music-User", headers)

        # Tampered token
        valid = dashboard.sign_session_token("astro")
        tampered = valid[:-4] + "ffff"
        status, headers, _ = self._call_handler(
            cookie=f"session_token={tampered}", fwd_uri="/music/api/album"
        )
        self.assertEqual(status, 401)
        self.assertNotIn("X-Music-User", headers)

    def test_freshness_check_invalidates_active_token_when_user_disabled(self):
        token = dashboard.sign_session_token("astro")
        status, _, _ = self._call_handler(cookie=f"session_token={token}")
        self.assertEqual(status, 200)

        # Disable user in store
        self.store_data["users"]["astro"]["disabled"] = True
        self.users_file.write_text(json.dumps(self.store_data))
        self.store.invalidate()

        status, headers, _ = self._call_handler(
            cookie=f"session_token={token}", fwd_uri="/music/api/song"
        )
        self.assertEqual(status, 401)
        self.assertNotIn("X-Music-User", headers)


class TestMusicBrowserEntryAndStateContract(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.users_file = Path(self.temp_dir.name) / "users.json"
        self.pw_hash = auth.hash_password("correct-horse-battery-staple", iterations=100_000)
        self.store_data = {
            "version": 1,
            "users": {
                "astro": {"role": "admin", "password_hash": self.pw_hash, "disabled": False},
                "family": {"role": "family", "password_hash": self.pw_hash, "disabled": False},
            },
        }
        self.users_file.write_text(json.dumps(self.store_data))
        self.store = auth.CredentialStore(path=str(self.users_file))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _call_music_entry(self, cookie=None):
        handler = MagicMock()
        handler.path = "/music/"
        handler.headers = {}
        if cookie:
            handler.headers["Cookie"] = cookie
        handler.get_session_user = lambda: dashboard.DashboardHandler.get_session_user(handler)

        headers_sent = {}
        status_sent = []
        body_written = []

        handler.send_response.side_effect = lambda code: status_sent.append(code)
        handler.send_header.side_effect = lambda k, v: headers_sent.update({k: v})
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = lambda b: body_written.append(b)

        with patch.object(dashboard, "USER_STORE", self.store):
            dashboard.DashboardHandler.do_GET(handler)

        return (
            status_sent[0] if status_sent else None,
            headers_sent,
            b"".join(body_written).decode("utf-8"),
        )

    def test_anonymous_music_entry_redirects_to_login(self):
        status, headers, _ = self._call_music_entry(cookie=None)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")

    def test_admin_music_entry_serves_astro_web_bootstrap_script(self):
        token = dashboard.sign_session_token("astro")
        status, headers, body = self._call_music_entry(cookie=f"session_token={token}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")
        self.assertIn('var targetUser = "astro-web";', body)
        self.assertIn('var targetLib = 2;', body)
        self.assertIn('localStorage.removeItem("token");', body)
        self.assertIn('window.location.replace("/music/app/");', body)

    def test_family_music_entry_serves_family_music_bootstrap_script(self):
        token = dashboard.sign_session_token("family")
        status, headers, body = self._call_music_entry(cookie=f"session_token={token}")
        self.assertEqual(status, 200)
        self.assertIn('var targetUser = "family-music";', body)
        self.assertIn('var targetLib = 1;', body)
        self.assertIn('selectedLibraries: [targetLib]', body)


class TestCaddyMusicRoutesContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(REPO_ROOT / "config/Caddyfile", "r", encoding="utf-8") as f:
            cls.caddyfile_text = f.read()

    def test_portal_strips_forged_caller_headers(self):
        self.assertIn("request_header -X-Music-User", self.caddyfile_text)
        self.assertIn("request_header -Remote-User", self.caddyfile_text)

    def test_cookie_precedence_on_subsonic_and_login_routes(self):
        self.assertIn("@musicSubsonicNative", self.caddyfile_text)
        self.assertIn("@musicLoginNative", self.caddyfile_text)
        self.assertIn("@musicBearerNative", self.caddyfile_text)

        lines = self.caddyfile_text.splitlines()
        def get_block(name):
            start = next(i for i, l in enumerate(lines) if name in l)
            end = next(i for i in range(start, len(lines)) if lines[i].strip() == "}")
            return "\n".join(lines[start:end+1])

        subsonic_block = get_block("@musicSubsonicNative")
        self.assertIn("not header Cookie *session_token*", subsonic_block)

        login_block = get_block("@musicLoginNative")
        self.assertIn("not header Cookie *session_token*", login_block)

        bearer_block = get_block("@musicBearerNative")
        self.assertIn("path /music/api/* /music/events/*", bearer_block)
        self.assertIn("not header Cookie *session_token*", bearer_block)

    def test_forward_auth_route_strips_caller_token_headers(self):
        self.assertIn("uri query -jwt", self.caddyfile_text)
        self.assertIn("header_up -Cookie", self.caddyfile_text)
        self.assertIn("header_up -x-nd-authorization", self.caddyfile_text)
        self.assertIn("header_up -Authorization", self.caddyfile_text)
        self.assertIn("copy_headers X-Music-User", self.caddyfile_text)


class TestNavidromeLauncherCanonicalConfig(unittest.TestCase):
    def test_launcher_sets_canonical_supported_extauth_envs(self):
        with open(REPO_ROOT / "app/navidrome.py", "r", encoding="utf-8") as f:
            code = f.read()
        self.assertIn("ND_EXTAUTH_USERHEADER=X-Music-User", code)
        self.assertIn("ND_EXTAUTH_TRUSTEDSOURCES=127.0.0.1/32,::1/128", code)
        self.assertNotIn("ND_REVERSEPROXYUSERHEADER", code)
        self.assertNotIn("ND_REVERSEPROXYWHITELIST", code)


if __name__ == "__main__":
    unittest.main()
