#!/usr/bin/env python3
"""publish_suwayomi_covers.py

Atomic, idempotent publisher for Suwayomi manga thumbnail cache.
Uses Linux/Android renameat2(RENAME_NOREPLACE) to ensure existing thumbnails
(and concurrent Java thumbnail writes) are never overwritten.

Standard library only. Designed to run on host or phone.
Default mode is dry-run; pass --apply to publish.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple

AT_FDCWD = -100
RENAME_NOREPLACE = 1

SAFE_EXTENSIONS = (".webp", ".jpg", ".jpeg", ".png", ".avif")
DEFAULT_PHONE_THUMBNAIL_DIR = Path("/data/data/com.termux/files/home/suwayomi/data/downloads/thumbnails")


def _get_libc() -> Optional[ctypes.CDLL]:
    for name in (None, "libc.so", "libc.so.6"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except Exception:
            pass
    return None


def _rename_noreplace(src: Path, dst: Path, dir_fd: int = AT_FDCWD) -> bool:
    """Atomically rename src to dst without replacing dst.

    Returns True if rename succeeded.
    Returns False if dst already exists (errno.EEXIST).
    Raises OSError on any other failure or if renameat2 is unavailable.
    """
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "Atomic publication requires Linux/Android")
    src_bytes = os.fsencode(src)
    dst_bytes = os.fsencode(dst)
    libc = _get_libc()
    if not libc:
        raise OSError(errno.ENOSYS, "C library not available for renameat2")

    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        ret = renameat2(dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_NOREPLACE)
        if ret == 0:
            return True
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            return False
        raise OSError(err, f"renameat2 failed: {os.strerror(err)}")

    # Fallback to syscall if symbol not exported
    syscall = getattr(libc, "syscall", None)
    if syscall is None:
        raise OSError(errno.ENOSYS, "renameat2 and syscall not found in libc")

    import platform
    machine = platform.machine().lower()
    sys_renameat2 = None
    if machine in ("x86_64", "amd64"):
        sys_renameat2 = 316
    elif machine in ("aarch64", "arm64"):
        sys_renameat2 = 276
    elif machine.startswith("arm"):
        sys_renameat2 = 382
    elif machine in ("i386", "i686", "x86"):
        sys_renameat2 = 353
    elif machine == "riscv64":
        sys_renameat2 = 276

    if sys_renameat2 is None:
        raise OSError(errno.ENOSYS, f"SYS_renameat2 syscall unknown for architecture {machine}")

    syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    syscall.restype = ctypes.c_int
    ret = syscall(sys_renameat2, dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_NOREPLACE)
    if ret == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        return False
    raise OSError(err, f"renameat2 syscall failed: {os.strerror(err)}")


def _validate_target_dir(target_dir: Path) -> Tuple[bool, str]:
    """Validate target directory exists, is real (not symlink), and writable."""
    target_dir = target_dir.absolute()
    if target_dir.is_symlink() or target_dir.resolve() != target_dir:
        return False, "Directory or parent is a symlink or traversal path"
    try:
        info = target_dir.stat()
    except OSError:
        return False, f"Target directory does not exist: {target_dir}"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return False, "Directory must be real and owned by the current user"
    if not os.access(target_dir, os.W_OK):
        return False, f"Target directory is not writable: {target_dir}"
    return True, "Target directory valid"


def _validate_image_signature(data: bytes) -> Tuple[bool, str]:
    """Validate image magic bytes (JPEG, PNG, WebP)."""
    if len(data) < 16:
        return False, "File too small for valid image header"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return True, "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, "PNG"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True, "WEBP"
    return False, "Unrecognized image magic header"


def _find_existing_cache_file(target_dir: Path, manga_id: int, dir_fd: Optional[int] = None) -> Optional[str]:
    """Check if any valid thumbnail format exists for manga_id."""
    for ext in SAFE_EXTENSIONS:
        candidate = target_dir / f"{manga_id}{ext}"
        try:
            os.stat(candidate.name if dir_fd is not None else candidate, dir_fd=dir_fd, follow_symlinks=False)
            return candidate.name
        except FileNotFoundError:
            pass
    return None


def verify_manifest_and_staged_files(staging_dir: Path) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Verify staging directory structure, manifest format, and image integrity."""
    if not staging_dir.exists() or not staging_dir.is_dir():
        return False, f"Staging directory invalid: {staging_dir}", []
    if staging_dir.is_symlink():
        return False, f"Staging directory is a symlink: {staging_dir}", []

    if staging_dir.resolve() != staging_dir.absolute():
        return False, "Staging parent symlink or traversal rejected", []
    images_dir = staging_dir / "images"
    if images_dir.is_symlink() or not images_dir.is_dir():
        return False, "images directory symlink or missing directory rejected", []
    manifest_path = staging_dir / "manifest.json"
    if not manifest_path.exists():
        return False, f"manifest.json missing in {staging_dir}", []
    if manifest_path.is_symlink():
        return False, "manifest.json is a symlink (rejected)", []

    if not manifest_path.is_file() or manifest_path.stat().st_size > 256 * 1024:
        return False, "Manifest must be a bounded regular file", []
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return False, f"Failed to parse manifest.json: {e}", []

    if not isinstance(manifest, dict):
        return False, "manifest.json must be a JSON object", []
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        return False, f"Unsupported manifest schema_version: {manifest.get('schema_version')}", []

    if manifest.get("source_name") != "MangaFire" or manifest.get("source_id") != "6084907896154116083":
        return False, "Unexpected source identity", []
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return False, "manifest 'entries' must be a list", []

    if len(entries) > 128:
        return False, "Too many manifest entries", []
    if set(p.name for p in staging_dir.iterdir()) != {"manifest.json", "images"}:
        return False, "Unexpected staging files", []
    validated_entries: List[Dict[str, Any]] = []
    total_bytes = 0
    seen_ids = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return False, f"Entry [{idx}] is not an object", []

        manga_id = entry.get("manga_id")
        if type(manga_id) is not int or not 0 < manga_id < 2 ** 31:
            return False, f"Entry [{idx}] invalid manga_id: {manga_id}", []
        if manga_id in seen_ids:
            return False, f"Duplicate manga_id in manifest: {manga_id}", []
        seen_ids.add(manga_id)

        hid = entry.get("hid")
        if not isinstance(hid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", hid):
            return False, f"Entry [{idx}] invalid hid: {hid}", []

        url = entry.get("canonical_url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = (parsed and parsed.scheme == "https" and parsed.netloc == "static.mfcdn.nl"
                         and not parsed.query and not parsed.fragment
                         and re.fullmatch(r"/[a-zA-Z0-9/_-]+(?:@[1-9][0-9]{0,3})?\.jpe?g", parsed.path)
                         and len(url) <= 2048)
        except ValueError:
            valid_url = False
        if not valid_url:
            return False, f"Entry [{idx}] invalid canonical_url", []

        filename = entry.get("filename")
        expected_filename = f"images/{manga_id}.jpg"
        if filename != expected_filename:
            return False, f"Entry [{idx}] filename mismatch: {filename} != {expected_filename}", []

        expected_bytes = entry.get("bytes")
        if type(expected_bytes) is not int or not 0 < expected_bytes <= 4 * 1024 * 1024:
            return False, f"Entry [{idx}] invalid byte count: {expected_bytes}", []

        total_bytes += expected_bytes
        if total_bytes > 64 * 1024 * 1024:
            return False, "Staging dataset exceeds size limit", []
        if entry.get("format") != "JPEG" or any(type(entry.get(k)) is not int or not 0 < entry[k] <= 4000 for k in ("width", "height")):
            return False, "Expected decoded JPEG dimensions", []
        expected_sha256 = entry.get("sha256")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return False, f"Entry [{idx}] invalid sha256: {expected_sha256}", []

        # Check actual staged file
        img_path = staging_dir / filename
        if not img_path.exists():
            return False, f"Staged image missing: {img_path}", []
        if img_path.is_symlink():
            return False, f"Staged image is a symlink (rejected): {img_path}", []

        try:
            fd = os.open(img_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as image:
                if not stat.S_ISREG(os.fstat(image.fileno()).st_mode):
                    return False, "Image must be a regular file", []
                actual_data = image.read(4 * 1024 * 1024 + 1)
        except OSError as exc:
            return False, f"Cannot read staged image: {exc}", []
        if len(actual_data) != expected_bytes:
            return False, f"Entry [{idx}] byte length mismatch: disk {len(actual_data)} != manifest {expected_bytes}", []

        actual_sha256 = hashlib.sha256(actual_data).hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            return False, f"Entry [{idx}] sha256 mismatch for {filename}", []

        sig_ok, sig_type = _validate_image_signature(actual_data)
        if not sig_ok or sig_type != "JPEG":
            return False, f"Entry [{idx}] {filename} has invalid image header: {sig_type}", []

        # Publish the verified snapshot, never re-read mutable staging paths.
        validated_entries.append({**entry, "_data": actual_data})

    if set(p.name for p in images_dir.iterdir()) != {f"{entry['manga_id']}.jpg" for entry in validated_entries}:
        return False, "Unexpected image files", []
    return True, f"Verified {len(validated_entries)} entries in manifest", validated_entries


def publish_covers(
    staging_dir: Path,
    target_dir: Path,
    apply: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Publish staged covers to target thumbnail cache.

    If apply is False (default), performs dry-run checks only.
    If apply is True, copies staged file to hidden temp file in target_dir and
    atomically moves with renameat2(RENAME_NOREPLACE).
    """
    results: Dict[str, Any] = {
        "apply": apply,
        "target_dir": str(target_dir),
        "total_manifest_entries": 0,
        "already_cached": 0,
        "to_publish": 0,
        "published": 0,
        "skipped_race": 0,
        "errors": [],
        "entries": [],
    }

    # 1. Validate target directory
    tgt_ok, tgt_msg = _validate_target_dir(target_dir)
    if not tgt_ok:
        results["errors"].append(tgt_msg)
        return results

    # 2. Verify staging directory and manifest
    stg_ok, stg_msg, entries = verify_manifest_and_staged_files(staging_dir)
    if not stg_ok:
        results["errors"].append(stg_msg)
        return results

    results["total_manifest_entries"] = len(entries)
    if limit is not None:
        if type(limit) is not int or limit <= 0:
            results["errors"].append("Limit must be a positive integer")
            return results
        entries = entries[:limit]

    # 3. Process entries
    target_dir = target_dir.absolute()
    target_fd = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = os.fstat(target_fd)
    try:
        if opened.st_uid != os.getuid():
            raise ValueError("Target owner changed")
        for entry in entries:
            if target_dir.resolve() != target_dir:
                raise ValueError("Target parent changed to a symlink")
            current = target_dir.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Target directory changed")
            manga_id = entry["manga_id"]
            final_dest = target_dir / f"{manga_id}.jpg"

            # Check existing formats
            existing_name = _find_existing_cache_file(target_dir, manga_id, target_fd)
            if existing_name:
                results["already_cached"] += 1
                results["entries"].append({
                    "manga_id": manga_id,
                    "status": "ALREADY_CACHED",
                    "existing_file": existing_name,
                })
                continue

            results["to_publish"] += 1

            if not apply:
                results["entries"].append({
                    "manga_id": manga_id,
                    "status": "PLANNED",
                    "dest": final_dest.name,
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                })
                continue

            # In --apply mode: copy to hidden temp file and atomic renameat2(RENAME_NOREPLACE)
            tmp_name = f".tmp_pub_{os.getpid()}_{manga_id}_{uuid.uuid4().hex}.jpg"

            owned_temp = None
            try:
                # Write temp file with private permissions (0600)
                data = entry["_data"]
                fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target_fd)
                info = os.fstat(fd)
                owned_temp = (info.st_dev, info.st_ino)
                with os.fdopen(fd, "wb") as f:
                    os.fchmod(f.fileno(), 0o600)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())

                # Atomic rename without replace
                success = _rename_noreplace(Path(tmp_name), Path(final_dest.name), target_fd)
                if success:
                    os.fsync(target_fd)
                    current = target_dir.stat(follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or target_dir.resolve() != target_dir:
                        raise ValueError("Target directory changed during publication")
                    results["published"] += 1
                    results["entries"].append({
                        "manga_id": manga_id,
                        "status": "PUBLISHED",
                        "dest": final_dest.name,
                    })
                else:
                    # Target was created concurrently (EEXIST)
                    results["skipped_race"] += 1
                    results["entries"].append({
                        "manga_id": manga_id,
                        "status": "SKIPPED_RACE",
                        "dest": final_dest.name,
                    })
            except Exception as e:
                results["errors"].append(f"Manga {manga_id} publish error: {e}")
                results["entries"].append({
                    "manga_id": manga_id,
                    "status": "ERROR",
                    "error": str(e),
                })
            finally:
                if owned_temp is not None:
                    try:
                        info = os.stat(tmp_name, dir_fd=target_fd, follow_symlinks=False)
                        if (info.st_dev, info.st_ino) == owned_temp:
                            os.unlink(tmp_name, dir_fd=target_fd)
                    except FileNotFoundError:
                        pass
    finally:
        os.close(target_fd)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Suwayomi manga thumbnail covers atomically.")
    parser.add_argument("--staging-dir", type=Path, required=True, help="Path to verified staging directory")
    parser.add_argument("--target-dir", type=Path, default=DEFAULT_PHONE_THUMBNAIL_DIR, help="Target thumbnails directory")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of processed entries")
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")

    args = parser.parse_args()

    result = publish_covers(
        staging_dir=args.staging_dir,
        target_dir=args.target_dir,
        apply=args.apply,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        mode = "APPLY" if result["apply"] else "DRY-RUN"
        print(f"=== SUWAYOMI COVER PUBLICATION ({mode}) ===")
        print(f"Target: {result['target_dir']}")
        print(f"Total manifest entries: {result['total_manifest_entries']}")
        print(f"Already cached: {result['already_cached']}")
        print(f"To publish: {result['to_publish']}")
        if result["apply"]:
            print(f"Published: {result['published']}")
            print(f"Skipped (race): {result['skipped_race']}")
        if result["errors"]:
            print(f"Errors ({len(result['errors'])}):")
            for err in result["errors"]:
                print(f"  - {err}")

    if result["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
