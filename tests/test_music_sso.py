#!/usr/bin/env python3
"""Focused unit and routing tests for Navidrome Music SSO and boundary enforcement."""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "app"))
sys.path.insert(0, str(REPO_ROOT))

import app.dashboard as dashboard
import app.auth as auth
import app.navidrome as launcher
from tests.test_caddy_routes import adapt_caddyfile


class TestMusicAuthCheckEndpoint(unittest.TestCase):
    def setUp(self):
        self.users = {
            "admin_user": {"role": "admin", "password_hash": "dummy", "disabled": False},
            "family_user": {"role": "family", "password_hash": "dummy", "disabled": False},
            "disabled_admin": {"role": "admin", "password_hash": "dummy", "disabled": True},
        }
        self.mock_store = MagicMock()
        self.mock_store.users = self.users
        self.mock_store.exists.side_effect = lambda u: bool(self.users.get(u) and not self.users[u].get("disabled"))
        self.mock_store.is_admin.side_effect = lambda u: bool(self.users.get(u, {}).get("role") == "admin" and not self.users[u].get("disabled"))

    def _call_music_auth_check(self, user=None, fwd_uri="/music/app/"):
        handler = MagicMock()
        handler.path = "/api/music-auth-check"
        handler.headers = {"X-Forwarded-Uri": fwd_uri}
        headers_sent = {}
        status_sent = []

        handler.get_session_user.return_value = user
        handler.send_response.side_effect = lambda code: status_sent.append(code)
        handler.send_header.side_effect = lambda k, v: headers_sent.update({k: v})
        handler.wfile = MagicMock()

        with patch.object(dashboard, "USER_STORE", self.mock_store), \
             patch.object(dashboard, "is_admin", self.mock_store.is_admin):
            dashboard.DashboardHandler.do_GET(handler)

        return status_sent[0] if status_sent else None, headers_sent

    def test_admin_mapped_to_astro_web(self):
        status, headers = self._call_music_auth_check("admin_user")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Music-User"), "astro-web")
        self.assertEqual(headers.get("X-Music-Role"), "admin")
        self.assertEqual(headers.get("X-Forwarded-User"), "admin_user")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")

    def test_family_mapped_to_family_music(self):
        status, headers = self._call_music_auth_check("family_user")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Music-User"), "family-music")
        self.assertEqual(headers.get("X-Music-Role"), "family")
        self.assertEqual(headers.get("X-Forwarded-User"), "family_user")
        self.assertEqual(headers.get("Cache-Control"), "no-store, private")

    def test_anonymous_navigation_redirects_to_login(self):
        status, headers = self._call_music_auth_check(None, fwd_uri="/music/app/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertNotIn("X-Music-User", headers)

    def test_anonymous_api_returns_401(self):
        status, headers = self._call_music_auth_check(None, fwd_uri="/music/api/album")
        self.assertEqual(status, 401)
        self.assertNotIn("X-Music-User", headers)

    def test_disabled_user_rejected(self):
        status, headers = self._call_music_auth_check("disabled_admin")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertNotIn("X-Music-User", headers)

    def test_unknown_user_rejected(self):
        status, headers = self._call_music_auth_check("nonexistent_user")
        self.assertEqual(status, 302)
        self.assertNotIn("X-Music-User", headers)


class TestNavidromeLauncherConfig(unittest.TestCase):
    def test_launcher_exports_reverse_proxy_env(self):
        with open(REPO_ROOT / "app/navidrome.py", "r", encoding="utf-8") as f:
            code = f.read()
        self.assertIn("ND_REVERSEPROXYUSERHEADER=X-Music-User", code)
        self.assertIn("ND_REVERSEPROXYWHITELIST=127.0.0.1/32,::1/128", code)
        self.assertIn("ND_EXTAUTH_USERHEADER=X-Music-User", code)
        self.assertIn("ND_EXTAUTH_TRUSTEDSOURCES=127.0.0.1/32,::1/128", code)


class TestCaddyMusicRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(REPO_ROOT / "config/Caddyfile", "r", encoding="utf-8") as f:
            cls.caddyfile_text = f.read()

    def test_caddyfile_strips_caller_supplied_headers(self):
        self.assertIn("request_header -X-Music-User", self.caddyfile_text)
        self.assertIn("request_header -Remote-User", self.caddyfile_text)

    def test_caddyfile_has_native_and_sso_routes(self):
        self.assertIn("handle /music/rest/*", self.caddyfile_text)
        self.assertIn("@musicNativeAuth", self.caddyfile_text)
        self.assertIn("@musicNativeBearer", self.caddyfile_text)
        self.assertIn("/api/music-auth-check", self.caddyfile_text)
        self.assertIn("copy_headers X-Music-User", self.caddyfile_text)


if __name__ == "__main__":
    unittest.main()
