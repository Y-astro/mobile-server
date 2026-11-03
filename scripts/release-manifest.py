#!/usr/bin/env python3
"""
release-manifest.py: Deterministic release manifest generation and offline validation.

Provides machine-generated SHA-256 and byte sizes for maintained source files and
declared build artifacts. Enforces source-to-destination ownership, completeness
for required role templates, helper assets, launchers, and dynamically discovered
Python runtime modules, and strictly refuses path traversal, symlink escapes,
duplicate destinations, and secret/runtime state files.

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

# Canonical mapping of maintained repository sources to remote deployment destinations.
CANONICAL_MAPPINGS: Dict[str, str] = {
    # app/ - Python runtime modules
    "app/auth.py": "/data/data/com.termux/files/home/.local/lib/mobile-server/auth.py",
    "app/dashboard.py": "/data/data/com.termux/files/usr/bin/dashboard",
    "app/laptop.py": "/data/data/com.termux/files/home/.local/lib/mobile-server/laptop.py",
    "app/limits.py": "/data/data/com.termux/files/home/.local/lib/mobile-server/limits.py",
    "app/telemetry.py": "/data/data/com.termux/files/home/.local/lib/mobile-server/telemetry.py",
    "app/tor_proxy.py": "/data/data/com.termux/files/home/.local/lib/mobile-server/tor_proxy.py",

    # web/ - Served pages, stylesheets, and vendor bundles
    "web/admin.html": "/data/data/com.termux/files/home/.config/dashboard/static/admin.html",
    "web/agy_index.html": "/data/data/com.termux/files/home/.config/ttyd/agy_index.html",
    "web/family.html": "/data/data/com.termux/files/home/.config/dashboard/static/family.html",
    "web/filebrowser.js": "/data/data/com.termux/files/home/.config/dashboard/static/patched_index.js",
    "web/interactive_player.js": "/data/data/com.termux/files/home/.config/dashboard/static/interactive_player.js",
    "web/laptop.js": "/data/data/com.termux/files/home/.config/dashboard/static/laptop.js",
    "web/login.html": "/data/data/com.termux/files/home/.config/dashboard/static/login.html",
    "web/portal.css": "/data/data/com.termux/files/home/.config/dashboard/static/portal.css",
    "web/termux_index.html": "/data/data/com.termux/files/home/.config/ttyd/termux_index.html",

    # scripts/ - Process lifecycle, supervisor, launchers, and management tools
    "scripts/agy-resume": "/data/data/com.termux/files/usr/bin/agy-resume",
    "scripts/agy-runner-loop": "/data/data/com.termux/files/usr/var/lib/proot-distro/containers/debian/rootfs/usr/local/bin/agy-runner-loop",
    "scripts/agy_session.sh": "/data/data/com.termux/files/home/bin/agy_session.sh",
    "scripts/battery_guard.sh": "/data/data/com.termux/files/home/bin/battery_guard.sh",
    "scripts/laptop_session.py": "/data/data/com.termux/files/home/bin/laptop_session.py",
    "scripts/server-down": "/data/data/com.termux/files/usr/bin/server-down",
    "scripts/server-health": "/data/data/com.termux/files/usr/bin/server-health",
    "scripts/server-restart": "/data/data/com.termux/files/usr/bin/server-restart",
    "scripts/server-status": "/data/data/com.termux/files/usr/bin/server-status",
    "scripts/server-up": "/data/data/com.termux/files/usr/bin/server-up",
    "scripts/server-user": "/data/data/com.termux/files/usr/bin/server-user",
    "scripts/server_supervisor.py": "/data/data/com.termux/files/home/bin/server_supervisor.py",
    "scripts/start-services.sh": "/data/data/com.termux/files/home/start_services.sh",
    "scripts/start_suwayomi.sh": "/data/data/com.termux/files/home/bin/start_suwayomi.sh",
    "scripts/suwayomi_daily_update.py": "/data/data/com.termux/files/home/bin/suwayomi_daily_update.py",
    "scripts/suwayomi_graphql_filter.py": "/data/data/com.termux/files/home/bin/suwayomi_graphql_filter.py",
    "scripts/suwayomi_update.sh": "/data/data/com.termux/files/home/bin/suwayomi_update.sh",
    "scripts/termux_session.sh": "/data/data/com.termux/files/home/bin/termux_session.sh",

    # config/ - Edge proxy configuration
    "config/Caddyfile": "/data/data/com.termux/files/home/.config/caddy/Caddyfile",
}

# Required assets that cannot silently be omitted from any valid release.
REQUIRED_SOURCES: Set[str] = {
    "web/admin.html",
    "web/family.html",
    "web/login.html",
    "web/portal.css",
    "app/limits.py",
    "app/auth.py",
    "app/dashboard.py",
    "app/telemetry.py",
    "app/tor_proxy.py",
    "app/laptop.py",
    "web/agy_index.html",
    "web/termux_index.html",
    "web/filebrowser.js",
    "web/laptop.js",
    "web/interactive_player.js",
    "scripts/server_supervisor.py",
    "scripts/battery_guard.sh",
    "scripts/agy_session.sh",
    "scripts/laptop_session.py",
    "scripts/termux_session.sh",
    "scripts/start-services.sh",
    "scripts/start_suwayomi.sh",
    "scripts/suwayomi_daily_update.py",
    "scripts/suwayomi_graphql_filter.py",
    "scripts/suwayomi_update.sh",
    "scripts/server-user",
    "scripts/server-status",
    "scripts/server-health",
    "scripts/server-up",
    "scripts/server-down",
    "scripts/server-restart",
    "scripts/agy-resume",
    "scripts/agy-runner-loop",
    "config/Caddyfile",
}

# Source directory prefixes allowed for release inputs.
ALLOWED_SOURCE_PREFIXES: Tuple[str, ...] = ("app/", "web/", "scripts/", "config/", ".work/")

# Authorized target prefixes on the deployed system.
ALLOWED_REMOTE_PREFIXES: Tuple[str, ...] = ("/data/data/com.termux/files/",)

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

DEFAULT_GATEWAY_LOCAL = ".work/mobile-gateway"
DEFAULT_GATEWAY_REMOTE = "/data/data/com.termux/files/home/bin/mobile-gateway"


class ManifestError(Exception):
    """Base exception for manifest validation and generation errors."""
    pass


def is_forbidden_secret_or_runtime_path(path_str: str) -> bool:
    """Check whether a path matches forbidden secret, runtime, DB, or credential patterns."""
    normalized = path_str.replace("\\", "/").lower()
    basename = os.path.basename(normalized)
    parts = normalized.split("/")

    for pattern in FORBIDDEN_SECRET_PATTERNS:
        pat_lower = pattern.lower()
        if fnmatch.fnmatch(basename, pat_lower):
            return True
        if fnmatch.fnmatch(normalized, pat_lower):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat_lower):
                return True

    # Check substring indicators of credentials/tokens
    for keyword in ("token", "credential", "private_key", "secret_key"):
        if keyword in basename:
            return True

    return False


def validate_path_safety(local_str: str, remote_str: str, root_dir: Path) -> None:
    """Refuse path traversal, unsafe destinations, forbidden secret/runtime patterns."""
    # 1. Local path checks
    if not local_str or not isinstance(local_str, str):
        raise ManifestError("Local source path must be a non-empty string")

    norm_local = os.path.normpath(local_str)
    if os.path.isabs(local_str):
        raise ManifestError(f"Refused absolute source path: {local_str}")

    if norm_local.startswith("..") or "/../" in local_str or local_str.startswith("../"):
        raise ManifestError(f"Refused path traversal in source: {local_str}")

    if not any(norm_local.startswith(prefix.rstrip("/")) for prefix in ALLOWED_SOURCE_PREFIXES):
        raise ManifestError(
            f"Refused source path: {local_str} is not inside allowed source roots "
            f"({', '.join(ALLOWED_SOURCE_PREFIXES)})"
        )

    if is_forbidden_secret_or_runtime_path(local_str):
        raise ManifestError(f"Refused secret or runtime class in source path: {local_str}")

    # Check symlink escape on filesystem if file exists
    full_local = root_dir / norm_local
    if full_local.is_symlink():
        try:
            target = full_local.resolve()
            root_resolved = root_dir.resolve()
            if not target.is_relative_to(root_resolved):
                raise ManifestError(f"Refused symlink escape: {local_str} points outside release root ({target})")
            if is_forbidden_secret_or_runtime_path(str(target)):
                raise ManifestError(f"Refused symlink pointing to forbidden secret: {local_str} -> {target}")
        except (ValueError, RuntimeError) as exc:
            raise ManifestError(f"Refused invalid symlink: {local_str}: {exc}") from exc

    # 2. Remote path checks
    if not remote_str or not isinstance(remote_str, str):
        raise ManifestError(f"Remote destination path for '{local_str}' must be a non-empty string")

    if not os.path.isabs(remote_str):
        raise ManifestError(f"Refused relative remote destination: {remote_str}")

    norm_remote = os.path.normpath(remote_str)
    if "/../" in remote_str or norm_remote.startswith("..") or "/.." in remote_str:
        raise ManifestError(f"Refused path traversal in remote destination: {remote_str}")

    if not any(norm_remote.startswith(prefix) for prefix in ALLOWED_REMOTE_PREFIXES):
        raise ManifestError(
            f"Refused remote destination: {remote_str} is outside permitted prefix "
            f"({', '.join(ALLOWED_REMOTE_PREFIXES)})"
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
    mapping: Optional[Dict[str, str]] = None,
    built_artifacts: Optional[List[Tuple[str, str]]] = None,
    include_gateway: bool = False,
    require_gateway: bool = False,
) -> List[Dict[str, Any]]:
    """
    Generate candidate release manifest from source tree.
    Enforces that all required sources exist, newly added app/*.py modules are mapped,
    and returns deterministically sorted entries with exact sha256 and sizes.
    """
    effective_mapping = dict(CANONICAL_MAPPINGS) if mapping is None else dict(mapping)

    # Check for newly added Python runtime modules in app/
    app_dir = root_dir / "app"
    if app_dir.is_dir():
        for py_path in sorted(app_dir.glob("*.py")):
            rel_path = py_path.relative_to(root_dir).as_posix()
            if rel_path not in effective_mapping:
                # Newly added Python module must be mapped
                if rel_path == "app/dashboard.py":
                    effective_mapping[rel_path] = "/data/data/com.termux/files/usr/bin/dashboard"
                else:
                    effective_mapping[rel_path] = f"/data/data/com.termux/files/home/.local/lib/mobile-server/{py_path.name}"

    # Handle built gateway artifact
    if include_gateway or require_gateway:
        gw_path = root_dir / DEFAULT_GATEWAY_LOCAL
        if gw_path.exists():
            effective_mapping[DEFAULT_GATEWAY_LOCAL] = DEFAULT_GATEWAY_REMOTE
        elif require_gateway:
            raise ManifestError(
                f"Required Go built artifact not found at {gw_path}. "
                "Build gateway first: cd gateway && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -o ../.work/mobile-gateway ."
            )

    # Process explicit declared built artifacts
    if built_artifacts:
        for local_art, remote_art in built_artifacts:
            effective_mapping[local_art] = remote_art

    # Validate required sources are present in mapping
    missing_required = REQUIRED_SOURCES - set(effective_mapping.keys())
    if missing_required:
        raise ManifestError(
            f"Release manifest generation missing required sources: {', '.join(sorted(missing_required))}"
        )

    # Validate unique remote destinations
    seen_remotes: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []

    for local_str in sorted(effective_mapping.keys()):
        remote_str = effective_mapping[local_str]

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

    # Sort deterministically by local source path
    entries.sort(key=lambda x: x["local"])
    return entries


def validate_manifest(
    manifest_entries: List[Dict[str, Any]],
    root_dir: Path,
    require_gateway: bool = False,
) -> None:
    """
    Validate candidate release manifest entries against source/candidate bundle root.
    Nonzero failure on missing, unmapped, tampered, escaping, or forbidden files.
    """
    if not isinstance(manifest_entries, list):
        raise ManifestError("Manifest root must be a list of file entries")

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
        expected_sha256 = entry["sha256"].lower()
        expected_bytes = entry["bytes"]

        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ManifestError(f"Invalid byte size {expected_bytes} for {local_str}")

        validate_path_safety(local_str, remote_str, root_dir)

        if remote_str in seen_remotes:
            raise ManifestError(
                f"Duplicate remote destination '{remote_str}' declared for '{seen_remotes[remote_str]}' and '{local_str}'"
            )
        seen_remotes[remote_str] = local_str

        if local_str in manifest_locals:
            raise ManifestError(f"Duplicate local entry in manifest: '{local_str}'")
        manifest_locals.add(local_str)

        # Verify file presence and integrity on disk
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

    # Verify all required core sources are declared
    missing_required = REQUIRED_SOURCES - manifest_locals
    if missing_required:
        raise ManifestError(
            f"Release manifest omits required assets: {', '.join(sorted(missing_required))}"
        )

    # Verify no newly added Python runtime module in app/ was silently omitted
    app_dir = root_dir / "app"
    if app_dir.is_dir():
        for py_path in sorted(app_dir.glob("*.py")):
            rel_path = py_path.relative_to(root_dir).as_posix()
            if rel_path not in manifest_locals:
                raise ManifestError(
                    f"Newly added Python runtime module '{rel_path}' is not declared in release manifest. "
                    "All runtime app modules must be explicitly mapped in the release."
                )

    # If gateway is required, verify it is declared and matched
    if require_gateway and DEFAULT_GATEWAY_LOCAL not in manifest_locals:
        raise ManifestError(
            f"Required Go built artifact '{DEFAULT_GATEWAY_LOCAL}' is not declared in release manifest"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic release manifest generator and offline validator"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Generate parser
    gen_parser = subparsers.add_parser("generate", help="Generate deterministic release manifest")
    gen_parser.add_argument("--root", default=".", help="Root directory of release source (default: .)")
    gen_parser.add_argument("--output", "-o", default=None, help="Output JSON path (default: stdout)")
    gen_parser.add_argument("--mapping", default=None, help="Optional JSON mapping override file")
    gen_parser.add_argument(
        "--built-artifact",
        action="append",
        dest="built_artifacts",
        metavar="LOCAL:REMOTE",
        help="Explicit declared built artifact mapping (can be repeated)",
    )
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

    # Validate parser
    val_parser = subparsers.add_parser("validate", help="Validate candidate release manifest")
    val_parser.add_argument("--manifest", "-m", required=True, help="Path to manifest JSON file")
    val_parser.add_argument("--root", default=".", help="Root directory of candidate bundle (default: .)")
    val_parser.add_argument(
        "--require-gateway",
        action="store_true",
        help=f"Require {DEFAULT_GATEWAY_LOCAL} to be present in manifest and on disk",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    root_dir = Path(args.root).resolve()

    try:
        if args.command == "generate":
            mapping: Optional[Dict[str, str]] = None
            if args.mapping:
                with open(args.mapping, "r", encoding="utf-8") as f:
                    raw_mapping = json.load(f)
                    if isinstance(raw_mapping, list):
                        mapping = {item["local"]: item["remote"] for item in raw_mapping}
                    elif isinstance(raw_mapping, dict):
                        mapping = raw_mapping
                    else:
                        raise ManifestError("Mapping file must be a JSON array or object")

            built_list: List[Tuple[str, str]] = []
            if args.built_artifacts:
                for item in args.built_artifacts:
                    if ":" not in item:
                        raise ManifestError(f"Built artifact must be in LOCAL:REMOTE format: {item}")
                    loc, rem = item.split(":", 1)
                    built_list.append((loc.strip(), rem.strip()))

            entries = generate_manifest(
                root_dir=root_dir,
                mapping=mapping,
                built_artifacts=built_list,
                include_gateway=args.include_gateway,
                require_gateway=args.require_gateway,
            )

            # Format deterministic JSON
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
            )
            print(f"Release manifest validation PASSED: {len(entries)} verified entries.")
            return 0

    except (ManifestError, json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
