import json
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

if __name__ == '__main__': unittest.main()
