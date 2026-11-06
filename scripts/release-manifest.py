#!/usr/bin/env python3
"""
release-manifest.py: Deterministic release manifest generation and offline validation.

Provides machine-generated SHA-256 and byte sizes for maintained source files and
declared built gateway artifact. Derives canonical source-to-destination ownership
from audited config/deployment-files.json and safe dynamic app/*.py helpers. Enforces
strict path resolution, parent symlink escape rejection, exact destination matching,
and non-zero failure on missing, tampered, unowned, or forbidden secret/runtime state files.

Usage:
  python3 scripts/release-manifest.py generate [--root DIR] [--output FILE] [--include-gateway]
  python3 scripts/release-manifest.py validate --manifest FILE [--root DIR] [--require-gateway]
"""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Authorized top-level source directories
ALLOWED_TOP_DIRS: Tuple[str, ...] = ("app", "web", "scripts", "config", ".work")

# Authorized remote destination prefix
ALLOWED_REMOTE_PREFIX = "/data/data/com.termux/files/"

# Frozen audited 35-row baseline metadata checksum (SHA-256 of config/deployment-files.json bytes)
AUDITED_BASELINE_SHA256 = "85282ec24628491bdc25975dc4767d81f54d02e368cf5d652dc3665c47fa3cd1"

# Built gateway artifact constants
DEFAULT_GATEWAY_LOCAL = ".work/mobile-gateway"
DEFAULT_GATEWAY_REMOTE = "/data/data/com.termux/files/home/bin/mobile-gateway"

# Built Suwayomi JAR artifact constants
DEFAULT_SUWAYOMI_JAR_LOCAL = ".work/suwayomi-parallel.jar"
DEFAULT_SUWAYOMI_JAR_REMOTE = "/data/data/com.termux/files/home/suwayomi/bin/Suwayomi-Server.jar"

# Mobile server python helper lib destination
MOBILE_SERVER_LIB_PREFIX = "/data/data/com.termux/files/home/.local/lib/mobile-server/"

# Reviewed additional maintained source assets outside frozen historical baseline
REVIEWED_ADDITIONAL_ASSETS: Dict[str, str] = {
    "web/portal-polling.js": "/data/data/com.termux/files/home/.config/dashboard/static/portal-polling.js",
    "web/theme-tokens.css": "/data/data/com.termux/files/home/.config/dashboard/static/theme-tokens.css",
    "web/appearance.css": "/data/data/com.termux/files/home/.config/dashboard/static/appearance.css",
    "web/appearance.js": "/data/data/com.termux/files/home/.config/dashboard/static/appearance.js",
    "web/filebrowser.css": "/data/data/com.termux/files/home/.config/dashboard/static/filebrowser.css",
    "web/filebrowser-entry.js": "/data/data/com.termux/files/home/.config/dashboard/static/filebrowser-entry.js",
    "web/antra.html": "/data/data/com.termux/files/home/.config/dashboard/static/antra.html",
    "web/antra.css": "/data/data/com.termux/files/home/.config/dashboard/static/antra.css",
    "web/antra.js": "/data/data/com.termux/files/home/.config/dashboard/static/antra.js",
}

# Forbidden secret, runtime, credential, database, and history patterns.
FORBIDDEN_SECRET_PATTERNS: Tuple[str, ...] = (
    "users.json",
    "*users.json*",
    "*.env",
    "*.env.*",
    ".env",
    "*.key",
    "*.pem",
    "*.crt",
    "*.cert",
    "*.pfx",
    "*.p12",
    "*id_rsa*",
    "*id_ed25519*",
    "*session.key*",
    "*authorized_keys*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*filebrowser.db*",
    "*.log",
    "*history*",
    "*.bak",
    "*.swp",
    "*services_disabled*",
    "*.phone-server-paused*",
    "*server_recovery*",
)


class ManifestError(Exception):
    """Base exception for manifest validation and generation errors."""
    pass


