"""Tests for wallpaper data models, offline preparation tools, and HTTP API integration."""
import copy
import errno
import hashlib
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
    publish_palettes,
    atomic_exchange_dirs,
    generate_palettes_for_library,
    sha256_file,
    compute_display_dimensions,
    compute_contain_dimensions,
    M3_PALETTE_ROLES,
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

    def test_existing_incomplete_or_symlink_library_is_corrupt(self):
        with self.assertRaises(CorruptLibraryError):
            WallpaperLibrary(self.lib_path).get_public_list()
        link = self.lib_path / "alias"
        link.symlink_to(self.lib_path, target_is_directory=True)
        with self.assertRaises(CorruptLibraryError):
            WallpaperLibrary(link).get_public_list()

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

    def test_bool_dimensions_rejected(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": True,  # bool should be rejected
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_duplicate_id_and_name_rejected(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        # Duplicate ID
        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test1.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                },
                {
                    "id": w_id,
                    "name": "test2.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                },
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_default_id_not_in_manifest_rejected(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        manifest = {
            "version": 1,
            "default_id": "f" * 64,  # not in wallpapers
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_public_list()

    def test_mismatched_derivative_bytes_rejected(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 999},  # size mismatch
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        with self.assertRaises(CorruptLibraryError):
            lib.get_derivative(w_id, "display.webp")

    def test_manifest_replacement_equal_size_mtime_detected(self):
        w_id1 = "1" * 64
        w_id2 = "2" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id1}.webp").write_bytes(b"disp1")
        (thumb_dir / f"{w_id1}.webp").write_bytes(b"thmb1")
        (disp_dir / f"{w_id2}.webp").write_bytes(b"disp2")
        (thumb_dir / f"{w_id2}.webp").write_bytes(b"thmb2")

        # Manifest 1 and Manifest 2 have identical string length
        manifest1 = {
            "version": 1,
            "default_id": w_id1,
            "wallpapers": [
                {
                    "id": w_id1,
                    "name": "imgA.png",
                    "width": 100,
                    "height": 100,
                    "display": {"path": f"display/{w_id1}.webp", "sha256": "b" * 64, "bytes": 5},
                    "thumbnail": {"path": f"thumbnails/{w_id1}.webp", "sha256": "c" * 64, "bytes": 5},
                }
            ],
        }
        manifest2 = {
            "version": 1,
            "default_id": w_id2,
            "wallpapers": [
                {
                    "id": w_id2,
                    "name": "imgB.png",
                    "width": 100,
                    "height": 100,
                    "display": {"path": f"display/{w_id2}.webp", "sha256": "d" * 64, "bytes": 5},
                    "thumbnail": {"path": f"thumbnails/{w_id2}.webp", "sha256": "e" * 64, "bytes": 5},
                }
            ],
        }
        txt1 = json.dumps(manifest1)
        txt2 = json.dumps(manifest2)
        self.assertEqual(len(txt1), len(txt2))

        m_file = self.lib_path / "manifest.json"
        m_file.write_text(txt1, encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        pub1 = lib.get_public_list()
        self.assertEqual(pub1["default_id"], w_id1)

        # Atomic replacement: create temp and rename over manifest.json with same mtime
        st1 = m_file.stat()
        tmp_m = self.lib_path / "manifest.tmp"
        tmp_m.write_text(txt2, encoding="utf-8")
        os.utime(tmp_m, (st1.st_atime, st1.st_mtime))
        tmp_m.rename(m_file)

        # Inode / ctime changes even if size and mtime are equal
        pub2 = lib.get_public_list()
        self.assertEqual(pub2["default_id"], w_id2)

    def test_valid_palette_parsed_in_manifest(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        palette = {
            "surface": "#101417",
            "surface_dim": "#101417",
            "surface_bright": "#363a3e",
            "surface_container_lowest": "#0b0f12",
            "surface_container_low": "#181c20",
            "surface_container": "#1c2024",
            "surface_container_high": "#272a2e",
            "surface_container_highest": "#313539",
            "on_surface": "#e0e3e8",
            "on_surface_variant": "#c2c7ce",
            "outline": "#8c9198",
            "outline_variant": "#42474d",
            "primary": "#98ccf9",
            "on_primary": "#003350",
            "primary_container": "#034b71",
            "on_primary_container": "#cce5ff",
            "tertiary": "#d1bfe7",
            "on_tertiary": "#372a4a",
            "tertiary_container": "#4e4162",
            "on_tertiary_container": "#ecdcff",
            "error": "#ffb4ab",
            "on_error": "#690005",
            "error_container": "#93000a",
            "on_error_container": "#ffdad6",
        }
        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                    "palette": palette,
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        pub = lib.get_public_list()
        self.assertEqual(pub["wallpapers"][0]["palette"], palette)

    def test_malformed_palette_falls_back_gracefully(self):
        w_id = "a" * 64
        disp_dir = self.lib_path / "display"
        disp_dir.mkdir(parents=True)
        thumb_dir = self.lib_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (disp_dir / f"{w_id}.webp").write_bytes(b"disp")
        (thumb_dir / f"{w_id}.webp").write_bytes(b"thumb")

        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "test.png",
                    "width": 1920,
                    "height": 1080,
                    "display": {"path": f"display/{w_id}.webp", "sha256": "b" * 64, "bytes": 4},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": "c" * 64, "bytes": 5},
                    "palette": {"surface": "not-a-color", "primary": "#123"},
                }
            ],
        }
        (self.lib_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        lib = WallpaperLibrary(self.lib_path)
        pub = lib.get_public_list()
        self.assertNotIn("palette", pub["wallpapers"][0])


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

    def test_generate_palettes_symlink_traversal_rejected(self):
        self._create_synthetic_image("test.png", (800, 600), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        # Create symlinked directory or file
        sym_dir = self.root / "sym_lib"
        sym_dir.symlink_to(self.dest_dir)
        ok, msg = generate_palettes_for_library(sym_dir)
        self.assertFalse(ok)
        self.assertIn("symlink", msg.lower())

    def test_generate_palettes_failure_preserves_files_unchanged(self):
        self._create_synthetic_image("test.png", (800, 600), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        manifest_before = (self.dest_dir / "manifest.json").read_bytes()
        tm_before = (self.dest_dir / "transport-manifest.json").read_bytes()

        # Call with nonexistent matugen binary -> must fail without touching disk
        ok, msg = generate_palettes_for_library(self.dest_dir, matugen_bin="/nonexistent/matugen")
        self.assertFalse(ok)

        manifest_after = (self.dest_dir / "manifest.json").read_bytes()
        tm_after = (self.dest_dir / "transport-manifest.json").read_bytes()

        self.assertEqual(manifest_before, manifest_after)
        self.assertEqual(tm_before, tm_after)

    def test_generate_palettes_real_matugen_success(self):
        if not os.path.isfile("/usr/bin/matugen") or not os.access("/usr/bin/matugen", os.X_OK):
            self.skipTest("/usr/bin/matugen not available")

        self._create_synthetic_image("test.png", (800, 600), "blue")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        ok, msg = generate_palettes_for_library(self.dest_dir, matugen_bin="/usr/bin/matugen")
        self.assertTrue(ok, msg)

        # Verify manifest now contains complete 24-role palette
        manifest = json.loads((self.dest_dir / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["wallpapers"][0]
        self.assertIn("palette", item)
        palette = item["palette"]
        for role in M3_PALETTE_ROLES:
            self.assertIn(role, palette)
            self.assertRegex(palette[role], r"^#[0-9a-fA-F]{6}$")

        # Transport manifest checksum must be updated and verify_library must pass
        verify_ok, verify_msg = verify_library(self.dest_dir)
        self.assertTrue(verify_ok, verify_msg)

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

    def test_unknown_extra_regular_file_rejected_by_verifier(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        # Add undeclared regular file
        extra_file = self.dest_dir / "extra.txt"
        extra_file.write_text("rogue", encoding="utf-8")
        ok, msg = verify_library(self.dest_dir)
        self.assertFalse(ok)
        self.assertIn("Unexpected file in library", msg)

    def test_undeclared_symlink_rejected_by_verifier(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        # Add undeclared symlink inside library
        sentinel = self.root / "outside_sentinel.txt"
        sentinel.write_text("secret", encoding="utf-8")
        link_target = self.dest_dir / "sentinel_link.txt"
        os.symlink(sentinel, link_target)
        ok, msg = verify_library(self.dest_dir)
        self.assertFalse(ok)
        self.assertIn("Undeclared symlink in library", msg)

    def test_parent_escape_original_path_rejected(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        # Tamper manifest with ../ escape
        manifest_path = self.dest_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["wallpapers"][0]["original"]["path"] = "../outside.txt"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        ok, msg = verify_library(self.dest_dir)
        self.assertFalse(ok)
        self.assertIn("canonical", msg)

    def test_absolute_path_rejected(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        # Tamper manifest with absolute path
        manifest_path = self.dest_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["wallpapers"][0]["original"]["path"] = "/etc/passwd"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        ok, msg = verify_library(self.dest_dir)
        self.assertFalse(ok)
        self.assertIn("canonical", msg)

    def test_parent_dir_symlink_rejected(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)
        # Replace thumbnails dir with symlink to outside
        outside_dir = self.root / "outside_thumbnails"
        outside_dir.mkdir()
        shutil.rmtree(self.dest_dir / "thumbnails")
        os.symlink(outside_dir, self.dest_dir / "thumbnails")
        ok, msg = verify_library(self.dest_dir)
        self.assertFalse(ok)
        self.assertIn("symlink", msg)

    def test_existing_temp_staging_preserved(self):
        # Create a pre-existing predictable staging folder with a sentinel file
        sentinel_dir = self.root / "dest.staging"
        sentinel_dir.mkdir()
        sentinel_file = sentinel_dir / "keep.txt"
        sentinel_file.write_text("do not delete", encoding="utf-8")

        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        # Pre-existing staging sentinel must be completely untouched
        self.assertTrue(sentinel_file.exists())
        self.assertEqual(sentinel_file.read_text(encoding="utf-8"), "do not delete")

    def test_conflicting_symlink_destination_preserved(self):
        self._create_synthetic_image("test.png", (100, 100), "green")
        build_library(src_dir=self.src_dir, dest_dir=self.dest_dir, inventory_path=None)

        # Create a symlink as target_dir
        outside_dest = self.root / "outside_dest"
        outside_dest.mkdir()
        symlink_target = self.root / "phone_symlink_dest"
        os.symlink(outside_dest, symlink_target)

        pub_ok, msg = publish_library(self.dest_dir, symlink_target)
        self.assertFalse(pub_ok)
        self.assertIn("symlink", msg)
        self.assertTrue(symlink_target.is_symlink())


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
        else:
            os.environ.pop("HOME", None)

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
        self.assertEqual(hdrs.get("cache-control"), "private, no-store")

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

        fake_sha = hashlib.sha256(fake_webp).hexdigest()
        manifest = {
            "version": 1,
            "default_id": w_id,
            "wallpapers": [
                {
                    "id": w_id,
                    "name": "sample.png",
                    "width": 1920,
                    "height": 1080,
                    "original": {"path": "originals/sample.png", "sha256": w_id, "bytes": 7},
                    "display": {"path": f"display/{w_id}.webp", "sha256": fake_sha, "bytes": len(fake_webp)},
                    "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": fake_sha, "bytes": len(fake_webp)},
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

        # Same-size corruption must fail even after the catalog has been cached.
        (disp_dir / f"{w_id}.webp").write_bytes(b"X" * len(fake_webp))
        st, _, headers = self.request("GET", f"/api/wallpapers/{w_id}/display.webp", user="testfamily")
        self.assertEqual(st, 503)
        self.assertEqual(headers.get("cache-control"), "private, no-store")
        st, body, headers = self.request("HEAD", f"/api/wallpapers/{w_id}/display.webp", user="testfamily")
        self.assertEqual(st, 503)
        self.assertEqual(body, b"")

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


class WallpaperPublishPalettesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.target_dir = self.root / "wallpapers"
        self.candidate_dir = self.root / "candidate"
        self.old_m_sha, self.old_tm_sha = self._create_synthetic_library(self.target_dir)
        self.cand_m_sha, self.cand_tm_sha = self._create_candidate_metadata(self.candidate_dir, self.target_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_synthetic_library(self, lib_dir: Path, count: int = 2) -> Tuple[str, str]:
        lib_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("originals", "display", "thumbnails"):
            (lib_dir / sub).mkdir(parents=True, exist_ok=True)

        wallpapers = []
        transport_files = []

        for i in range(count):
            name = f"{i:02d}. test.png"
            orig_bytes = f"synthetic-original-payload-{i}".encode("utf-8")
            disp_bytes = f"synthetic-display-payload-{i}".encode("utf-8")
            thumb_bytes = f"synthetic-thumb-payload-{i}".encode("utf-8")

            orig_sha = hashlib.sha256(orig_bytes).hexdigest()
            disp_sha = hashlib.sha256(disp_bytes).hexdigest()
            thumb_sha = hashlib.sha256(thumb_bytes).hexdigest()
            w_id = orig_sha

            (lib_dir / "originals" / name).write_bytes(orig_bytes)
            (lib_dir / "display" / f"{w_id}.webp").write_bytes(disp_bytes)
            (lib_dir / "thumbnails" / f"{w_id}.webp").write_bytes(thumb_bytes)

            wallpapers.append({
                "id": w_id,
                "name": name,
                "width": 1920,
                "height": 1080,
                "original": {"path": f"originals/{name}", "sha256": orig_sha, "bytes": len(orig_bytes)},
                "display": {"path": f"display/{w_id}.webp", "sha256": disp_sha, "bytes": len(disp_bytes), "width": 1920, "height": 1080},
                "thumbnail": {"path": f"thumbnails/{w_id}.webp", "sha256": thumb_sha, "bytes": len(thumb_bytes), "width": 320, "height": 180},
            })

            transport_files.extend([
                {"path": f"originals/{name}", "sha256": orig_sha, "bytes": len(orig_bytes)},
                {"path": f"display/{w_id}.webp", "sha256": disp_sha, "bytes": len(disp_bytes)},
                {"path": f"thumbnails/{w_id}.webp", "sha256": thumb_sha, "bytes": len(thumb_bytes)},
            ])

        manifest = {"version": 1, "default_id": wallpapers[0]["id"], "wallpapers": wallpapers}
        m_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        (lib_dir / "manifest.json").write_bytes(m_bytes)

        transport_files.append({"path": "manifest.json", "sha256": hashlib.sha256(m_bytes).hexdigest(), "bytes": len(m_bytes)})
        transport_files.sort(key=lambda x: x["path"])

        tm = {
            "version": 1,
            "payload_entries": len(transport_files),
            "total_actual_files": len(transport_files) + 1,
            "total_payload_bytes": sum(f["bytes"] for f in transport_files),
            "files": transport_files,
        }
        tm_bytes = json.dumps(tm, indent=2).encode("utf-8")
        (lib_dir / "transport-manifest.json").write_bytes(tm_bytes)
        return sha256_file(lib_dir / "manifest.json"), sha256_file(lib_dir / "transport-manifest.json")

    def _create_candidate_metadata(self, cand_dir: Path, target_dir: Path, mutate_m_fn=None, mutate_tm_fn=None) -> Tuple[str, str]:
        cand_dir.mkdir(parents=True, exist_ok=True)
        with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
            m = json.load(f)
        with open(target_dir / "transport-manifest.json", "r", encoding="utf-8") as f:
            tm = json.load(f)

        for w in m["wallpapers"]:
            w["palette"] = {role: "#1a2b3c" for role in M3_PALETTE_ROLES}

        if mutate_m_fn:
            mutate_m_fn(m)

        m_bytes = json.dumps(m, indent=2).encode("utf-8")
        (cand_dir / "manifest.json").write_bytes(m_bytes)
        m_sha = hashlib.sha256(m_bytes).hexdigest()

        for f in tm["files"]:
            if f["path"] == "manifest.json":
                f["sha256"] = m_sha
                f["bytes"] = len(m_bytes)
        tm["total_payload_bytes"] = sum(f["bytes"] for f in tm["files"])

        if mutate_tm_fn:
            mutate_tm_fn(tm)

        tm_bytes = json.dumps(tm, indent=2).encode("utf-8")
        (cand_dir / "transport-manifest.json").write_bytes(tm_bytes)
        return m_sha, sha256_file(cand_dir / "transport-manifest.json")

    def test_successful_atomic_exchange_retains_payload_hashes_and_backup(self):
        orig_files = {}
        for root, _, files in os.walk(self.target_dir):
            for fn in files:
                rel = str(Path(root).relative_to(self.target_dir) / fn)
                if rel not in ("manifest.json", "transport-manifest.json"):
                    fp = Path(root) / fn
                    orig_files[rel] = (fp.stat().st_size, sha256_file(fp))

        ok, msg, backup_path = publish_palettes(
            self.candidate_dir,
            self.target_dir,
            self.old_m_sha,
            self.old_tm_sha,
        )
        self.assertTrue(ok, msg)
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.is_dir())

        # Backup retains old metadata and passes verify_library
        bk_ok, bk_msg = verify_library(backup_path)
        self.assertTrue(bk_ok, bk_msg)
        self.assertEqual(sha256_file(backup_path / "manifest.json"), self.old_m_sha)
        self.assertEqual(sha256_file(backup_path / "transport-manifest.json"), self.old_tm_sha)

        # Target has new metadata and passes verify_library
        tgt_ok, tgt_msg = verify_library(self.target_dir)
        self.assertTrue(tgt_ok, tgt_msg)
        self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.cand_m_sha)
        self.assertEqual(sha256_file(self.target_dir / "transport-manifest.json"), self.cand_tm_sha)

        # All payload hashes and sizes remain identical
        for rel, (size, sha) in orig_files.items():
            curr_fp = self.target_dir / rel
            self.assertTrue(curr_fp.is_file(), f"Missing {rel} in published target")
            self.assertEqual(curr_fp.stat().st_size, size, f"Size changed for {rel}")
            self.assertEqual(sha256_file(curr_fp), sha, f"Hash changed for {rel}")

    def test_idempotent_double_invocation(self):
        ok1, msg1, backup1 = publish_palettes(self.candidate_dir, self.target_dir, self.old_m_sha, self.old_tm_sha)
        self.assertTrue(ok1, msg1)
        self.assertIsNotNone(backup1)

        ok2, msg2, backup2 = publish_palettes(self.candidate_dir, self.target_dir, self.old_m_sha, self.old_tm_sha)
        self.assertTrue(ok2, msg2)
        self.assertIn("already up to date", msg2)
        self.assertIsNone(backup2)

    def test_wrong_old_manifest_hash_rejected(self):
        wrong_m_sha = "0" * 64
        ok, msg, backup = publish_palettes(self.candidate_dir, self.target_dir, wrong_m_sha, self.old_tm_sha)
        self.assertFalse(ok)
        self.assertIn("Target manifest SHA256", msg)
        self.assertIsNone(backup)
        self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.old_m_sha)

    def test_wrong_old_transport_hash_rejected(self):
        wrong_tm_sha = "1" * 64
        ok, msg, backup = publish_palettes(self.candidate_dir, self.target_dir, self.old_m_sha, wrong_tm_sha)
        self.assertFalse(ok)
        self.assertIn("Target transport manifest SHA256", msg)
        self.assertIsNone(backup)
        self.assertEqual(sha256_file(self.target_dir / "transport-manifest.json"), self.old_tm_sha)

    def test_candidate_nonpalette_mutation_rejected(self):
        mutations = [
            lambda m: m["wallpapers"][0].__setitem__("name", "mutated.png"),
            lambda m: m["wallpapers"][0].__setitem__("width", 9999),
            lambda m: m["wallpapers"][0].__setitem__("extra_field", "bad"),
            lambda m: m["wallpapers"].append(copy.deepcopy(m["wallpapers"][0])),
            lambda m: m.__setitem__("default_id", "f" * 64),
        ]
        for mutate in mutations:
            cand_mut = self.root / f"cand_mut_{abs(hash(mutate))}"
            cand_mut.mkdir()
            c_m_sha, c_tm_sha = self._create_candidate_metadata(cand_mut, self.target_dir, mutate_m_fn=mutate)
            ok, msg, backup = publish_palettes(cand_mut, self.target_dir, self.old_m_sha, self.old_tm_sha)
            self.assertFalse(ok, f"Expected mutation to fail: {msg}")
            self.assertTrue(
                "non-palette mutations" in msg or "count mismatch" in msg or "default_id mismatch" in msg,
                f"Unexpected rejection message: {msg}",
            )
            self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.old_m_sha)

    def test_malformed_palette_rejected(self):
        def drop_role(m):
            del m["wallpapers"][0]["palette"]["primary"]

        def bad_hex(m):
            m["wallpapers"][0]["palette"]["surface"] = "rgb(0,0,0)"

        def extra_role(m):
            m["wallpapers"][0]["palette"]["bogus_role"] = "#123456"

        for mutate in (drop_role, bad_hex, extra_role):
            cand_mut = self.root / f"cand_pal_mut_{abs(hash(mutate))}"
            cand_mut.mkdir()
            self._create_candidate_metadata(cand_mut, self.target_dir, mutate_m_fn=mutate)
            ok, msg, backup = publish_palettes(cand_mut, self.target_dir, self.old_m_sha, self.old_tm_sha)
            self.assertFalse(ok, f"Expected malformed palette to fail: {msg}")
            self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.old_m_sha)

    def test_altered_image_record_in_transport_rejected(self):
        def alter_image_record(tm):
            for f in tm["files"]:
                if f["path"] != "manifest.json":
                    f["sha256"] = "e" * 64
                    break

        cand_mut = self.root / "cand_alt_img"
        cand_mut.mkdir()
        self._create_candidate_metadata(cand_mut, self.target_dir, mutate_tm_fn=alter_image_record)
        ok, msg, backup = publish_palettes(cand_mut, self.target_dir, self.old_m_sha, self.old_tm_sha)
        self.assertFalse(ok)
        self.assertIn("Candidate transport record altered for payload file", msg)
        self.assertEqual(sha256_file(self.target_dir / "transport-manifest.json"), self.old_tm_sha)

    def test_symlink_target_and_candidate_rejected(self):
        # Target is symlink
        target_link = self.root / "target_symlink"
        target_link.symlink_to(self.target_dir, target_is_directory=True)
        ok, msg, _ = publish_palettes(self.candidate_dir, target_link, self.old_m_sha, self.old_tm_sha)
        self.assertFalse(ok)
        self.assertIn("symlink", msg.lower())

        # Candidate is symlink
        cand_link = self.root / "cand_symlink"
        cand_link.symlink_to(self.candidate_dir, target_is_directory=True)
        ok, msg, _ = publish_palettes(cand_link, self.target_dir, self.old_m_sha, self.old_tm_sha)
        self.assertFalse(ok)
        self.assertIn("symlink", msg.lower())

        # Candidate manifest is symlink
        cand_bad = self.root / "cand_sym_file"
        cand_bad.mkdir()
        shutil.copy2(self.candidate_dir / "transport-manifest.json", cand_bad / "transport-manifest.json")
        (cand_bad / "manifest.json").symlink_to(self.candidate_dir / "manifest.json")
        ok, msg, _ = publish_palettes(cand_bad, self.target_dir, self.old_m_sha, self.old_tm_sha)
        self.assertFalse(ok)
        self.assertIn("symlink", msg.lower())

    def test_unsupported_exchange_preserves_original(self):
        with patch("tools.prepare_wallpapers.atomic_exchange_dirs", side_effect=OSError(errno.ENOSYS, "renameat2 unsupported")):
            ok, msg, backup = publish_palettes(self.candidate_dir, self.target_dir, self.old_m_sha, self.old_tm_sha)
            self.assertFalse(ok)
            self.assertIn("Atomic directory exchange", msg)
            self.assertIsNone(backup)
            # Target untouched
            ok_v, msg_v = verify_library(self.target_dir)
            self.assertTrue(ok_v, msg_v)
            self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.old_m_sha)

    def test_post_exchange_failure_rolls_back_preserving_original(self):
        real_verify = verify_library
        call_count = [0]

        def fail_post_verify(path):
            call_count[0] += 1
            if call_count[0] == 3:
                return False, "Simulated post-exchange corruption"
            return real_verify(path)

        with patch("tools.prepare_wallpapers.verify_library", side_effect=fail_post_verify):
            ok, msg, backup = publish_palettes(self.candidate_dir, self.target_dir, self.old_m_sha, self.old_tm_sha)
            self.assertFalse(ok)
            self.assertIn("Post-exchange verification failed", msg)
            self.assertIn("rolled back target to original state", msg)

        # Target restored to old state and passes verify
        tgt_ok, tgt_msg = verify_library(self.target_dir)
        self.assertTrue(tgt_ok, tgt_msg)
        self.assertEqual(sha256_file(self.target_dir / "manifest.json"), self.old_m_sha)
        self.assertEqual(sha256_file(self.target_dir / "transport-manifest.json"), self.old_tm_sha)

    def test_publish_without_pillow_prerequisite(self):
        target2 = self.root / "wallpapers_no_pil"
        old_m, old_tm = self._create_synthetic_library(target2)
        cand2 = self.root / "cand_no_pil"
        c_m, c_tm = self._create_candidate_metadata(cand2, target2)

        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageOps": None}):
            ok, msg, backup = publish_palettes(cand2, target2, old_m, old_tm)
            self.assertTrue(ok, msg)
            self.assertIsNotNone(backup)
            ok_v, _ = verify_library(target2)
            self.assertTrue(ok_v)

    def test_pre_activation_drift_refused(self):
        target3 = self.root / "wallpapers_drift"
        old_m, old_tm = self._create_synthetic_library(target3)
        cand3 = self.root / "cand_drift"
        c_m, c_tm = self._create_candidate_metadata(cand3, target3)

        real_link = os.link

        def drift_target(src, dst):
            (target3 / "manifest.json").write_text('{"drifted": true}', encoding="utf-8")
            return real_link(src, dst)

        with patch("os.link", side_effect=drift_target):
            ok, msg, backup = publish_palettes(cand3, target3, old_m, old_tm)
            self.assertFalse(ok)
            self.assertIn("Pre-activation drift detected", msg)

    def test_cli_publish_palettes(self):
        import subprocess
        tool_py = str(Path(__file__).resolve().parents[1] / "tools" / "prepare_wallpapers.py")

        target_cli = self.root / "wallpapers_cli"
        old_m, old_tm = self._create_synthetic_library(target_cli)
        cand_cli = self.root / "cand_cli"
        c_m, c_tm = self._create_candidate_metadata(cand_cli, target_cli)

        cmd = [
            sys.executable, tool_py, "publish-palettes",
            str(cand_cli), str(target_cli),
            "--old-manifest-sha", old_m,
            "--old-transport-sha", old_tm,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PUBLISH-PALETTES OK", res.stdout)
        self.assertIn("BACKUP PATH:", res.stdout)

        # Idempotent re-run via CLI
        res2 = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertIn("already up to date", res2.stdout)

    def test_real_candidate_against_real_library_copy(self):
        cand_dir = Path("/home/astro/repos/mobile-server/.superpowers/sdd/wallpaper-palette-candidate")
        real_src = Path("/home/astro/server-audit/2026-11-05/cloud-glass-data/.work/wallpapers-library")
        if not cand_dir.is_dir() or not real_src.is_dir():
            self.skipTest("Real candidate or real library source not present on host")

        test_target = self.root / "real_library_target"
        test_target.mkdir()
        for sub in ("originals", "display", "thumbnails"):
            (test_target / sub).mkdir()
            for f in (real_src / sub).iterdir():
                try:
                    os.link(f, test_target / sub / f.name)
                except OSError:
                    shutil.copy2(f, test_target / sub / f.name)
        shutil.copy2(real_src / "manifest.json", test_target / "manifest.json")
        shutil.copy2(real_src / "transport-manifest.json", test_target / "transport-manifest.json")

        old_m_sha = "47509cfdfcf804ec85b48383b755af8e7a78c0ccc43c971f76d12aeae74bb23a"
        old_tm_sha = "be8ac6180ad71fc284f673a5858771d4d55864cf899a2911ffcb2d77367d1bb0"
        cand_m_sha = "9be929cad6824d6e668ea7d17fb2977a180bff451309478ada73df3b0c61fca5"
        cand_tm_sha = "3e13df8b1a7e43304acbaa4c1ff9af5b8622142ffbb351371dbd311c0301b426"

        self.assertEqual(sha256_file(test_target / "manifest.json"), old_m_sha)
        self.assertEqual(sha256_file(test_target / "transport-manifest.json"), old_tm_sha)

        ok, msg, backup_path = publish_palettes(cand_dir, test_target, old_m_sha, old_tm_sha)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.is_dir())

        bk_ok, bk_msg = verify_library(backup_path)
        self.assertTrue(bk_ok, bk_msg)
        self.assertEqual(sha256_file(backup_path / "manifest.json"), old_m_sha)
        self.assertEqual(sha256_file(backup_path / "transport-manifest.json"), old_tm_sha)

        tgt_ok, tgt_msg = verify_library(test_target)
        self.assertTrue(tgt_ok, tgt_msg)
        self.assertEqual(sha256_file(test_target / "manifest.json"), cand_m_sha)
        self.assertEqual(sha256_file(test_target / "transport-manifest.json"), cand_tm_sha)


if __name__ == "__main__":
    unittest.main()
