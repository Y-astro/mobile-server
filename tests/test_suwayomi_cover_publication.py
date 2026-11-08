"""Unit and safety tests for Suwayomi cover publication tool."""

import errno
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.publish_suwayomi_covers import (
    _find_existing_cache_file,
    _rename_noreplace,
    _validate_image_signature,
    _validate_target_dir,
    publish_covers,
    verify_manifest_and_staged_files,
)

# Minimal valid JPEG fixture (SOI, APP0, SOF0, SOS, EOI)
VALID_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
    b"\xff\xdb\x00C\x00" + b"\x08" * 64 +
    b"\xff\xc0\x00\x0b\x08\x00\x0a\x00\x0a\x01\x01\x11\x00"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00"
    b"\xbf\x00"
    b"\xff\xd9"
)


class TestSuwayomiCoverPublication(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.staging_dir = self.base_dir / "staging"
        self.images_dir = self.staging_dir / "images"
        self.images_dir.mkdir(parents=True)
        self.target_dir = self.base_dir / "thumbnails"
        self.target_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_staged_cover(self, manga_id: int, data: bytes = VALID_JPEG_BYTES, hid: str = "test1") -> dict:
        img_path = self.images_dir / f"{manga_id}.jpg"
        img_path.write_bytes(data)
        sha256 = hashlib.sha256(data).hexdigest()
        return {
            "manga_id": manga_id,
            "hid": hid,
            "canonical_url": f"https://static.mfcdn.nl/test/{manga_id}.jpg",
            "filename": f"images/{manga_id}.jpg",
            "bytes": len(data),
            "sha256": sha256,
        }

    def _write_manifest(self, entries: list) -> Path:
        manifest = {
            "schema_version": 1,
            "source_name": "MangaFire",
            "source_id": "6084907896154116083",
            "entries": entries,
        }
        manifest_path = self.staging_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        return manifest_path

    def test_image_signature_validation(self):
        """Test detection of valid JPEG, PNG, WebP headers and rejection of bad headers."""
        ok, fmt = _validate_image_signature(VALID_JPEG_BYTES)
        self.assertTrue(ok)
        self.assertEqual(fmt, "JPEG")

        ok, fmt = _validate_image_signature(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        self.assertTrue(ok)
        self.assertEqual(fmt, "PNG")

        ok, fmt = _validate_image_signature(b"RIFF\x20\x00\x00\x00WEBPVP8 ")
        self.assertTrue(ok)
        self.assertEqual(fmt, "WEBP")

        ok, _ = _validate_image_signature(b"<html>not an image</html>")
        self.assertFalse(ok)

        ok, _ = _validate_image_signature(b"")
        self.assertFalse(ok)

    def test_target_dir_validation(self):
        """Test target dir must exist, be real directory, not symlink."""
        ok, _ = _validate_target_dir(self.target_dir)
        self.assertTrue(ok)

        nonexistent = self.base_dir / "nonexistent"
        ok, msg = _validate_target_dir(nonexistent)
        self.assertFalse(ok)
        self.assertIn("does not exist", msg)

        # Symlink target directory must be rejected
        link_dir = self.base_dir / "link_thumbnails"
        link_dir.symlink_to(self.target_dir)
        ok, msg = _validate_target_dir(link_dir)
        self.assertFalse(ok)
        self.assertIn("symlink", msg)

    def test_manifest_tampering_refusal_byte_mismatch(self):
        """Manifest entry byte count mismatch must fail validation."""
        entry = self._create_staged_cover(107)
        entry["bytes"] = entry["bytes"] + 10  # tampered
        self._write_manifest([entry])

        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("byte length mismatch", msg)

    def test_manifest_tampering_refusal_sha256_mismatch(self):
        """Manifest entry sha256 mismatch must fail validation."""
        entry = self._create_staged_cover(107)
        entry["sha256"] = "a" * 64  # tampered
        self._write_manifest([entry])

        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("sha256 mismatch", msg)

    def test_manifest_path_and_id_rejection(self):
        """Invalid IDs, paths with traversal, or illegal URLs must be rejected."""
        # Non-positive ID
        entry = self._create_staged_cover(107)
        entry["manga_id"] = -5
        self._write_manifest([entry])
        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("invalid manga_id", msg)

        # Traversal in filename
        entry = self._create_staged_cover(107)
        entry["filename"] = "../images/107.jpg"
        self._write_manifest([entry])
        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("filename mismatch", msg)

        # Invalid CDN URL
        entry = self._create_staged_cover(107)
        entry["canonical_url"] = "http://evil.com/cover.jpg"
        self._write_manifest([entry])
        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("invalid canonical_url", msg)

    def test_symlink_rejection_in_staging(self):
        """Symlinks inside staging directory must be rejected."""
        entry = self._create_staged_cover(107)
        real_file = self.images_dir / "107.jpg"
        sym_file = self.images_dir / "108.jpg"
        sym_file.symlink_to(real_file)

        entry2 = {
            "manga_id": 108,
            "hid": "test2",
            "canonical_url": "https://static.mfcdn.nl/test/108.jpg",
            "filename": "images/108.jpg",
            "bytes": len(VALID_JPEG_BYTES),
            "sha256": hashlib.sha256(VALID_JPEG_BYTES).hexdigest(),
        }
        self._write_manifest([entry, entry2])

        ok, msg, _ = verify_manifest_and_staged_files(self.staging_dir)
        self.assertFalse(ok)
        self.assertIn("symlink (rejected)", msg)

    def test_existing_file_preservation(self):
        """Existing format ({id}.webp/jpg/png) in target_dir must not be overwritten."""
        entry = self._create_staged_cover(107)
        self._write_manifest([entry])

        # Pre-create 107.webp in target
        target_webp = self.target_dir / "107.webp"
        original_data = b"original-webp-thumbnail-data"
        target_webp.write_bytes(original_data)

        # Run with --apply
        res = publish_covers(self.staging_dir, self.target_dir, apply=True)
        self.assertEqual(res["already_cached"], 1)
        self.assertEqual(res["published"], 0)
        self.assertEqual(target_webp.read_bytes(), original_data)
        self.assertFalse((self.target_dir / "107.jpg").exists())

    def test_dry_run_does_not_modify_target(self):
        """Default dry-run mode plans publication but writes nothing to target_dir."""
        entry = self._create_staged_cover(107)
        self._write_manifest([entry])

        res = publish_covers(self.staging_dir, self.target_dir, apply=False)
        self.assertFalse(res["apply"])
        self.assertEqual(res["to_publish"], 1)
        self.assertEqual(res["published"], 0)
        self.assertEqual(len(os.listdir(self.target_dir)), 0)

    def test_atomic_apply_and_idempotence(self):
        """Apply creates file atomically; second run detects it as already cached."""
        entry = self._create_staged_cover(107)
        self._write_manifest([entry])

        # Run 1: publish
        res1 = publish_covers(self.staging_dir, self.target_dir, apply=True)
        self.assertEqual(res1["published"], 1)
        target_jpg = self.target_dir / "107.jpg"
        self.assertTrue(target_jpg.exists())
        self.assertEqual(target_jpg.read_bytes(), VALID_JPEG_BYTES)

        # Check no temporary files remained
        temp_files = [f for f in os.listdir(self.target_dir) if f.startswith(".tmp_pub")]
        self.assertEqual(temp_files, [])

        # Run 2: idempotent skip
        res2 = publish_covers(self.staging_dir, self.target_dir, apply=True)
        self.assertEqual(res2["already_cached"], 1)
        self.assertEqual(res2["published"], 0)

    def test_raced_final_creation_preserved(self):
        """If renameat2 reports EEXIST due to race, existing file is preserved and temp cleaned up."""
        entry = self._create_staged_cover(107)
        self._write_manifest([entry])

        # Mock _rename_noreplace to simulate race condition (target created just before rename)
        with patch("tools.publish_suwayomi_covers._rename_noreplace", return_value=False):
            res = publish_covers(self.staging_dir, self.target_dir, apply=True)
            self.assertEqual(res["skipped_race"], 1)
            self.assertEqual(res["published"], 0)

            # Ensure temp file was cleaned up
            temp_files = [f for f in os.listdir(self.target_dir) if f.startswith(".tmp_pub")]
            self.assertEqual(temp_files, [])

    def test_interrupted_write_cleans_up_temp_file(self):
        """If an error occurs during write/rename, temp file is removed."""
        entry = self._create_staged_cover(107)
        self._write_manifest([entry])

        # Mock _rename_noreplace to raise an unexpected error
        with patch("tools.publish_suwayomi_covers._rename_noreplace", side_effect=OSError(errno.EIO, "I/O error")):
            res = publish_covers(self.staging_dir, self.target_dir, apply=True)
            self.assertEqual(len(res["errors"]), 1)
            self.assertEqual(res["published"], 0)

            # Ensure temp file was cleaned up
            temp_files = [f for f in os.listdir(self.target_dir) if f.startswith(".tmp_pub")]
            self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