def is_forbidden_secret_or_runtime_path(path_str: str) -> bool:
    """Check whether a path matches forbidden secret, runtime, DB, key, or credential patterns."""
    normalized = path_str.replace("\\", "/").lower()
    # This public stylesheet defines design tokens, not authentication tokens.
    if path_str in (
        "web/theme-tokens.css",
        "/data/data/com.termux/files/home/.config/dashboard/static/theme-tokens.css",
    ):
        return False
    basename = os.path.basename(normalized)
    parts = normalized.split("/")

    for pattern in FORBIDDEN_SECRET_PATTERNS:
        pat_lower = pattern.lower()
        if fnmatch.fnmatch(basename, pat_lower) or fnmatch.fnmatch(normalized, pat_lower):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat_lower):
                return True

    for keyword in ("token", "credential", "private_key", "secret_key", "authorized_keys"):
        if keyword in basename or keyword in normalized:
            return True

    return False


def load_baseline_mapping(root_dir: Path) -> Dict[str, str]:
    """
    Load canonical ownership mapping from config/deployment-files.json.
    Verifies that the baseline file resolves strictly within root_dir and matches
    the immutable audited SHA-256 checksum before parsing. Fails closed on
    unreadable baseline, checksum mismatch, invalid shape, or duplicate ownership.
    Deliberate future baseline changes require an independent audit recapture
    and updated pin.
    """
    baseline_file = root_dir / "config/deployment-files.json"
    root_resolved = root_dir.resolve()

    # Symlink escape guard before reading baseline file
    try:
        resolved_baseline = baseline_file.resolve()
        if not resolved_baseline.is_relative_to(root_resolved):
            raise ManifestError(
                f"Refused baseline symlink escape: '{baseline_file}' points outside release root "
                f"to {resolved_baseline}. Reviewed audit recapture is required."
            )
    except (ValueError, RuntimeError) as exc:
        raise ManifestError(
            f"Refused invalid baseline symlink '{baseline_file}': {exc}. "
            "Reviewed audit recapture is required."
        ) from exc

    if not baseline_file.exists():
        raise ManifestError(
            f"Audited baseline mapping file not found at {baseline_file}. "
            "Reviewed audit recapture is required."
        )

    # Read raw bytes and verify immutable audited checksum before parsing
    try:
        baseline_bytes = baseline_file.read_bytes()
    except Exception as exc:
        raise ManifestError(
            f"Failed to read baseline mapping {baseline_file}: {exc}. "
            "Reviewed audit recapture is required."
        ) from exc

    computed_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    if computed_sha256 != AUDITED_BASELINE_SHA256:
        raise ManifestError(
            f"Audited baseline mapping checksum mismatch: expected {AUDITED_BASELINE_SHA256}, "
            f"got {computed_sha256}. Historical baseline config/deployment-files.json is immutable. "
            "Reviewed audit recapture is required to update audited baseline."
        )

    try:
        data = json.loads(baseline_bytes.decode("utf-8"))
    except Exception as exc:
        raise ManifestError(
            f"Failed to parse baseline mapping {baseline_file}: {exc}. "
            "Reviewed audit recapture is required."
        ) from exc

    if not isinstance(data, list):
        raise ManifestError(
            f"Baseline mapping in {baseline_file} must be a JSON array. "
            "Reviewed audit recapture is required."
        )

    mapping: Dict[str, str] = {}
    seen_remotes: Dict[str, str] = {}

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ManifestError(
                f"Baseline item at index {idx} is not an object. "
                "Reviewed audit recapture is required."
            )
        for key in ("local", "remote"):
            if key not in item or not isinstance(item[key], str) or not item[key].strip():
                raise ManifestError(
                    f"Baseline item at index {idx} missing valid '{key}' field. "
                    "Reviewed audit recapture is required."
                )

        local_path = item["local"].strip()
        remote_path = item["remote"].strip()

        if local_path in mapping:
            raise ManifestError(
                f"Duplicate local entry in baseline mapping: {local_path}. "
                "Reviewed audit recapture is required."
            )
        if remote_path in seen_remotes:
            raise ManifestError(
                f"Duplicate remote destination '{remote_path}' in baseline for '{seen_remotes[remote_path]}' and '{local_path}'. "
                "Reviewed audit recapture is required."
            )

        mapping[local_path] = remote_path
        seen_remotes[remote_path] = local_path

    return mapping


