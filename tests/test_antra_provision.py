"""Provisioning must preserve account identity and never overwrite unrelated secrets."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from tools.provision_antra_account import provision, SetupError


class FakeOpener:
    def __init__(self, fail_register=False):
        self.calls = []
        self.username = None
        self.fail_register = fail_register

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        if request.data:
            self.username = json.loads(request.data)["username"]
        if self.fail_register and request.full_url.endswith("/register"):
            raise urllib.error.URLError("simulated interruption")
        return io.BytesIO(json.dumps({"username": self.username}).encode())


class ProvisionTests(unittest.TestCase):
    def test_interrupted_registration_keeps_identity_and_private_modes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "antra"
            failed = FakeOpener(fail_register=True)
            with self.assertRaises(urllib.error.URLError):
                provision(directory, lambda jar: failed)
            before = json.loads((directory / "credentials.json").read_text())
            resumed = FakeOpener()
            result = provision(directory, lambda jar: resumed)
            after = json.loads((directory / "credentials.json").read_text())
            self.assertTrue(result["authenticated"])
            self.assertEqual(before["username"], after["username"])
            self.assertEqual(before["password"], after["password"])
            self.assertNotIn("managed_pending", after)
            self.assertTrue(all(not path.endswith("/register") for path in resumed.calls))
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(directory / "credentials.json").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(directory / "cookies.txt").st_mode & 0o777, 0o600)
            self.assertNotIn(after["password"], json.dumps(result))

    def test_existing_symlink_is_preserved_without_network_access(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "antra"
            directory.mkdir(mode=0o700)
            sentinel = Path(root) / "secret"
            sentinel.write_text("untouched")
            (directory / "credentials.json").symlink_to(sentinel)
            opener = FakeOpener()
            with self.assertRaises(SetupError):
                provision(directory, lambda jar: opener)
            self.assertEqual(opener.calls, [])
            self.assertEqual(sentinel.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
