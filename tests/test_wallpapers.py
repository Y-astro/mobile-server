"""Tests for wallpaper data models, offline preparation tools, and HTTP API integration."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.client import HTTPConnection

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import hash_password, save_users
import telemetry
from wallpapers import (
    WallpaperLibrary,
    WallpaperError,
    CorruptLibraryError,
    WallpaperNotFoundError,
    dispatch_wallpapers,
)
from tools.prepare_wallpapers import (
    build_library,
    verify_library,
    publish_library,
    sha256_file,
    compute_display_dimensions,
    compute_contain_dimensions,
)


class WallpaperLibraryUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lib_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_library_returns_empty_contract(self):
        lib = WallpaperLibrary(self.lib_path / "non_existent")
        res = lib.get_public_list()
        self.assertEqual(res, {"version": 1, "default_id": None, "wallpapers": []})

    def test_corrupt_manifest_raises_error(self):
        manifest_file = self.lib_path / "manifest.json"
        manifest_file.write_text("invalid json", encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_invalid_manifest_version_raises_error(self):
        manifest_file = self.lib_path / "manifest.json"
        manifest_file.write_text(json.dumps({"version": 2, "wallpapers": []}), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_missing_derivative_file_raises_corrupt_library_error(self):
        w_id = "a" * 64
        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp"},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp"},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_derivative_symlink_refused(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)

        target = self.lib_path / "target.webp"
        target.write_bytes(b"webpdata")

        symlink = disp_dir / f"{w_id}.webp"
        os.symlink(target, symlink)
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumbdata")

        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp"},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp"},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_get_derivative_guards(self):
        lib = WallpaperLibrary(self.lib_path)
        # Invalid hex ID
        with self.assertRaises(WallpaperNotFoundError):
            lib.get_derivative("../etc/passwd", "thumbnail.webp")
        # Invalid derivative name
        with self.assertRaises(WallpaperNotFoundError):
            lib.get_derivative("a" * 64, "original.png")


class WallpaperToolsUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.src_dir = self.root / "src"
        self.src_dir.mkdir()
        self.dest_dir = self.root / "dest"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_synthetic_image(self, name: str, size: tuple, color: str = "blue"):
        img = Image.new("RGB", size, color=color)
        out_path = self.src_dir / name
        img.save(out_path)
        return out_path

    def test_dimension_calculations(self):
        # Landscape 3840x2160 -> long edge 2560 -> 2560x1440
        disp_w, disp_h = compute_display_dimensions(3840, 2160, 2560)
        self.assertEqual((disp_w, disp_h), (2560, 1440))

        # Portrait 2160x3840 -> long edge 2560 -> 1440x2560
        disp_w, disp_h = compute_display_dimensions(2160, 3840, 2560)
        self.assertEqual((disp_w, disp_h), (1440, 2560))

        # Small image not upscaled: 1920x1080 -> 1920x1080
        disp_w, disp_h = compute_display_dimensions(1920, 1080, 2560)
        self.assertEqual((disp_w, disp_h), (1920, 1080))

        # Thumbnail contain: 2560x1440 into 320x180 -> 320x180
        tw, th = compute_contain_dimensions(2560, 1440, 320, 180)
        self.assertEqual((tw, th), (320, 180))

        # Thumbnail contain: portrait 1440x2560 into 320x180 -> 101x180
        tw, th = compute_contain_dimensions(1440, 2560, 320, 180)
        self.assertLessEqual(tw, 320)
        self.assertLessEqual(th, 180)
        self.assertEqual(th, 180)

    def test_synthetic_build_and_verification(self):
        self._create_synthetic_image("06. Frosted Glass.png", (3000, 2000), "gray")
        self._create_synthetic_image("portrait.jpg", (1200, 2400), "red")
        # Also create an mp4 to ensure exclusion
        (self.src_dir / "clip.mp4").write_bytes(b"fake-video-bytes")

        manifest = build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        self.assertEqual(len(manifest["wallpapers"]), 2)
        # Default ID should match 06. Frosted Glass.png
        frosted_sha = sha256_file(self.src_dir / "06. Frosted Glass.png")
        self.assertEqual(manifest["default_id"], frosted_sha)

        # Stdlib verification
        ok, msg = verify_library(self.dest_dir)
        self.assertTrue(ok, msg)

        # Stdlib publication
        target_dir = self.root / "phone_wallpapers"
        pub_ok, pub_msg = publish_library(self.dest_dir, target_dir)
        self.assertTrue(pub_ok, pub_msg)

        # Idempotent republish
        pub_ok2, pub_msg2 = publish_library(self.dest_dir, target_dir)
        self.assertTrue(pub_ok2, pub_msg2)
        self.assertIn("already up to date", pub_msg2)

        # Conflict refusal
        tampered_file = target_dir / "manifest.json"
        tampered_file.write_text("corrupted", encoding="utf-8")
        pub_fail, pub_fail_msg = publish_library(self.dest_dir, target_dir)
        self.assertFalse(pub_fail)
        self.assertIn("Refusing to overwrite conflicting", pub_fail_msg)

    def test_verify_and_publish_without_pillow(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        target_dir = self.root / "phone_stdlib_only"
        # Mock sys.modules to simulate missing Pillow
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageOps": None}):
            ok, msg = verify_library(self.dest_dir)
            self.assertTrue(ok)
            pub_ok, pub_msg = publish_library(self.dest_dir, target_dir)
            self.assertTrue(pub_ok)


class WallpaperHTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        telemetry_patch = patch.object(telemetry.Telemetry, "start", return_value=None)
        telemetry_patch.start()
        cls.addClassCleanup(telemetry_patch.stop)

        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.web_dir = cls.repo_root / "web"
        cls.temp = tempfile.TemporaryDirectory()
        cls.oldhome = os.environ.get("HOME")
        os.environ["HOME"] = cls.temp.name

        cls.static_dir = Path(cls.temp.name) / ".config/dashboard/static"
        cls.static_dir.mkdir(parents=True, exist_ok=True)
        for page in ["admin.html", "family.html", "login.html"]:
            shutil.copy2(cls.web_dir / page, cls.static_dir / page)

        # Add shared appearance test assets to static dir
        (cls.static_dir / "theme-tokens.css").write_text(":root { --test: 1; }", encoding="utf-8")
        (cls.static_dir / "appearance.css").write_text(".glass { opacity: 0.9; }", encoding="utf-8")
        (cls.static_dir / "appearance.js").write_text("console.log('appearance');", encoding="utf-8")

        save_users(
            Path(cls.temp.name) / ".config/dashboard/users.json",
            {
                "testadmin": {"password_hash": hash_password("test-password-long"), "role": "admin"},
                "testfamily": {"password_hash": hash_password("family-password-long"), "role": "family"},
            },
        )

        spec = importlib.util.spec_from_file_location(
            "isolated_dashboard_wallpapers", cls.repo_root / "app/dashboard.py"
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

        cls.server = cls.mod.ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.DashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()
        if cls.oldhome is not None:
            os.environ["HOME"] = cls.oldhome

    def request(self, method, path, user=None, body=None, headers=None):
        c = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        h = headers or {}
        if user:
            h["Cookie"] = "session_token=" + self.mod.sign_session_token(user)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        result = (r.status, r.read(), {k.lower(): v for k, v in r.getheaders()})
        c.close()
        return result

    def test_unauthenticated_requests_refused_before_reading(self):
        # 401 on unauthenticated list and derivative routes
        st, body, hdrs = self.request("GET", "/api/wallpapers")
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(body)["status"], "error")

        st, body, hdrs = self.request("GET", f"/api/wallpapers/{'a' * 64}/thumbnail.webp")
        self.assertEqual(st, 401)

        st, body, hdrs = self.request("GET", f"/api/wallpapers/{'a' * 64}/display.webp")
        self.assertEqual(st, 401)

    def test_missing_library_returns_empty_200(self):
        # When ~/wallpapers does not exist
        wallpapers_dir = Path(self.temp.name) / "wallpapers"
        if wallpapers_dir.exists():
            shutil.rmtree(wallpapers_dir)

        st, body, hdrs = self.request("GET", "/api/wallpapers", user="testfamily")
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("cache-control"), "private, no-store")
        data = json.loads(body)
        self.assertEqual(data, {"version": 1, "default_id": None, "wallpapers": []})

    def test_corrupt_library_returns_503(self):
        wallpapers_dir = Path(self.temp.name) / "wallpapers"
        wallpapers_dir.mkdir(parents=True, exist_ok=True)
        (wallpapers_dir / "manifest.json").write_text("{corrupt json", encoding="utf-8")

        st, body, hdrs = self.request("GET", "/api/wallpapers", user="testfamily")
        self.assertEqual(st, 503)
        err = json.loads(body)
        self.assertEqual(err["status"], "error")
        self.assertNotIn(str(wallpapers_dir), err["message"])

    def test_authenticated_valid_library_and_derivatives(self):
        wallpapers_dir = Path(self.temp.name) / "wallpapers"
        if wallpapers_dir.exists():
            shutil.rmtree(wallpapers_dir)

        w_id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        disp_dir = wallpapers_dir / "display"
        thumb_dir = wallpapers_dir / "thumbnails"
        orig_dir = wallpapers_dir / "originals"
        disp_dir.mkdir(parents=True)
        thumb_dir.mkdir(parents=True)
        orig_dir.mkdir(parents=True)

        fake_webp = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\xb0\x01\x00\x9d\x01\x2a\x01\x00\x01\x00"
        (disp_dir / f"{w_id}.webp").write_bytes(fake_webp)
        (thumb_dir / f"{w_id}.webp").write_bytes(fake_webp)
        (orig_dir / "sample.png").write_bytes(b"origpng")

        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "sample.png",
                    "width": 1920,
                    "height": 1080,
                    "original": {"path": "originals/sample.png", "sha256": "fake", "bytes": 7},
                    "display": {"path": f"display/{w_id}.webp", "sha256": "fake", "bytes": len(fake_webp)},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "fake", "bytes": len(fake_webp)},
                }
            ],
        }
        (wallpapers_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Admin and family both have access
        for user in ["testfamily", "testadmin"]:
            st, body, hdrs = self.request("GET", "/api/wallpapers", user=user)
            self.assertEqual(st, 200)
            self.assertEqual(hdrs.get("cache-control"), "private, no-store")
            catalog = json.loads(body)
            self.assertEqual(catalog["version"], 1)
            self.assertEqual(catalog["default_id"], w_id)
            self.assertEqual(len(catalog["wallpapers"]), 1)
            item = catalog["wallpapers"][0]
            self.assertEqual(item["id"], w_id)
            self.assertEqual(item["name"], "sample.png")
            self.assertEqual(item["thumbnail_url"], f"/api/wallpapers/{w_id}/thumbnail.webp")
            self.assertEqual(item["image_url"], f"/api/wallpapers/{w_id}/display.webp")
            # Private manifest metadata must be stripped
            self.assertNotIn("original", item)
            self.assertNotIn("sha256", item)

            # Test derivative endpoints
            for deriv in ["thumbnail.webp", "display.webp"]:
                st, deriv_body, deriv_hdrs = self.request(
                    "GET", f"/api/wallpapers/{w_id}/{deriv}", user=user
                )
                self.assertEqual(st, 200)
                self.assertEqual(deriv_hdrs.get("content-type"), "image/webp")
                self.assertEqual(deriv_hdrs.get("cache-control"), "private, no-store")
                self.assertEqual(deriv_body, fake_webp)

        # Unknown ID -> 404
        st, _, _ = self.request("GET", f"/api/wallpapers/{'f' * 64}/display.webp", user="testfamily")
        self.assertEqual(st, 404)

        # Path traversal -> 404
        st, _, _ = self.request("GET", "/api/wallpapers/../etc/passwd", user="testfamily")
        self.assertEqual(st, 404)

        # Invalid derivative name -> 404
        st, _, _ = self.request("GET", f"/api/wallpapers/{w_id}/sample.png", user="testfamily")
        self.assertEqual(st, 404)

        # HEAD request respects HEAD semantics
        st, body, hdrs = self.request("HEAD", "/api/wallpapers", user="testfamily")
        self.assertEqual(st, 200)
        self.assertEqual(body, b"")
        self.assertIn("content-length", hdrs)

        st, body, hdrs = self.request("HEAD", f"/api/wallpapers/{w_id}/thumbnail.webp", user="testfamily")
        self.assertEqual(st, 200)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs.get("content-type"), "image/webp")

    def test_public_shared_appearance_assets(self):
        for path, expected_type in [
            ("/theme-tokens.css", "text/css; charset=utf-8"),
            ("/appearance.css", "text/css; charset=utf-8"),
            ("/appearance.js", "application/javascript; charset=utf-8"),
            ("/static/theme-tokens.css", "text/css; charset=utf-8"),
            ("/static/appearance.css", "text/css; charset=utf-8"),
            ("/static/appearance.js", "application/javascript; charset=utf-8"),
        ]:
            st, body, hdrs = self.request("GET", path)
            self.assertEqual(st, 200, f"Failed GET {path}")
            self.assertEqual(hdrs.get("content-type"), expected_type)
            self.assertEqual(hdrs.get("cache-control"), "no-cache")
            self.assertGreater(len(body), 0)

            # HEAD request
            st, h_body, h_hdrs = self.request("HEAD", path)
            self.assertEqual(st, 200)
            self.assertEqual(h_body, b"")
            self.assertEqual(h_hdrs.get("content-type"), expected_type)

        # Non-allowlisted static path returns 404 for authenticated user, 302 to login for unauthenticated
        st, _, _ = self.request("GET", "/unknown.css", user="testfamily")
        self.assertEqual(st, 404)
        st, _, hdrs = self.request("GET", "/unknown.css")
        self.assertEqual(st, 302)
        self.assertEqual(hdrs.get("location"), "/login")


if __name__ == "__main__":
    unittest.main()