def get_canonical_ownership(
    root_dir: Path,
    include_gateway: bool = False,
    include_suwayomi_jar: bool = False,
) -> Dict[str, str]:
    """
    Construct canonical ownership table:
    1. Audited baseline rows from config/deployment-files.json
    2. Safe dynamically discovered app/*.py helpers (excluding dashboard executable)
    3. Optional declared built gateway (.work/mobile-gateway)
    4. Optional declared built Suwayomi JAR (.work/suwayomi-parallel.jar)
    """
    ownership = load_baseline_mapping(root_dir)
    ownership.update(REVIEWED_ADDITIONAL_ASSETS)

    # Dynamically discover any additional safe Python runtime helpers in app/
    app_dir = root_dir / "app"
    if app_dir.is_dir():
        # Check for nested subdirectories in app/ (ignoring __pycache__) - reject unsupported nesting explicitly
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            rel_root = Path(root).relative_to(root_dir)
            if rel_root != Path("app") and (dirs or files):
                raise ManifestError(
                    f"Unsupported nested directory or modules in app/: {rel_root.as_posix()}. "
                    "Only flat app/*.py runtime modules are supported."
                )

        for py_path in sorted(app_dir.glob("*.py")):
            rel_path = py_path.relative_to(root_dir).as_posix()
            if rel_path not in ownership:
                if py_path.name == "dashboard.py":
                    ownership[rel_path] = "/data/data/com.termux/files/usr/bin/dashboard"
                else:
                    ownership[rel_path] = f"{MOBILE_SERVER_LIB_PREFIX}{py_path.name}"

    if include_gateway:
        gw_path = root_dir / DEFAULT_GATEWAY_LOCAL
        if gw_path.exists():
            ownership[DEFAULT_GATEWAY_LOCAL] = DEFAULT_GATEWAY_REMOTE

    if include_suwayomi_jar:
        jar_path = root_dir / DEFAULT_SUWAYOMI_JAR_LOCAL
        if jar_path.exists():
            ownership[DEFAULT_SUWAYOMI_JAR_LOCAL] = DEFAULT_SUWAYOMI_JAR_REMOTE

    return ownership


def validate_path_safety(local_str: str, remote_str: str, root_dir: Path) -> None:
    """
    Validate path safety, component structure, symlink safety (including all parents),
    and secret/runtime protection.
    """
    if not local_str or not isinstance(local_str, str):
        raise ManifestError("Local source path must be a non-empty string")

    # Reject backslashes and normalize
    if "\\" in local_str:
        raise ManifestError(f"Refused non-posix local path: {local_str}")
    norm_local = os.path.normpath(local_str)
    if os.path.isabs(local_str):
        raise ManifestError(f"Refused absolute source path: {local_str}")

    local_path = Path(norm_local)
    parts = local_path.parts

    if not parts or parts[0] == ".." or ".." in parts:
        raise ManifestError(f"Refused traversal in source path: {local_str}")

    # Check first path component exactly matches allowed directory
    if parts[0] not in ALLOWED_TOP_DIRS:
        raise ManifestError(
            f"Refused source path '{local_str}': component '{parts[0]}' is not inside allowed source roots "
            f"({', '.join(ALLOWED_TOP_DIRS)})"
        )

    if is_forbidden_secret_or_runtime_path(local_str):
        raise ManifestError(f"Refused secret or runtime class in source path: {local_str}")

    # Validate all parent paths and the file itself for symlink traversal escaping root
    root_resolved = root_dir.resolve()
    current = root_dir
    for part in parts:
        current = current / part
        if current.is_symlink():
            try:
                target = current.resolve()
                if not target.is_relative_to(root_resolved):
                    raise ManifestError(
                        f"Refused symlink escape in path '{local_str}': component '{current.name}' "
                        f"points outside release root to {target}"
                    )
                rel_target_str = target.relative_to(root_resolved).as_posix()
                if is_forbidden_secret_or_runtime_path(rel_target_str):
                    raise ManifestError(
                        f"Refused symlink pointing to forbidden secret: {local_str} -> {target}"
                    )
            except (ValueError, RuntimeError) as exc:
                raise ManifestError(f"Refused invalid symlink in path '{local_str}': {exc}") from exc

    # Final resolved target check
    if current.exists() or current.is_symlink():
        try:
            target = current.resolve()
            if not target.is_relative_to(root_resolved):
                raise ManifestError(f"Refused symlink escape: '{local_str}' points outside release root ({target})")
            rel_target_str = target.relative_to(root_resolved).as_posix()
            if is_forbidden_secret_or_runtime_path(rel_target_str):
                raise ManifestError(f"Refused symlink pointing to forbidden secret: {local_str} -> {target}")
        except (ValueError, RuntimeError) as exc:
            raise ManifestError(f"Refused invalid symlink: '{local_str}': {exc}") from exc

    # Remote destination checks
    if not remote_str or not isinstance(remote_str, str):
        raise ManifestError(f"Remote destination path for '{local_str}' must be a non-empty string")
    if not os.path.isabs(remote_str):
        raise ManifestError(f"Refused relative remote destination: {remote_str}")

    norm_remote = os.path.normpath(remote_str)
    remote_parts = Path(norm_remote).parts
    if ".." in remote_parts:
        raise ManifestError(f"Refused path traversal in remote destination: {remote_str}")

    if not norm_remote.startswith(ALLOWED_REMOTE_PREFIX):
        raise ManifestError(
            f"Refused remote destination: {remote_str} is outside permitted prefix {ALLOWED_REMOTE_PREFIX}"
        )

    if is_forbidden_secret_or_runtime_path(remote_str):
        raise ManifestError(f"Refused secret or runtime class in destination path: {remote_str}")


