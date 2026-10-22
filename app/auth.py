"""Password verification and bounded login throttling; no plaintext credentials."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

ITERATIONS = 600_000


def hash_password(password, *, iterations=ITERATIONS):
    if not isinstance(password, str) or not password:
        raise ValueError('Password must not be empty')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return 'pbkdf2_sha256${}${}${}'.format(iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password, encoded):
    if not isinstance(password, str) or len(password.encode()) > 1024:
        return False
    try:
        scheme, rounds, salt, expected = encoded.split('$')
        rounds = int(rounds)
        if scheme != 'pbkdf2_sha256' or not 100_000 <= rounds <= 2_000_000:
            return False
        salt = base64.b64decode(salt, validate=True)
        expected = base64.b64decode(expected, validate=True)
        if len(salt) < 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class CredentialStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('MOBILE_SERVER_USERS', '~/.config/dashboard/users.json')).expanduser()
        self._lock = threading.Lock()
        self._version = None
        self._users = {}
        self._dummy = hash_password(secrets.token_urlsafe(32))

    @property
    def users(self):
        with self._lock:
            try:
                stat = self.path.stat()
                version = (stat.st_mtime_ns, stat.st_size)
                if version != self._version:
                    data = json.loads(self.path.read_text())
                    if data.get('version') != 1 or not isinstance(data.get('users'), dict):
                        raise ValueError('Invalid credential store')
                    users = data['users']
                    if any(not isinstance(u, dict) or u.get('role') not in ('admin', 'family') or not isinstance(u.get('password_hash'), str) for u in users.values()):
                        raise ValueError('Invalid user record')
                    self._users = users
                    self._version = version
            except FileNotFoundError:
                self._users = {}
                self._version = None
            return dict(self._users)

    def verify(self, username, password):
        record = self.users.get(username)
        valid = verify_password(password, record['password_hash'] if record else self._dummy)
        return bool(record and not record.get('disabled') and valid)

    def is_admin(self, username):
        record = self.users.get(username) or {}
        return record.get('role') == 'admin' and not record.get('disabled')

    def exists(self, username):
        record = self.users.get(username)
        return bool(record and not record.get('disabled'))


class LoginLimiter:
    """Bound failures per source and per account, and bound stored keys."""
    def __init__(self, window=600, max_keys=4096, clock=time.monotonic):
        self.window, self.max_keys, self.clock = window, max_keys, clock
        self._failures = {}
        self._lock = threading.Lock()
        self.verifiers = threading.BoundedSemaphore(2)

    def _prune(self, now):
        for key in list(self._failures):
            self._failures[key] = [t for t in self._failures[key] if now-t < self.window]
            if not self._failures[key]:
                del self._failures[key]

    def allowed(self, client, username):
        with self._lock:
            now = self.clock()
            self._prune(now)
            return (len(self._failures.get(('ip', client), [])) < 20
                    and len(self._failures.get(('user', username), [])) < 5
                    and len(self._failures) < self.max_keys)

    def record(self, client, username, success):
        with self._lock:
            if success:
                # Source abuse is not reset by authenticating another account.
                self._failures.pop(('user', username), None)
            else:
                now = self.clock()
                self._prune(now)
                for key in (('ip', client), ('user', username)):
                    if len(self._failures) < self.max_keys or key in self._failures:
                        self._failures.setdefault(key, []).append(now)


def save_users(path, users):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'version': 1, 'users': users}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    temp.replace(path)
