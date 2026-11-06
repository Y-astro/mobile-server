#!/usr/bin/env python3
"""Offline wallpaper builder, verifier, and publication tool.

Features:
- Offline host builder: reads/hashes original pictures, preserves originals byte-for-byte,
  and generates WebP display (<=2560px long edge) and thumbnail (<=320x180 contain) derivatives.
- Verifier (standard-library only): strict dataset trust boundary validation.
- Publisher (standard-library only): idempotent publication with conflict refusal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

EXCLUDED_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_SRC_DIR = Path("/home/astro/Pictures/Wallpapers")
DEFAULT_DEST_DIR = Path(".work/wallpapers-library")
DEFAULT_INVENTORY_PATH = Path("/home/astro/repos/mobile-server/.superpowers/sdd/wallpaper-originals-inventory.json")
DEFAULT_PHONE_DEST = Path(os.path.expanduser("~/wallpapers"))
DEFAULT_IMAGE_NAME = "06. Frosted Glass.png"
ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024  # 2 MiB


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


def _is_valid_pos_int(val: Any) -> bool:
    return type(val) is int and not isinstance(val, bool) and val > 0


def verify_library(library_dir: Path) -> Tuple[bool, str]:
    if library_dir.is_symlink():
        return False, f"Library root is a symlink: {library_dir}"
    if not library_dir.is_dir():
        return False, f"Directory does not exist: {library_dir}"

    root_resolved = library_dir.resolve()
    manifest_path = library_dir / "manifest.json"
    if manifest_path.is_symlink():
        return False, f"manifest.json is a symlink: {manifest_path}"
    if not manifest_path.is_file():
        return False, f"Missing manifest file: {manifest_path}"

    try:
        st = manifest_path.stat()
        if st.st_size > MAX_MANIFEST_BYTES or st.st_size == 0:
            return False, f"manifest.json size out of bounds: {st.st_size} bytes"
        with open(manifest_path, "rb") as f:
            content = f.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            return False, "manifest.json exceeds size limit"
        manifest = json.loads(content)
    except Exception as e:
        return False, f"Failed to parse manifest: {e}"

    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        return False, "Invalid manifest version or structure"

    wallpapers = manifest.get("wallpapers")
    if not isinstance(wallpapers, list) or len(wallpapers) == 0:
        return False, "Manifest contains no wallpapers"

    default_id = manifest.get("default_id")
    if default_id is not None and (not isinstance(default_id, str) or not ID_PATTERN.fullmatch(default_id)):
        return False, f"Invalid default_id format: {default_id}"

    # Required subdirectories
    for subdir in ("originals", "display", "thumbnails"):
        sub_p = library_dir / subdir
        if sub_p.is_symlink():
            return False, f"Subdirectory is a symlink: {subdir}"
        if not sub_p.is_dir():
            return False, f"Missing required subdirectory: {subdir}"
        if not sub_p.resolve().is_relative_to(root_resolved):
            return False, f"Subdirectory escapes root: {subdir}"

    expected_files: Set[str] = {"manifest.json"}
    seen_ids: Set[str] = set()
    seen_names: Set[str] = set()

    for item in wallpapers:
        if not isinstance(item, dict):
            return False, f"Wallpaper item is not an object: {item}"
        w_id = item.get("id")
        name = item.get("name")
        width = item.get("width")
        height = item.get("height")

        if not isinstance(w_id, str) or not ID_PATTERN.fullmatch(w_id):
            return False, f"Invalid wallpaper ID: {w_id}"
        if w_id in seen_ids:
            return False, f"Duplicate wallpaper ID: {w_id}"
        seen_ids.add(w_id)

        if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name or ".." in name:
            return False, f"Invalid wallpaper name: {name}"
        if name in seen_names:
            return False, f"Duplicate wallpaper name: {name}"
        seen_names.add(name)

        if not _is_valid_pos_int(width) or not _is_valid_pos_int(height):
            return False, f"Invalid dimensions for {w_id}: width={width}, height={height}"

        # 1. Original file checks
        orig_meta = item.get("original")
        if not isinstance(orig_meta, dict):
            return False, f"Original metadata missing or invalid for {w_id}"
        orig_path = orig_meta.get("path")
        expected_orig_path = f"originals/{name}"
        if orig_path != expected_orig_path:
            return False, f"Original path {orig_path!r} does not match canonical {expected_orig_path!r}"
        if orig_meta.get("sha256") != w_id:
            return False, f"Original sha256 does not match wallpaper ID: {orig_meta.get('sha256')} vs {w_id}"
        orig_bytes = orig_meta.get("bytes")
        if not _is_valid_pos_int(orig_bytes):
            return False, f"Invalid byte size for original {w_id}: {orig_bytes}"

        orig_file = library_dir / expected_orig_path
        if orig_file.is_symlink():
            return False, f"Original file is a symlink: {expected_orig_path}"
        if not orig_file.is_file():
            return False, f"Original file missing: {expected_orig_path}"
        if not orig_file.resolve().is_relative_to(root_resolved):
            return False, f"Original file escapes library root: {expected_orig_path}"
        if orig_file.stat().st_size != orig_bytes:
            return False, f"Original file byte size mismatch for {expected_orig_path}"
        if sha256_file(orig_file) != w_id:
            return False, f"Original file SHA256 mismatch for {expected_orig_path}"
        expected_files.add(expected_orig_path)

        # 2. Display derivative checks
        disp_meta = item.get("display")
        if not isinstance(disp_meta, dict):
            return False, f"Display metadata missing or invalid for {w_id}"
        expected_disp_path = f"display/{w_id}.webp"
        if disp_meta.get("path") != expected_disp_path:
            return False, f"Display path {disp_meta.get('path')!r} does not match canonical {expected_disp_path!r}"
        disp_sha = disp_meta.get("sha256")
        if not isinstance(disp_sha, str) or not ID_PATTERN.fullmatch(disp_sha):
            return False, f"Invalid display SHA256 for {w_id}"
        disp_bytes = disp_meta.get("bytes")
        if not _is_valid_pos_int(disp_bytes):
            return False, f"Invalid display byte size for {w_id}: {disp_bytes}"
        disp_w = disp_meta.get("width")
        disp_h = disp_meta.get("height")
        if not _is_valid_pos_int(disp_w) or not _is_valid_pos_int(disp_h):
            return False, f"Invalid display dimensions for {w_id}"

        disp_file = library_dir / expected_disp_path
        if disp_file.is_symlink():
            return False, f"Display file is a symlink: {expected_disp_path}"
        if not disp_file.is_file():
            return False, f"Display file missing: {expected_disp_path}"
        if not disp_file.resolve().is_relative_to(root_resolved):
            return False, f"Display file escapes library root: {expected_disp_path}"
        if disp_file.stat().st_size != disp_bytes:
            return False, f"Display file byte size mismatch for {expected_disp_path}"
        if sha256_file(disp_file) != disp_sha:
            return False, f"Display file SHA256 mismatch for {expected_disp_path}"
        expected_files.add(expected_disp_path)

        # 3. Thumbnail derivative checks
        thumb_meta = item.get("thumbnail")
        if not isinstance(thumb_meta, dict):
            return False, f"Thumbnail metadata missing or invalid for {w_id}"
        expected_thumb_path = f"thumbnails/{w_id}.webp"
        if thumb_meta.get("path") != expected_thumb_path:
            return False, f"Thumbnail path {thumb_meta.get('path')!r} does not match canonical {expected_thumb_path!r}"
        thumb_sha = thumb_meta.get("sha256")
        if not isinstance(thumb_sha, str) or not ID_PATTERN.fullmatch(thumb_sha):
            return False, f"Invalid thumbnail SHA256 for {w_id}"
        thumb_bytes = thumb_meta.get("bytes")
        if not _is_valid_pos_int(thumb_bytes):
            return False, f"Invalid thumbnail byte size for {w_id}: {thumb_bytes}"
        thumb_w = thumb_meta.get("width")
        thumb_h = thumb_meta.get("height")
        if not _is_valid_pos_int(thumb_w) or not _is_valid_pos_int(thumb_h):
            return False, f"Invalid thumbnail dimensions for {w_id}"

        thumb_file = library_dir / expected_thumb_path
        if thumb_file.is_symlink():
            return False, f"Thumbnail file is a symlink: {expected_thumb_path}"
        if not thumb_file.is_file():
            return False, f"Thumbnail file missing: {expected_thumb_path}"
        if not thumb_file.resolve().is_relative_to(root_resolved):
            return False, f"Thumbnail file escapes library root: {expected_thumb_path}"
        if thumb_file.stat().st_size != thumb_bytes:
            return False, f"Thumbnail file byte size mismatch for {expected_thumb_path}"
        if sha256_file(thumb_file) != thumb_sha:
            return False, f"Thumbnail file SHA256 mismatch for {expected_thumb_path}"
        expected_files.add(expected_thumb_path)

    if default_id and default_id not in seen_ids:
        return False, f"default_id {default_id} not present in wallpapers"

    # 4. Transport manifest check if present
    tm_file = library_dir / "transport-manifest.json"
    if not tm_file.exists() and not tm_file.is_symlink():
        return False, "Missing transport-manifest.json"
    if tm_file.exists() or tm_file.is_symlink():
        if tm_file.is_symlink():
            return False, f"transport-manifest.json is a symlink"
        if not tm_file.is_file():
            return False, f"transport-manifest.json is not a regular file"
        try:
            with open(tm_file, "rb") as f:
                content = f.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                return False, "transport-manifest.json exceeds size limit"
            tm = json.loads(content)
            if not isinstance(tm, dict) or type(tm.get("version")) is not int or tm["version"] != 1:
                return False, "Invalid transport-manifest structure"
            tm_files = tm.get("files")
            if not isinstance(tm_files, list):
                return False, "transport-manifest files is not a list"
            tm_map = {}
            for t_item in tm_files:
                if not isinstance(t_item, dict) or "path" not in t_item or "sha256" not in t_item or "bytes" not in t_item:
                    return False, f"Invalid transport item: {t_item}"
                if not isinstance(t_item["path"], str) or not isinstance(t_item["sha256"], str) or not ID_PATTERN.fullmatch(t_item["sha256"]) or not _is_valid_pos_int(t_item["bytes"]):
                    return False, "Invalid transport item values"
                if t_item["path"] in tm_map:
                    return False, f"Duplicate entry in transport manifest: {t_item['path']}"
                tm_map[t_item["path"]] = t_item

            # All payload files in expected_files must match transport-manifest
            if set(tm_map.keys()) != expected_files:
                return False, f"Transport manifest files do not match library files: {set(tm_map.keys()) ^ expected_files}"
            for rel_p, t_item in tm_map.items():
                p_disk = library_dir / rel_p
                if p_disk.stat().st_size != t_item["bytes"]:
                    return False, f"Transport byte size mismatch for {rel_p}"
                if sha256_file(p_disk) != t_item["sha256"]:
                    return False, f"Transport hash mismatch for {rel_p}"
            expected_files.add("transport-manifest.json")
        except Exception as e:
            return False, f"Failed to parse transport manifest: {e}"

    # 5. Exhaustive filesystem sweep: no undeclared files, directories, or symlinks
    for root, dirs, files in os.walk(library_dir, followlinks=False):
        rel_root = Path(root).relative_to(library_dir)
        for d in dirs:
            dp = Path(root) / d
            if dp.is_symlink():
                return False, f"Undeclared symlink directory in library: {dp.relative_to(library_dir)}"
            rel_d = str(rel_root / d) if str(rel_root) != "." else d
            if rel_d not in {"originals", "display", "thumbnails"}:
                return False, f"Unexpected directory in library: {rel_d}"
        for fn in files:
            fp = Path(root) / fn
            if fp.is_symlink():
                return False, f"Undeclared symlink in library: {fp.relative_to(library_dir)}"
            rel_f = str(rel_root / fn) if str(rel_root) != "." else fn
            if rel_f not in expected_files:
                return False, f"Unexpected file in library: {rel_f}"

    return True, f"Library verified successfully: {len(wallpapers)} wallpapers intact ({len(expected_files)} files)"


def build_library(
    src_dir: Path = DEFAULT_SRC_DIR,
    dest_dir: Path = DEFAULT_DEST_DIR,
    inventory_path: Optional[Path] = DEFAULT_INVENTORY_PATH,
) -> Dict[str, Any]:
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Source wallpapers directory not found: {src_dir}")
    if dest_dir.is_symlink():
        raise RuntimeError(f"Destination {dest_dir} is a symlink; refusing build.")

    # Check if dest_dir already exists and is complete & verified
    if dest_dir.exists():
        dest_ok, _ = verify_library(dest_dir)
        if dest_ok:
            dest_manifest_file = dest_dir / "manifest.json"
            try:
                with open(dest_manifest_file, "r", encoding="utf-8") as f:
                    dest_m = json.load(f)
                return dest_m
            except Exception:
                pass
        raise RuntimeError(f"Destination {dest_dir} exists with conflicting or invalid data; refusing overwrite.")

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

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    # Unique owned temporary staging directory (never blindly delete predictable existing paths)
    staging_dir_str = tempfile.mkdtemp(prefix="wallpapers-staging-", dir=dest_dir.parent)
    staging_dir = Path(staging_dir_str)

    try:
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
            dest_orig = orig_dir / s_file.name
            shutil.copy2(s_file, dest_orig)
            orig_sha = sha256_file(dest_orig)
            orig_bytes = dest_orig.stat().st_size
            w_id = orig_sha

            if s_file.name == DEFAULT_IMAGE_NAME:
                default_id = w_id

            transport_files.append({
                "path": f"originals/{s_file.name}",
                "sha256": orig_sha,
                "bytes": orig_bytes,
            })

            # Derive pixels strictly from the copied/admitted snapshot in staging
            with Image.open(dest_orig) as raw_im:
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
        transport_files.sort(key=lambda x: x["path"])

        # Accurate count: 79 payload entries including library manifest PLUS transport-manifest.json itself makes 80 actual files
        transport_manifest = {
            "version": 1,
            "payload_entries": len(transport_files),
            "total_actual_files": len(transport_files) + 1,
            "total_payload_bytes": sum(item["bytes"] for item in transport_files),
            "files": transport_files,
            "root_instructions": [
                f"1. Rsync staged library: rsync -avz --checksum -e 'ssh -p 8022' {dest_dir}/ u0_a505@100.75.159.53:wallpapers-staging/",
                "2. Copy reviewed prepare_wallpapers.py tool: scp -P 8022 tools/prepare_wallpapers.py u0_a505@100.75.159.53:prepare_wallpapers.py",
                "3. Verify staging on phone: ssh -p 8022 u0_a505@100.75.159.53 'python3 prepare_wallpapers.py verify ~/wallpapers-staging'",
                "4. Publish on phone: ssh -p 8022 u0_a505@100.75.159.53 'python3 prepare_wallpapers.py publish ~/wallpapers-staging ~/wallpapers'",
            ],
        }

        transport_file = staging_dir / "transport-manifest.json"
        with open(transport_file, "w", encoding="utf-8") as f:
            json.dump(transport_manifest, f, indent=2)

        ok, msg = verify_library(staging_dir)
        if not ok:
            raise RuntimeError(f"Staging verification failed: {msg}")

        staging_dir.rename(dest_dir)
        return manifest
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


def publish_library(staging_dir: Path, target_dir: Path = DEFAULT_PHONE_DEST) -> Tuple[bool, str]:
    if staging_dir.is_symlink():
        return False, f"Staging directory {staging_dir} is a symlink"
    if not staging_dir.is_dir():
        return False, f"Staging directory {staging_dir} does not exist"

    ok, msg = verify_library(staging_dir)
    if not ok:
        return False, f"Staging library verification failed: {msg}"

    with open(staging_dir / "manifest.json", "r", encoding="utf-8") as f:
        staging_manifest = json.load(f)

    if target_dir.is_symlink():
        return False, f"Target directory {target_dir} is a symlink; refusing publication."

    if target_dir.exists():
        target_ok, _ = verify_library(target_dir)
        if target_ok:
            try:
                with open(target_dir / "manifest.json", "r", encoding="utf-8") as f:
                    target_manifest = json.load(f)
                if target_manifest == staging_manifest:
                    return True, f"Target directory {target_dir} is already up to date"
            except Exception:
                pass
        return False, f"Refusing to overwrite conflicting target directory: {target_dir}"

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    candidate_str = tempfile.mkdtemp(prefix=".wallpapers-pub-", dir=target_dir.parent)
    candidate_dir = Path(candidate_str)

    try:
        # Safe copy: never follow or duplicate symlinks
        for root, dirs, files in os.walk(staging_dir, followlinks=False):
            rel_root = Path(root).relative_to(staging_dir)
            for d in dirs:
                sd = Path(root) / d
                if sd.is_symlink():
                    return False, f"Symlink directory rejected during publication: {sd}"
                (candidate_dir / rel_root / d).mkdir(parents=True, exist_ok=True)
            for fn in files:
                sf = Path(root) / fn
                if sf.is_symlink():
                    return False, f"Symlink file rejected during publication: {sf}"
                dest_f = candidate_dir / rel_root / fn
                shutil.copy2(sf, dest_f)

        candidate_ok, candidate_msg = verify_library(candidate_dir)
        if not candidate_ok:
            return False, f"Copied library candidate verification failed: {candidate_msg}"

        candidate_dir.rename(target_dir)
    except Exception as e:
        return False, f"Failed to publish library: {e}"
    finally:
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)

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