def compute_sha256_and_size(file_path: Path) -> Tuple[str, int]:
    """Compute deterministic SHA-256 and byte count for a file."""
    if not file_path.exists():
        raise ManifestError(f"Source file does not exist: {file_path}")
    if not file_path.is_file():
        raise ManifestError(f"Source path is not a regular file: {file_path}")

    hasher = hashlib.sha256()
    size = 0
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)

    return hasher.hexdigest(), size


def generate_manifest(
    root_dir: Path,
    include_gateway: bool = False,
    require_gateway: bool = False,
    include_suwayomi_jar: bool = False,
    require_suwayomi_jar: bool = False,
) -> List[Dict[str, Any]]:
    """
    Generate candidate release manifest from source tree based on canonical ownership.
    """
    canonical_ownership = get_canonical_ownership(
        root_dir,
        include_gateway=include_gateway,
        include_suwayomi_jar=include_suwayomi_jar,
    )

    if require_gateway:
        gw_path = root_dir / DEFAULT_GATEWAY_LOCAL
        if not gw_path.exists():
            raise ManifestError(
                f"Required Go built artifact not found at {gw_path}. "
                "Build gateway first: cd gateway && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -o ../.work/mobile-gateway ."
            )
        canonical_ownership[DEFAULT_GATEWAY_LOCAL] = DEFAULT_GATEWAY_REMOTE

    if require_suwayomi_jar:
        jar_path = root_dir / DEFAULT_SUWAYOMI_JAR_LOCAL
        if not jar_path.exists():
            raise ManifestError(
                f"Required Suwayomi JAR built artifact not found at {jar_path}. "
                f"Build or place JAR first at {DEFAULT_SUWAYOMI_JAR_LOCAL}"
            )
        canonical_ownership[DEFAULT_SUWAYOMI_JAR_LOCAL] = DEFAULT_SUWAYOMI_JAR_REMOTE

    seen_remotes: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []

    for local_str in sorted(canonical_ownership.keys()):
        remote_str = canonical_ownership[local_str]
        validate_path_safety(local_str, remote_str, root_dir)

        if remote_str in seen_remotes:
            raise ManifestError(
                f"Duplicate remote destination '{remote_str}' mapped from both '{seen_remotes[remote_str]}' and '{local_str}'"
            )
        seen_remotes[remote_str] = local_str

        full_path = root_dir / local_str
        if not full_path.exists():
            raise ManifestError(f"Missing required release source on disk: {local_str} (at {full_path})")

        sha256_hex, file_bytes = compute_sha256_and_size(full_path)

        entries.append({
            "local": local_str,
            "remote": remote_str,
            "sha256": sha256_hex,
            "bytes": file_bytes,
        })

    entries.sort(key=lambda x: x["local"])
    return entries


