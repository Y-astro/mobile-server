"""Wallpaper library reader and HTTP request dispatcher.

Provides manifest validation, public wallpaper catalog listings,
and bounded WebP derivative delivery with strict auth and traversal guards.
"""
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional, Set, Tuple

ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_DERIVATIVES = {"thumbnail.webp", "display.webp"}
DERIVATIVE_DIRS = {
    "thumbnail.webp": "thumbnails",
    "display.webp": "display",
}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_DERIVATIVE_BYTES = 20 * 1024 * 1024  # 20 MiB


class WallpaperError(Exception):
    """Base error for wallpaper operations."""
    pass


class CorruptLibraryError(WallpaperError):
    """Raised when manifest or derivative files are missing, corrupt, or invalid."""
    pass


class WallpaperNotFoundError(WallpaperError):
    """Raised when an unknown wallpaper or invalid path is requested."""
    pass


def _is_valid_pos_int(val: Any) -> bool:
    return type(val) is int and not isinstance(val, bool) and val > 0


class WallpaperLibrary:
    def __init__(self, root_dir: Optional[Path] = None):
        self._custom_root_dir = Path(root_dir) if root_dir is not None else None
        # Cache key binds to canonical root + device + inode + ctime_ns + mtime_ns + size
        self._cached_stat: Optional[Tuple[str, int, int, int, int, int]] = None
        self._cached_manifest: Optional[Dict[str, Any]] = None
        self._cached_public_list: Optional[Dict[str, Any]] = None

    @property
    def root_dir(self) -> Path:
        if self._custom_root_dir is not None:
            return self._custom_root_dir
        return Path(os.path.expanduser("~/wallpapers"))

    def _read_and_validate_manifest(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if self.root_dir.is_symlink() or not self.root_dir.is_dir():
            empty_payload = {"version": 1, "default_id": None, "wallpapers": []}
            return {}, empty_payload

        resolved_root = self.root_dir.resolve()
        manifest_file = self.root_dir / "manifest.json"
        if manifest_file.is_symlink() or not manifest_file.is_file():
            empty_payload = {"version": 1, "default_id": None, "wallpapers": []}
            return {}, empty_payload

        try:
            st = manifest_file.stat()
            if st.st_size > MAX_MANIFEST_BYTES or st.st_size == 0:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            current_stat = (str(resolved_root), st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)
            if self._cached_stat == current_stat and self._cached_manifest is not None and self._cached_public_list is not None:
                return self._cached_manifest, self._cached_public_list
        except OSError:
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        # Required subdirectories cannot be symlinks
        for subdir in ("display", "thumbnails"):
            sub_p = self.root_dir / subdir
            if sub_p.is_symlink() or not sub_p.is_dir() or not sub_p.resolve().is_relative_to(resolved_root):
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                raw_manifest = json.load(f)
        except Exception:
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        if not isinstance(raw_manifest, dict):
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
        if raw_manifest.get("version") != 1:
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        wallpapers_data = raw_manifest.get("wallpapers")
        if not isinstance(wallpapers_data, list):
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        public_wallpapers = []
        validated_manifest = {"version": 1, "default_id": raw_manifest.get("default_id"), "wallpapers": {}}
        seen_ids: Set[str] = set()
        seen_names: Set[str] = set()

        for entry in wallpapers_data:
            if not isinstance(entry, dict):
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            w_id = entry.get("id")
            w_name = entry.get("name")
            w_width = entry.get("width")
            w_height = entry.get("height")

            if not isinstance(w_id, str) or not ID_PATTERN.fullmatch(w_id):
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            if w_id in seen_ids:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            seen_ids.add(w_id)

            if not isinstance(w_name, str) or not w_name.strip() or "/" in w_name or "\\" in w_name or ".." in w_name:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            if w_name in seen_names:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
            seen_names.add(w_name)

            if not _is_valid_pos_int(w_width) or not _is_valid_pos_int(w_height):
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

            for deriv in ("display", "thumbnail"):
                deriv_meta = entry.get(deriv)
                if not isinstance(deriv_meta, dict):
                    raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                sub_name = "display" if deriv == "display" else "thumbnails"
                expected_rel = f"{sub_name}/{w_id}.webp"
                if deriv_meta.get("path") != expected_rel:
                    raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                d_sha = deriv_meta.get("sha256")
                if not isinstance(d_sha, str) or not ID_PATTERN.fullmatch(d_sha):
                    raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                d_bytes = deriv_meta.get("bytes")
                if not _is_valid_pos_int(d_bytes) or d_bytes > MAX_DERIVATIVE_BYTES:
                    raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

                file_path = self.root_dir / expected_rel
                try:
                    if file_path.is_symlink() or not file_path.is_file():
                        raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                    if not file_path.resolve().is_relative_to(resolved_root):
                        raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                    if file_path.stat().st_size != d_bytes:
                        raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
                except OSError:
                    raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

            public_wallpapers.append({
                "id": w_id,
                "name": w_name,
                "width": w_width,
                "height": w_height,
                "thumbnail_url": f"/api/wallpapers/{w_id}/thumbnail.webp",
                "image_url": f"/api/wallpapers/{w_id}/display.webp",
            })
            validated_manifest["wallpapers"][w_id] = entry

        default_id = raw_manifest.get("default_id")
        if default_id is not None:
            if not isinstance(default_id, str) or default_id not in seen_ids:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        public_list = {
            "version": 1,
            "default_id": default_id,
            "wallpapers": public_wallpapers,
        }

        self._cached_stat = current_stat
        self._cached_manifest = validated_manifest
        self._cached_public_list = public_list
        return validated_manifest, public_list

    def get_public_list(self) -> Dict[str, Any]:
        _, public_list = self._read_and_validate_manifest()
        return public_list

    def get_derivative(self, wallpaper_id: str, derivative_name: str) -> Tuple[bytes, str]:
        if not isinstance(wallpaper_id, str) or not ID_PATTERN.fullmatch(wallpaper_id):
            raise WallpaperNotFoundError("Unknown wallpaper ID")
        if derivative_name not in ALLOWED_DERIVATIVES:
            raise WallpaperNotFoundError("Unknown derivative requested")

        if self.root_dir.is_symlink() or not self.root_dir.is_dir():
            raise WallpaperNotFoundError("Wallpaper not found")

        manifest_file = self.root_dir / "manifest.json"
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise WallpaperNotFoundError("Wallpaper not found")

        validated_manifest, _ = self._read_and_validate_manifest()
        entry = validated_manifest.get("wallpapers", {}).get(wallpaper_id)
        if entry is None:
            raise WallpaperNotFoundError("Wallpaper not found")

        deriv_key = "thumbnail" if derivative_name == "thumbnail.webp" else "display"
        deriv_meta = entry.get(deriv_key, {})
        expected_bytes = deriv_meta.get("bytes")
        if not _is_valid_pos_int(expected_bytes) or expected_bytes > MAX_DERIVATIVE_BYTES:
            raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

        sub_dir = DERIVATIVE_DIRS[derivative_name]
        target_file = self.root_dir / sub_dir / f"{wallpaper_id}.webp"

        try:
            if target_file.is_symlink() or not target_file.is_file():
                raise WallpaperNotFoundError("Wallpaper file missing")
            if not target_file.resolve().is_relative_to(self.root_dir.resolve()):
                raise WallpaperNotFoundError("Wallpaper path escape")
            if target_file.stat().st_size != expected_bytes:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")

            # Bounded read
            with open(target_file, "rb") as f:
                content = f.read(expected_bytes + 1)
            if len(content) != expected_bytes:
                raise CorruptLibraryError("Wallpaper library is corrupt or incomplete")
        except OSError:
            raise WallpaperNotFoundError("Wallpaper file read failure")

        return content, "image/webp"


DEFAULT_LIBRARY = WallpaperLibrary()


def dispatch_wallpapers(handler, user: Optional[str] = None, library: Optional[WallpaperLibrary] = None) -> None:
    if user is None:
        user = handler.get_session_user()
    if not user:
        handler.json_error(401, "Authentication required")
        return

    lib = library if library is not None else DEFAULT_LIBRARY
    raw_path = handler.path.split("?", 1)[0]

    if raw_path in ("/api/wallpapers", "/api/wallpapers/"):
        try:
            data = lib.get_public_list()
        except CorruptLibraryError:
            handler.json_error(503, "Wallpaper library is corrupt or incomplete")
            return

        body = json.dumps(data).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "private, no-store")
        handler.end_headers()
        if getattr(handler, "command", "GET") != "HEAD":
            handler.wfile.write(body)
        return

    if raw_path.startswith("/api/wallpapers/"):
        remainder = raw_path[len("/api/wallpapers/"):]
        parts = remainder.split("/")
        if len(parts) != 2:
            handler.json_error(404, "Unknown resource")
            return

        wallpaper_id, derivative_name = parts[0], parts[1]
        try:
            content, mime_type = lib.get_derivative(wallpaper_id, derivative_name)
        except WallpaperNotFoundError:
            handler.json_error(404, "Wallpaper not found")
            return
        except CorruptLibraryError:
            handler.json_error(503, "Wallpaper library is corrupt or incomplete")
            return

        handler.send_response(200)
        handler.send_header("Content-Type", mime_type)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "private, no-store")
        handler.end_headers()
        if getattr(handler, "command", "GET") != "HEAD":
            handler.wfile.write(content)
        return

    handler.json_error(404, "Unknown resource")
