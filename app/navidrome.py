#!/usr/bin/env python3
"""Navidrome service launcher for mobile-server environment.

Validates the checksummed Navidrome static binary and executes it
inside the Debian PRoot container environment with isolated loopback config.
"""

import hashlib
import os
from pathlib import Path
import sys

EXPECTED_SHA256 = "e613bb7869f74bd99a7cb20116c69b4f3469b62e9c9193de1a32500e2040a84d"

HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
PREFIX = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))

BINARY_PATH = HOME / ".local/opt/navidrome/0.64.2/navidrome"
CONFIG_PATH = HOME / ".config/navidrome/navidrome.toml"
PROOT_DISTRO = PREFIX / "bin/proot-distro"


def verify_binary(binary_path: Path) -> None:
    if not binary_path.is_file():
        raise FileNotFoundError(f"Navidrome binary not found at {binary_path}")
    h = hashlib.sha256()
    with binary_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    actual_sha = h.hexdigest().lower()
    if actual_sha != EXPECTED_SHA256.lower():
        raise ValueError(
            f"Navidrome binary checksum mismatch: expected {EXPECTED_SHA256}, got {actual_sha}"
        )


def main() -> None:
    verify_binary(BINARY_PATH)
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Navidrome configuration not found at {CONFIG_PATH}")
    if not PROOT_DISTRO.is_file() or not os.access(PROOT_DISTRO, os.X_OK):
        raise FileNotFoundError(f"proot-distro executable not found at {PROOT_DISTRO}")

    argv = [
        str(PROOT_DISTRO),
        "login",
        "debian",
        "--bind", f"{HOME}:{HOME}",
        "--bind", f"{PREFIX}:{PREFIX}",
        "--bind", "/storage/emulated/0:/storage/emulated/0",
        "--",
        "/usr/bin/env",
        f"PATH={PREFIX}/bin:/usr/local/bin:/usr/bin:/bin",
        str(BINARY_PATH),
        "--configfile",
        str(CONFIG_PATH),
    ]
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
