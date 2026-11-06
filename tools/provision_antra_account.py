#!/usr/bin/env python3
"""Provision one approved Antra account; keep credentials and cookies on the phone."""
import fcntl
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import urllib.error
import urllib.request

ORIGIN = "https://antra.hoshi.cfd"


class SetupError(Exception):
    pass


def check_private(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        raise SetupError("Existing account file must be an owned regular file with mode 0600")


def replace_private(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".antra-setup-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def call(opener, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(ORIGIN + path, data=body, headers={
        "User-Agent": "MobileServerAntra/1.0", "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            content = response.read(16385)
            if len(content) > 16384:
                raise SetupError("Unexpected oversized account response")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise SetupError("Unexpected account response")
            return data
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise SetupError(f"Antra returned HTTP {code}") from None


def provision(config_dir, opener_factory=None):
    config_dir = Path(config_dir)
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = config_dir.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise SetupError("Account directory must be owned and private (0700)")
    fd = os.open(config_dir / "provision.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _provision_locked(config_dir, opener_factory)


def _provision_locked(config_dir, opener_factory):
    credentials = config_dir / "credentials.json"
    fresh = not os.path.lexists(credentials)
    if fresh:
        record = {"username": "astro_music_" + secrets.token_hex(4),
                  "password": "Music" + secrets.token_hex(8), "managed_pending": True}
        fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
    check_private(credentials)
    with credentials.open("rb") as stream:
        original = stream.read(16385)
    if len(original) > 16384:
        raise SetupError("Account file is too large")
    record = json.loads(original)
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k] for k in ("username", "password")):
        raise SetupError("Invalid account configuration")
    cookies_path = config_dir / "cookies.txt"
    if os.path.lexists(cookies_path):
        check_private(cookies_path)
    jar = http.cookiejar.MozillaCookieJar()
    opener = opener_factory(jar) if opener_factory else urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    payload = {key: record[key] for key in ("username", "password")}
    registered = False
    if fresh:
        call(opener, "/api/auth/register", payload)
        registered = True
    else:
        try:
            call(opener, "/api/auth/login", payload)
        except SetupError as error:
            # Retry the same identity after an interrupted first registration; never rotate accounts.
            if str(error) != "Antra returned HTTP 401" or record.get("managed_pending") is not True:
                raise
            call(opener, "/api/auth/register", payload)
            registered = True
    profile = call(opener, "/api/auth/me")
    if profile.get("username") != record["username"]:
        raise SetupError("Could not verify the signed-in account")
    if credentials.read_bytes() != original:
        raise SetupError("Account configuration changed during setup")
    record.pop("managed_pending", None)
    replace_private(credentials, (json.dumps(record) + "\n").encode())
    fd, temporary = tempfile.mkstemp(dir=config_dir, prefix=".antra-cookies-")
    os.close(fd)
    try:
        jar.save(temporary, ignore_discard=True, ignore_expires=True)
        os.replace(temporary, cookies_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"authenticated": True, "created_new_account": registered,
            "saved_cookie_count": len(jar),
            "limits": {key: profile.get(key) for key in ("albums_limit", "playlists_limit", "singles_limit")}}


if __name__ == "__main__":
    try:
        print(json.dumps(provision(Path.home() / ".config/antra")))
    except (SetupError, OSError, ValueError):
        print(json.dumps({"authenticated": False, "error": "Account setup failed; credentials were preserved. Check connectivity, service availability, and private-file permissions."}))
        raise SystemExit(1)
