import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from notes import NotesStore


class NotesStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.notes_file = os.path.join(self.temp_dir.name, "notes.json")
        self.store = NotesStore(self.notes_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_constructor_no_side_effects(self):
        non_existent_path = os.path.join(self.temp_dir.name, "subdir", "custom_notes.json")
        store = NotesStore(non_existent_path)
        self.assertEqual(store.path, non_existent_path)
        self.assertFalse(os.path.exists(non_existent_path))
        self.assertFalse(os.path.exists(os.path.dirname(non_existent_path)))

    def test_anonymous_and_empty_user(self):
        self.assertEqual(self.store.get(""), "")
        self.assertEqual(self.store.get(None), "")
        self.assertFalse(self.store.set("", "content"))
        self.assertFalse(self.store.set(None, "content"))

    def test_get_nonexistent_file(self):
        self.assertEqual(self.store.get("alice"), "")

    def test_roundtrip_and_two_user_isolation(self):
        self.assertTrue(self.store.set("alice", "Alice's secret notes"))
        self.assertTrue(self.store.set("bob", "Bob's reminders"))

        self.assertEqual(self.store.get("alice"), "Alice's secret notes")
        self.assertEqual(self.store.get("bob"), "Bob's reminders")

    def test_unicode_roundtrip(self):
        unicode_text = "Unicode test: 🚀 🌟 ñoño — 中文 ⚡️ \n line 2"
        self.assertTrue(self.store.set("alice", unicode_text))
        self.assertEqual(self.store.get("alice"), unicode_text)

        with open(self.notes_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["alice"], unicode_text)

    def test_file_permissions_600(self):
        self.assertTrue(self.store.set("alice", "secure notes"))
        st_mode = os.stat(self.notes_file).st_mode
        self.assertEqual(stat.S_IMODE(st_mode), 0o600)

    def test_corrupted_read_behavior(self):
        with open(self.notes_file, "w", encoding="utf-8") as f:
            f.write("INVALID JSON {[[")
        self.assertEqual(self.store.get("alice"), "")

    def test_corrupted_write_overwrites_cleanly(self):
        with open(self.notes_file, "w", encoding="utf-8") as f:
            f.write("CORRUPTED")
        self.assertTrue(self.store.set("alice", "fresh notes"))
        self.assertEqual(self.store.get("alice"), "fresh notes")

    def test_failed_replacement_cleans_tmp_and_preserves_prior(self):
        self.assertTrue(self.store.set("alice", "original notes"))

        # Make replace fail
        original_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("Simulated atomic replacement failure")

        os.replace = failing_replace
        try:
            success = self.store.set("alice", "new notes")
            self.assertFalse(success)
        finally:
            os.replace = original_replace

        tmp_file = self.notes_file + ".tmp"
        self.assertFalse(os.path.exists(tmp_file))
        self.assertEqual(self.store.get("alice"), "original notes")

    def test_concurrent_writes_through_one_store(self):
        threads = []
        errors = []

        def worker(idx):
            username = f"user_{idx}"
            content = f"content_{idx}"
            if not self.store.set(username, content):
                errors.append(f"set failed for {username}")
            if self.store.get(username) != content:
                errors.append(f"get mismatch for {username}")

        for i in range(20):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for i in range(20):
            self.assertEqual(self.store.get(f"user_{i}"), f"content_{i}")


if __name__ == "__main__":
    unittest.main()
