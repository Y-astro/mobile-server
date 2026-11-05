#!/usr/bin/env python3
"""Offline wallpaper builder, verifier, and publication tool.

Features:
- Offline host builder: reads/hashes original pictures, preserves originals byte-for-byte,
  and generates WebP display (<=2560px long edge) and thumbnail (<=320x180 contain) derivatives.
- Verifier (standard-library only): validates manifest integrity, file existence, and hashes.
- Publisher (standard-library only): idempotent publication with conflict refusal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

EXCLUDED_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_SRC_DIR = Path("/home/astro/Pictures/Wallpapers")
DEFAULT_DEST_DIR = Path(".work/wallpapers-library")
DEFAULT_INVENTORY_PATH = Path("/home/astro/repos/mobile-server/.superpowers/sdd/wallpaper-originals-inventory.json")
DEFAULT_PHONE_DEST = Path(os.path.expanduser("~/wallpapers"))
DEFAULT_IMAGE_NAME = "06. Frosted Glass.png"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_contain_dimensions(orig_w: int, orig_h: int, max_w: int, max_h: int, upscale: bool = False) -> Tuple[int, int]:
    if not upscale and orig_w <= max_w and orig_h <= max_h:
        return orig_w, orig_h
    scale = min(max_w / orig_w, max_h / orig_h)
    new_w = max(1, round(orig_w * scale))
    new_h = max(1, round(orig_h * scale))
    return new_w, new_h


def compute_display_dimensions(orig_w: int, orig_h: int, max_long_edge: int = 2560) -> Tuple[int, int]:
    long_edge = max(orig_w, orig_h)
    if long_edge <= max_long_edge:
        return orig_w, orig_h
    scale = max_long_edge / float(long_edge)
    new_w = max(1, round(orig_w * scale))
    new_h = max(1, round(orig_h * scale))
    return new_w, new_h


def verify_library(library_dir: Path) -> Tuple[bool, str]:
    manifest_path = library_dir / "manifest.json"
    if not library_dir.is_dir():
        return False, f"Directory does not exist: {library_dir}"
    if not manifest_path.is_file():
        return False, f"Missing manifest file: {manifest_path}"

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return False, f"Failed to parse manifest: {e}"

    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return False, "Invalid manifest version or structure"

    wallpapers = manifest.get("wallpapers")
    if not isinstance(wallpapers, list) or len(wallpapers) == 0:
        return False, "Manifest contains no wallpapers"

    default_id = manifest.get("default_id")
    found_ids = set()

    for item in wallpapers:
        w_id = item.get("id")
        name = item.get("name")
        width = item.get("width")
        height = item.get("height")
        if not w_id or not name or not width or not height:
            return False, f"Wallpaper entry missing basic attributes: {item}"
        found_ids.add(w_id)

        orig_meta = item.get("original")
        if not orig_meta or "path" not in orig_meta or "sha256" not in orig_meta:
            return False, f"Original metadata missing for {w_id}"
        orig_file = library_dir / orig_meta["path"]
        if not orig_file.is_file() or orig_file.is_symlink():
            return False, f"Original file missing or symlink: {orig_meta['path']}"
        if orig_meta.get("bytes") and orig_file.stat().st_size != orig_meta["bytes"]:
            return False, f"Original file byte size mismatch: {orig_meta['path']}"
        if sha256_file(orig_file) != orig_meta["sha256"]:
            return False, f"Original file hash mismatch: {orig_meta['path']}"

        for deriv_name in ("display", "thumbnail"):
            deriv_meta = item.get(deriv_name)
            if not deriv_meta or "path" not in deriv_meta or "sha256" not in deriv_meta:
                return False, f"Derivative metadata missing for {w_id} ({deriv_name})"
            deriv_file = library_dir / deriv_meta["path"]
            if not deriv_file.is_file() or deriv_file.is_symlink():
                return False, f"Derivative file missing or symlink: {deriv_meta['path']}"
            if deriv_meta.get("bytes") and deriv_file.stat().st_size != deriv_meta["bytes"]:
                return False, f"Derivative byte size mismatch: {deriv_meta['path']}"
            if sha256_file(deriv_file) != deriv_meta["sha256"]:
                return False, f"Derivative file hash mismatch: {deriv_meta['path']}"

    if default_id and default_id not in found_ids:
        return False, f"default_id {default_id} not present in wallpapers"

    return True, f"Library verified successfully: {len(wallpapers)} wallpapers intact"


def build_library(
    src_dir: Path = DEFAULT_SRC_DIR,
    dest_dir: Path = DEFAULT_DEST_DIR,
    inventory_path: Optional[Path] = DEFAULT_INVENTORY_PATH,
) -> Dict[str, Any]:
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Source wallpapers directory not found: {src_dir}")

    # Lazy import Pillow only for offline builder
    try:
        from PIL import Image, ImageOps
    except ImportError as e:
        raise RuntimeError("Pillow is required for building wallpapers. Install Pillow or run in host environment.") from e

    expected_inventory: Optional[Dict[str, Dict[str, Any]]] = None
    if inventory_path and inventory_path.is_file():
        try:
            with open(inventory_path, "r", encoding="utf-8") as f:
                inv_list = json.load(f)
                expected_inventory = {item["name"]: item for item in inv_list}
        except Exception as e:
            raise RuntimeError(f"Failed to read inventory file {inventory_path}: {e}")

    source_files: List[Path] = []
    for entry in sorted(src_dir.iterdir()):
        if entry.is_file():
            ext = entry.suffix.lower()
            if ext in EXCLUDED_EXTENSIONS:
                continue
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                source_files.append(entry)

    if expected_inventory is not None:
        if len(source_files) != len(expected_inventory):
            raise RuntimeError(
                f"Source file count mismatch: found {len(source_files)} images, expected {len(expected_inventory)} from inventory"
            )
        for s_file in source_files:
            if s_file.name not in expected_inventory:
                raise RuntimeError(f"Unexpected source image not in inventory: {s_file.name}")
            inv_entry = expected_inventory[s_file.name]
            f_size = s_file.stat().st_size
            if f_size != inv_entry["bytes"]:
                raise RuntimeError(
                    f"File size mismatch for {s_file.name}: disk={f_size}, inventory={inv_entry['bytes']}"
                )
            f_sha = sha256_file(s_file)
            if f_sha != inv_entry["sha256"]:
                raise RuntimeError(
                    f"SHA-256 mismatch for {s_file.name}: disk={f_sha}, inventory={inv_entry['sha256']}"
                )

    staging_dir = dest_dir.parent / (dest_dir.name + ".staging")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    orig_dir = staging_dir / "originals"
    disp_dir = staging_dir / "display"
    thumb_dir = staging_dir / "thumbnails"
    orig_dir.mkdir(parents=True, exist_ok=True)
    disp_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    wallpapers_meta = []
    default_id: Optional[str] = None
    transport_files: List[Dict[str, Any]] = []

    for s_file in source_files:
        orig_sha = sha256_file(s_file)
        orig_bytes = s_file.stat().st_size
        w_id = orig_sha

        if s_file.name == DEFAULT_IMAGE_NAME:
            default_id = w_id

        dest_orig = orig_dir / s_file.name
        shutil.copy2(s_file, dest_orig)
        transport_files.append({
            "path": f"originals/{s_file.name}",
            "sha256": orig_sha,
            "bytes": orig_bytes,
        })

        # Process one image at a time
        with Image.open(s_file) as raw_im:
            im = ImageOps.exif_transpose(raw_im)
            orig_w, orig_h = im.size

            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                work_im = im.convert("RGBA")
            else:
                work_im = im.convert("RGB")

            # Display derivative: long edge <= 2560 without upscaling
            disp_w, disp_h = compute_display_dimensions(orig_w, orig_h, max_long_edge=2560)
            if (disp_w, disp_h) != (orig_w, orig_h):
                disp_im = work_im.resize((disp_w, disp_h), Image.Resampling.LANCZOS)
            else:
                disp_im = work_im

            disp_file = disp_dir / f"{w_id}.webp"
            disp_im.save(disp_file, format="WEBP", quality=85)
            disp_bytes = disp_file.stat().st_size
            disp_sha = sha256_file(disp_file)
            transport_files.append({
                "path": f"display/{w_id}.webp",
                "sha256": disp_sha,
                "bytes": disp_bytes,
            })

            # Thumbnail derivative: <= 320x180 contain, aspect ratio preserved, no stretching
            thumb_w, thumb_h = compute_contain_dimensions(orig_w, orig_h, max_w=320, max_h=180, upscale=False)
            if (thumb_w, thumb_h) != (orig_w, orig_h):
                thumb_im = work_im.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            else:
                thumb_im = work_im

            thumb_file = thumb_dir / f"{w_id}.webp"
            thumb_im.save(thumb_file, format="WEBP", quality=80)
            thumb_bytes = thumb_file.stat().st_size
            thumb_sha = sha256_file(thumb_file)
            transport_files.append({
                "path": f"thumbnails/{w_id}.webp",
                "sha256": thumb_sha,
                "bytes": thumb_bytes,
            })

        wallpapers_meta.append({
            "id": w_id,
            "name": s_file.name,
            "width": orig_w,
            "height": orig_h,
            "original": {
                "path": f"originals/{s_file.name}",
                "sha256": orig_sha,
                "bytes": orig_bytes,
            },
            "display": {
                "path": f"display/{w_id}.webp",
                "sha256": disp_sha,
                "bytes": disp_bytes,
                "width": disp_w,
                "height": disp_h,
            },
            "thumbnail": {
                "path": f"thumbnails/{w_id}.webp",
                "sha256": thumb_sha,
                "bytes": thumb_bytes,
                "width": thumb_w,
                "height": thumb_h,
            },
        })

    wallpapers_meta.sort(key=lambda x: x["name"])
    if not default_id and wallpapers_meta:
        default_id = wallpapers_meta[0]["id"]

    manifest = {
        "version": 1,
        "default_id": default_id,
        "wallpapers": wallpapers_meta,
    }

    manifest_file = staging_dir / "manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    transport_files.append({
        "path": "manifest.json",
        "sha256": sha256_file(manifest_file),
        "bytes": manifest_file.stat().st_size,
    })

    transport_manifest = {
        "version": 1,
        "total_files": len(transport_files),
        "total_bytes": sum(item["bytes"] for item in transport_files),
        "files": transport_files,
        "root_instructions": [
            f"1. Rsync staged library: rsync -avz --checksum {dest_dir}/ u0_a505@100.75.159.53:wallpapers-staging/",
            "2. Verify staging on phone: ssh u0_a505@100.75.159.53 'python3 tools/prepare_wallpapers.py verify ~/wallpapers-staging'",
            "3. Publish on phone: ssh u0_a505@100.75.159.53 'python3 tools/prepare_wallpapers.py publish ~/wallpapers-staging ~/wallpapers'",
        ],
    }

    transport_file = staging_dir / "transport-manifest.json"
    with open(transport_file, "w", encoding="utf-8") as f:
        json.dump(transport_manifest, f, indent=2)

    # Verify staging before publishing to dest
    ok, msg = verify_library(staging_dir)
    if not ok:
        shutil.rmtree(staging_dir)
        raise RuntimeError(f"Staging verification failed: {msg}")

    # Check if dest_dir already exists
    if dest_dir.exists():
        dest_ok, _ = verify_library(dest_dir)
        if dest_ok:
            dest_manifest_file = dest_dir / "manifest.json"
            with open(dest_manifest_file, "r", encoding="utf-8") as f:
                dest_m = json.load(f)
            if dest_m.get("wallpapers") == manifest.get("wallpapers"):
                shutil.rmtree(staging_dir)
                return manifest
        # If dest exists with differing contents, refuse
        shutil.rmtree(staging_dir)
        raise RuntimeError(f"Destination {dest_dir} exists with conflicting data; refusing overwrite.")

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir.rename(dest_dir)
    return manifest


def publish_library(staging_dir: Path, target_dir: Path = DEFAULT_PHONE_DEST) -> Tuple[bool, str]:
    if not staging_dir.is_dir():
        return False, f"Staging directory {staging_dir} does not exist"

    ok, msg = verify_library(staging_dir)
    if not ok:
        return False, f"Staging library verification failed: {msg}"

    with open(staging_dir / "manifest.json", "r", encoding="utf-8") as f:
        staging_manifest = json.load(f)

    if target_dir.exists():
        target_ok, _ = verify_library(target_dir)
        if target_ok:
            try:
                with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
                    target_manifest = json.load(f)
                if target_manifest.get("wallpapers") == staging_manifest.get("wallpapers"):
                    return True, f"Target directory {target_dir} is already up to date"
            except Exception:
                pass
        return False, f"Refusing to overwrite conflicting target directory: {target_dir}"

    temp_publish = target_dir.parent / (target_dir.name + f".pub.{os.getpid()}")
    if temp_publish.exists():
        shutil.rmtree(temp_publish)

    try:
        shutil.copytree(staging_dir, temp_publish)
        temp_ok, temp_msg = verify_library(temp_publish)
        if not temp_ok:
            shutil.rmtree(temp_publish)
            return False, f"Copied library verification failed: {temp_msg}"
        temp_publish.rename(target_dir)
    except Exception as e:
        if temp_publish.exists():
            shutil.rmtree(temp_publish)
        return False, f"Failed to publish library: {e}"

    return True, f"Library published successfully to {target_dir}"


def main():
    parser = argparse.ArgumentParser(description="Wallpaper preparation, verification, and publication tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build offline library and derivatives")
    build_parser.add_argument("--src", type=Path, default=DEFAULT_SRC_DIR, help="Source images directory")
    build_parser.add_argument("--dest", type=Path, default=DEFAULT_DEST_DIR, help="Destination library directory")
    build_parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY_PATH, help="Originals inventory JSON path")

    verify_parser = subparsers.add_parser("verify", help="Verify library integrity using stdlib only")
    verify_parser.add_argument("path", type=Path, nargs="?", default=DEFAULT_DEST_DIR, help="Library directory to verify")

    publish_parser = subparsers.add_parser("publish", help="Publish verified staging library using stdlib only")
    publish_parser.add_argument("staging", type=Path, help="Staged library directory")
    publish_parser.add_argument("target", type=Path, nargs="?", default=DEFAULT_PHONE_DEST, help="Target publication directory")

    args = parser.parse_args()

    if args.command == "build":
        try:
            m = build_library(src_dir=args.src, dest_dir=args.dest, inventory_path=args.inventory)
            print(f"Successfully built {len(m['wallpapers'])} wallpapers under {args.dest}")
            print(f"Default ID: {m.get('default_id')}")
        except Exception as e:
            print(f"Build error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "verify":
        ok, msg = verify_library(args.path)
        if ok:
            print(f"VERIFY OK: {msg}")
        else:
            print(f"VERIFY FAILED: {msg}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "publish":
        ok, msg = publish_library(args.staging, args.target)
        if ok:
            print(f"PUBLISH OK: {msg}")
        else:
            print(f"PUBLISH FAILED: {msg}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
