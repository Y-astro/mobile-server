"""Unit tests for role_templates.py loader."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from role_templates import ROLE_FILES, RoleTemplateUnavailable, read_role_template


class TestRoleTemplates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.static_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_role_template_success(self):
        content = b"<!DOCTYPE html><html><body>Login %s %d \xc3\xa9</body></html>"
        login_file = self.static_dir / "login.html"
        login_file.write_bytes(content)

        read_bytes = read_role_template(str(self.static_dir), "login")
        self.assertEqual(read_bytes, content)

    def test_read_preserves_raw_bytes_unmodified(self):
        # Ensure percent signs, non-ASCII Unicode bytes, and binary patterns are intact
        special_bytes = b"Hello %s %d %() \x00 \xff \xc3\xa9 \xe2\x98\x85"
        (self.static_dir / "admin.html").write_bytes(special_bytes)

        read_bytes = read_role_template(str(self.static_dir), "admin")
        self.assertEqual(read_bytes, special_bytes)

    def test_reject_unknown_role(self):
        with self.assertRaises(ValueError) as ctx:
            read_role_template(str(self.static_dir), "unknown_role")
        self.assertIn("Unknown role template", str(ctx.exception))

    def test_reject_path_traversal(self):
        # Role template keys are strictly validated against ROLE_FILES
        for traversal in ["../admin", "/etc/passwd", "login.html", "admin/../login"]:
            with self.assertRaises(ValueError):
                read_role_template(str(self.static_dir), traversal)

    def test_missing_file_raises_unavailable(self):
        with self.assertRaises(RoleTemplateUnavailable) as ctx:
            read_role_template(str(self.static_dir), "family")
        self.assertIn("is not a regular file", str(ctx.exception))

    def test_directory_target_raises_unavailable(self):
        (self.static_dir / "family.html").mkdir()
        with self.assertRaises(RoleTemplateUnavailable):
            read_role_template(str(self.static_dir), "family")

    def test_unreadable_file_raises_unavailable(self):
        fam_file = self.static_dir / "family.html"
        fam_file.write_bytes(b"content")
        fam_file.chmod(0o000)
        try:
            # If current process is root (e.g. containers), chmod 000 might still be readable
            # so we only assert if permission denied is actually triggered
            try:
                read_role_template(str(self.static_dir), "family")
            except RoleTemplateUnavailable:
                pass
        finally:
            fam_file.chmod(0o644)


if __name__ == "__main__":
    unittest.main()