def validate_manifest(
    manifest_entries: List[Dict[str, Any]],
    root_dir: Path,
    require_gateway: bool = False,
    require_suwayomi_jar: bool = False,
) -> None:
    """
    Validate candidate release manifest entries against canonical ownership and bundle root.
    Candidate destinations must exactly match canonical ownership.
    """
    if not isinstance(manifest_entries, list):
        raise ManifestError("Manifest root must be a list of file entries")

    # Load canonical allowed ownership
    canonical_ownership = get_canonical_ownership(
        root_dir,
        include_gateway=True,
        include_suwayomi_jar=True,
    )

    manifest_locals: Set[str] = set()
    seen_remotes: Dict[str, str] = {}

    for idx, entry in enumerate(manifest_entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"Manifest entry at index {idx} is not an object: {entry}")

        for key in ("local", "remote", "sha256", "bytes"):
            if key not in entry:
                raise ManifestError(f"Manifest entry at index {idx} missing required key '{key}': {entry}")

        local_str = entry["local"]
        remote_str = entry["remote"]
        raw_sha256 = entry["sha256"]
        expected_bytes = entry["bytes"]

        if not isinstance(raw_sha256, str):
            raise ManifestError(
                f"Invalid SHA-256 hash type {type(raw_sha256).__name__} for {local_str}: hash must be a string"
            )

        expected_sha256 = raw_sha256.lower()
        if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
            raise ManifestError(
                f"Invalid SHA-256 hash format for {local_str}: must be a 64-character hex string"
            )

        # Reject bool (since bool is a subclass of int in Python) and non-ints
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ManifestError(f"Invalid byte size {expected_bytes} for {local_str}")

        validate_path_safety(local_str, remote_str, root_dir)

        # Enforce exact canonical ownership match
        if local_str not in canonical_ownership:
            raise ManifestError(f"Unowned source file in candidate manifest: '{local_str}'")

        allowed_remote = canonical_ownership[local_str]
        if remote_str != allowed_remote:
            raise ManifestError(
                f"Destination substitution rejected for '{local_str}': candidate specifies '{remote_str}', "
                f"but allowed canonical destination is '{allowed_remote}'"
            )

        if remote_str in seen_remotes:
            raise ManifestError(
                f"Duplicate remote destination '{remote_str}' declared for '{seen_remotes[remote_str]}' and '{local_str}'"
            )
        seen_remotes[remote_str] = local_str

        if local_str in manifest_locals:
            raise ManifestError(f"Duplicate local entry in manifest: '{local_str}'")
        manifest_locals.add(local_str)

        full_path = root_dir / local_str
        if not full_path.exists():
            raise ManifestError(f"Missing release source file: {local_str} (expected at {full_path})")

        actual_sha256, actual_bytes = compute_sha256_and_size(full_path)

        if actual_bytes != expected_bytes:
            raise ManifestError(
                f"Byte size mismatch for {local_str}: manifest expects {expected_bytes} bytes, "
                f"actual on-disk size is {actual_bytes} bytes"
            )

        if actual_sha256.lower() != expected_sha256:
            raise ManifestError(
                f"SHA-256 hash mismatch for {local_str}: manifest expects {expected_sha256}, "
                f"actual on-disk hash is {actual_sha256}"
            )

    # Verify all baseline sources are declared in candidate manifest
    baseline_ownership = load_baseline_mapping(root_dir)
    missing_baseline = set(baseline_ownership.keys()) - manifest_locals
    if missing_baseline:
        raise ManifestError(
            f"Release manifest omits required baseline assets: {', '.join(sorted(missing_baseline))}"
        )

    # Verify all reviewed additional maintained source assets are declared in candidate manifest
    missing_additional = set(REVIEWED_ADDITIONAL_ASSETS.keys()) - manifest_locals
    if missing_additional:
        raise ManifestError(
            f"Release manifest omits required maintained assets: {', '.join(sorted(missing_additional))}"
        )

    # Verify all app/*.py files are declared in candidate manifest
    app_dir = root_dir / "app"
    if app_dir.is_dir():
        for py_path in sorted(app_dir.glob("*.py")):
            rel_path = py_path.relative_to(root_dir).as_posix()
            if rel_path not in manifest_locals:
                raise ManifestError(
                    f"Newly added Python runtime module '{rel_path}' is not declared in release manifest. "
                    "All runtime app modules must be explicitly mapped in the release."
                )

    if require_gateway and DEFAULT_GATEWAY_LOCAL not in manifest_locals:
        raise ManifestError(
            f"Required Go built artifact '{DEFAULT_GATEWAY_LOCAL}' is not declared in release manifest"
        )

    if require_suwayomi_jar and DEFAULT_SUWAYOMI_JAR_LOCAL not in manifest_locals:
        raise ManifestError(
            f"Required Suwayomi JAR built artifact '{DEFAULT_SUWAYOMI_JAR_LOCAL}' is not declared in release manifest"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic release manifest generator and offline validator"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    gen_parser = subparsers.add_parser("generate", help="Generate deterministic release manifest")
    gen_parser.add_argument("--root", default=".", help="Root directory of release source (default: .)")
    gen_parser.add_argument("--output", "-o", default=None, help="Output JSON path (default: stdout)")
    gen_parser.add_argument(
        "--include-gateway",
        action="store_true",
        help=f"Include {DEFAULT_GATEWAY_LOCAL} if present on disk",
    )
    gen_parser.add_argument(
        "--require-gateway",
        action="store_true",
        help=f"Require {DEFAULT_GATEWAY_LOCAL} to be built and included",
    )
    gen_parser.add_argument(
        "--include-suwayomi-jar",
        "--include-jar",
        dest="include_suwayomi_jar",
        action="store_true",
        help=f"Include {DEFAULT_SUWAYOMI_JAR_LOCAL} if present on disk",
    )
    gen_parser.add_argument(
        "--require-suwayomi-jar",
        "--require-jar",
        dest="require_suwayomi_jar",
        action="store_true",
        help=f"Require {DEFAULT_SUWAYOMI_JAR_LOCAL} to be built/present and included",
    )

    val_parser = subparsers.add_parser("validate", help="Validate candidate release manifest")
    val_parser.add_argument("--manifest", "-m", required=True, help="Path to manifest JSON file")
    val_parser.add_argument("--root", default=".", help="Root directory of candidate bundle (default: .)")
    val_parser.add_argument(
        "--require-gateway",
        action="store_true",
        help=f"Require {DEFAULT_GATEWAY_LOCAL} to be present in manifest and on disk",
    )
    val_parser.add_argument(
        "--require-suwayomi-jar",
        "--require-jar",
        dest="require_suwayomi_jar",
        action="store_true",
        help=f"Require {DEFAULT_SUWAYOMI_JAR_LOCAL} to be present in manifest and on disk",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    root_dir = Path(args.root).resolve()

    try:
        if args.command == "generate":
            entries = generate_manifest(
                root_dir=root_dir,
                include_gateway=args.include_gateway,
                require_gateway=args.require_gateway,
                include_suwayomi_jar=args.include_suwayomi_jar,
                require_suwayomi_jar=args.require_suwayomi_jar,
            )

            formatted_json = json.dumps(entries, indent=2) + "\n"

            if args.output:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(formatted_json)
            else:
                sys.stdout.write(formatted_json)

            return 0

        elif args.command == "validate":
            manifest_path = Path(args.manifest)
            if not manifest_path.exists():
                raise ManifestError(f"Manifest file not found: {manifest_path}")

            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict) and "files" in data:
                entries = data["files"]
            elif isinstance(data, list):
                entries = data
            else:
                raise ManifestError("Manifest file must be a JSON array or object with 'files' list")

            validate_manifest(
                manifest_entries=entries,
                root_dir=root_dir,
                require_gateway=args.require_gateway,
                require_suwayomi_jar=args.require_suwayomi_jar,
            )
            print(f"Release manifest validation PASSED: {len(entries)} verified entries.")
            return 0

    except (ManifestError, json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
