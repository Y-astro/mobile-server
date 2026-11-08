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
import sys
import uuid
from pathlib import Path
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


def _rename_noreplace(src: Path, dst: Path) -> bool:
    """Atomically rename src to dst without replacing dst.

    Returns True if rename succeeded.
    Returns False if dst already exists (errno.EEXIST).
    Raises OSError on any other failure or if renameat2 is unavailable.
    """
    libc = _get_libc()
    if not libc:
        raise OSError(errno.ENOSYS, "C library not available for renameat2")

    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        src_bytes = str(src).encode("utf-8")
        dst_bytes = str(dst).encode("utf-8")
        ret = renameat2(AT_FDCWD, src_bytes, AT_FDCWD, dst_bytes, RENAME_NOREPLACE)
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
    ret = syscall(sys_renameat2, AT_FDCWD, src_bytes, AT_FDCWD, dst_bytes, RENAME_NOREPLACE)
    if ret == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        return False
    raise OSError(err, f"renameat2 syscall failed: {os.strerror(err)}")


def _validate_target_dir(target_dir: Path) -> Tuple[bool, str]:
    """Validate target directory exists, is real (not symlink), and writable."""
    if not target_dir.exists():
        return False, f"Target directory does not exist: {target_dir}"
    if target_dir.is_symlink():
        return False, f"Target directory is a symlink (rejected): {target_dir}"
    if not target_dir.is_dir():
        return False, f"Target path is not a directory: {target_dir}"
    if not os.access(target_dir, os.W_OK):
        return False, f"Target directory is not writable: {target_dir}"
    return True, "Target directory valid"


def _validate_image_signature(data: bytes) -> Tuple[bool, str]:
    """Validate image magic bytes (JPEG, PNG, WebP)."""
    if len(data) < 16:
        return False, "File too small for valid image header"
    if data.startswith(b"\xff\xd8\xff"):
        return True, "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, "PNG"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True, "WEBP"
    return False, "Unrecognized image magic header"


def _find_existing_cache_file(target_dir: Path, manga_id: int) -> Optional[str]:
    """Check if any valid thumbnail format exists for manga_id."""
    for ext in SAFE_EXTENSIONS:
        candidate = target_dir / f"{manga_id}{ext}"
        if candidate.exists():
            return candidate.name
    return None


def verify_manifest_and_staged_files(staging_dir: Path) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Verify staging directory structure, manifest format, and image integrity."""
    if not staging_dir.exists() or not staging_dir.is_dir():
        return False, f"Staging directory invalid: {staging_dir}", []
    if staging_dir.is_symlink():
        return False, f"Staging directory is a symlink: {staging_dir}", []

    manifest_path = staging_dir / "manifest.json"
    if not manifest_path.exists():
        return False, f"manifest.json missing in {staging_dir}", []
    if manifest_path.is_symlink():
        return False, "manifest.json is a symlink (rejected)", []

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return False, f"Failed to parse manifest.json: {e}", []

    if not isinstance(manifest, dict):
        return False, "manifest.json must be a JSON object", []
    if manifest.get("schema_version") != 1:
        return False, f"Unsupported manifest schema_version: {manifest.get('schema_version')}", []

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return False, "manifest 'entries' must be a list", []

    validated_entries: List[Dict[str, Any]] = []
    seen_ids = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return False, f"Entry [{idx}] is not an object", []

        manga_id = entry.get("manga_id")
        if not isinstance(manga_id, int) or manga_id <= 0:
            return False, f"Entry [{idx}] invalid manga_id: {manga_id}", []
        if manga_id in seen_ids:
            return False, f"Duplicate manga_id in manifest: {manga_id}", []
        seen_ids.add(manga_id)

        hid = entry.get("hid")
        if not isinstance(hid, str) or not re.match(r"^[a-zA-Z0-9_-]+$", hid):
            return False, f"Entry [{idx}] invalid hid: {hid}", []

        url = entry.get("canonical_url")
        if not isinstance(url, str) or not (url.startswith("https://static.mfcdn.") or url.startswith("https://s.mfcdn.")):
            return False, f"Entry [{idx}] invalid canonical_url: {url}", []

        filename = entry.get("filename")
        expected_filename = f"images/{manga_id}.jpg"
        if filename != expected_filename:
            return False, f"Entry [{idx}] filename mismatch: {filename} != {expected_filename}", []

        expected_bytes = entry.get("bytes")
        if not isinstance(expected_bytes, int) or expected_bytes <= 0 or expected_bytes > 10 * 1024 * 1024:
            return False, f"Entry [{idx}] invalid byte count: {expected_bytes}", []

        expected_sha256 = entry.get("sha256")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            return False, f"Entry [{idx}] invalid sha256: {expected_sha256}", []

        # Check actual staged file
        img_path = staging_dir / filename
        if not img_path.exists():
            return False, f"Staged image missing: {img_path}", []
        if img_path.is_symlink():
            return False, f"Staged image is a symlink (rejected): {img_path}", []

        actual_data = img_path.read_bytes()
        if len(actual_data) != expected_bytes:
            return False, f"Entry [{idx}] byte length mismatch: disk {len(actual_data)} != manifest {expected_bytes}", []

        actual_sha256 = hashlib.sha256(actual_data).hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            return False, f"Entry [{idx}] sha256 mismatch for {filename}", []

        sig_ok, sig_type = _validate_image_signature(actual_data)
        if not sig_ok:
            return False, f"Entry [{idx}] {filename} has invalid image header: {sig_type}", []

        validated_entries.append(entry)

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
    if limit is not None and limit > 0:
        entries = entries[:limit]

    # 3. Process entries
    for entry in entries:
        manga_id = entry["manga_id"]
        rel_img = entry["filename"]
        src_path = staging_dir / rel_img
        final_dest = target_dir / f"{manga_id}.jpg"

        # Check existing formats
        existing_name = _find_existing_cache_file(target_dir, manga_id)
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
        tmp_name = f".tmp_pub_{os.getpid()}_{manga_id}_{uuid.uuid4().hex[:8]}.jpg"
        tmp_path = target_dir / tmp_name

        try:
            # Write temp file with private permissions (0600)
            data = src_path.read_bytes()
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            # Atomic rename without replace
            success = _rename_noreplace(tmp_path, final_dest)
            if success:
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
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

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
