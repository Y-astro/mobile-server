import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from auth import CredentialStore, LoginLimiter, hash_password, save_users, verify_password


class PasswordTests(unittest.TestCase):
    def test_salted_password_and_constant_time_verification(self):
        encoded = hash_password('example-test-password', iterations=100_000)
        self.assertNotIn('example-test-password', encoded)
        self.assertTrue(verify_password('example-test-password', encoded))
        self.assertFalse(verify_password('wrong', encoded))
        self.assertNotEqual(encoded, hash_password('example-test-password', iterations=100_000))

    def test_malformed_or_unbounded_hash_is_rejected(self):
        for value in ('bad', 'pbkdf2_sha256$9999999999$abc$abc', 'scrypt$100000$abc$abc'):
            self.assertFalse(verify_password('test', value))
        self.assertFalse(verify_password('x'*1025, hash_password('test', iterations=100_000)))

    def test_private_store_reload_and_disable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'users.json'
            record = {'operator': {'password_hash': hash_password('test-password', iterations=100_000), 'role':'admin'}}
            save_users(path, record)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            store = CredentialStore(path)
            self.assertTrue(store.verify('operator', 'test-password'))
            self.assertTrue(store.is_admin('operator'))
            self.assertFalse(store.verify('missing', 'test-password'))
            record['operator']['disabled'] = True
            save_users(path, record)
            self.assertFalse(store.exists('operator'))
            self.assertFalse(store.verify('operator', 'test-password'))

    def test_rate_limit_window_and_bounded_verifiers(self):
        now = [0]
        limiter = LoginLimiter(clock=lambda: now[0])
        for _ in range(5):
            self.assertTrue(limiter.allowed('source', 'operator'))
            limiter.record('source', 'operator', False)
        self.assertFalse(limiter.allowed('source', 'operator'))
        now[0] = 601
        self.assertTrue(limiter.allowed('source', 'operator'))
        self.assertTrue(limiter.verifiers.acquire(False))
        self.assertTrue(limiter.verifiers.acquire(False))
        self.assertFalse(limiter.verifiers.acquire(False))

    def test_credential_freshness_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'users.json'
            users = {
                'alice': {'password_hash': hash_password('alice-pass', iterations=100_000), 'role': 'admin'},
                'bob': {'password_hash': hash_password('bob-pass', iterations=100_000), 'role': 'family'}
            }
            save_users(path, users)
            store = CredentialStore(path)

            # Baseline checks
            self.assertTrue(store.verify('alice', 'alice-pass'))
            self.assertTrue(store.is_admin('alice'))
            self.assertTrue(store.verify('bob', 'bob-pass'))
            self.assertFalse(store.is_admin('bob'))

            # Unchanged file uses cached parsed dict rather than re-reading/re-parsing
            orig_users_dict = store.users
            with patch.object(Path, 'read_text', side_effect=AssertionError("read_text called")):
                self.assertIs(store.users, orig_users_dict)
                self.assertTrue(store.verify('alice', 'alice-pass'))

            # Immediate role downgrade
            users['alice']['role'] = 'family'
            save_users(path, users)
            self.assertTrue(store.verify('alice', 'alice-pass'))
            self.assertFalse(store.is_admin('alice'))

            # Immediate removal
            del users['bob']
            save_users(path, users)
            self.assertFalse(store.exists('bob'))
            self.assertFalse(store.verify('bob', 'bob-pass'))

            # Immediate disable
            users['alice']['disabled'] = True
            save_users(path, users)
            self.assertFalse(store.exists('alice'))
            self.assertFalse(store.verify('alice', 'alice-pass'))

            # Baseline reenable check before replacement
            users['alice']['disabled'] = False
            save_users(path, users)
            self.assertTrue(store.exists('alice'))
            self.assertTrue(store.verify('alice', 'alice-pass'))

            # Atomic replacement with same size / mtime via inode replacement
            pass2 = hash_password('alice-pass-2', iterations=100_000)
            users['alice']['password_hash'] = pass2
            # Write to a temp file and replace atomically
            temp_file = Path(d) / 'temp_replace.json'
            save_users(temp_file, users)
            stat_before = path.stat()
            # Ensure same mtime
            os.utime(temp_file, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
            self.assertEqual(temp_file.stat().st_size, stat_before.st_size)
            self.assertEqual(temp_file.stat().st_mtime_ns, stat_before.st_mtime_ns)
            os.replace(temp_file, path)
            stat_after = path.stat()
            self.assertNotEqual(stat_before.st_ino, stat_after.st_ino)
            self.assertEqual(stat_before.st_size, stat_after.st_size)
            self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)
            self.assertTrue(store.verify('alice', 'alice-pass-2'))
            self.assertFalse(store.verify('alice', 'alice-pass'))

            # Corrupted store fails closed and denies stale user
            path.write_text('{ invalid json')
            self.assertFalse(store.exists('alice'))
            self.assertFalse(store.verify('alice', 'alice-pass-2'))
            self.assertEqual(store.users, {})

            # Recoverable after valid replacement
            save_users(path, users)
            self.assertTrue(store.exists('alice'))
            self.assertTrue(store.verify('alice', 'alice-pass-2'))

            # Missing store fails closed and denies stale user
            path.unlink()
            self.assertFalse(store.exists('alice'))
            self.assertFalse(store.verify('alice', 'alice-pass-2'))
            self.assertEqual(store.users, {})

            # Recoverable again after valid replacement
            save_users(path, users)
            self.assertTrue(store.exists('alice'))

            # Invalidate forces reload
            store.invalidate()
            self.assertTrue(store.exists('alice'))

if __name__ == '__main__': unittest.main()
