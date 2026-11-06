#!/usr/bin/env python3
"""Durable, fail-closed filesystem migration tool for mobile music storage.

Splits /storage/emulated/0/Music into:
- /storage/emulated/0/Music/Family (shared family storage)
- /storage/emulated/0/Music/Astro (private storage)

Crucial Invariants:
1. Explicit destination: User must provide --legacy-destination=family|private.
   No default migration route is assumed.
2. Caller Precondition: Root operator must ensure FileBrowser and any active
   upload services are quiesced before apply/resume (--assert-quiesced).
3. Fail-Closed Privacy: The family_share/Music symlink is narrowed to Music/Family
   BEFORE any intended private files are moved. Interrupted runs never roll
   back the symlink to broad Music, preventing accidental Astro exposure.
4. Preserves Data: Moves top-level entries via atomic rename without copying,
   re-encoding, or deleting data. Verifies file counts and byte totals match.
5. Idempotent & Resumable: Uses an exclusive journal lock and fsync'd state
   transitions.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

TOOL_NAME = "split_music_storage"
JOURNAL_VERSION = 1

DEFAULT_MUSIC_ROOT = Path("/storage/emulated/0/Music")
DEFAULT_FAMILY_LINK = Path("/data/data/com.termux/files/home/family_share/Music")


class MigrationError(Exception):
    """Base exception for music storage migration failures."""


class PreconditionError(MigrationError):
    """Raised when preconditions (e.g. quiesce assertion, layout) fail."""


class ValidationError(MigrationError):
    """Raised when validation checks fail before or after mutation."""


class ConflictError(MigrationError):
    """Raised when unexpected collisions or foreign state are encountered."""


def scan_directory(path: Path) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Recursively scan a directory for regular files and total bytes without following symlinks."""
    total_files = 0
    total_bytes = 0
    entries_meta: List[Dict[str, Any]] = []

    for item in sorted(path.iterdir(), key=lambda p: p.name):
        # Ignore our journal and lock files
        if item.name in (".split_music_journal.json", ".split_music_journal.json.lock"):
            continue

        item_stat = item.lstat()
        if item.is_symlink():
            raise ValidationError(
                f"Nested symlink detected at {item}. Symlinks within music storage are forbidden."
            )

        if item.is_file():
            total_files += 1
            total_bytes += item_stat.st_size
            entries_meta.append({
                "name": item.name,
                "type": "file",
                "files_count": 1,
                "bytes": item_stat.st_size,
                "mtime_ns": item_stat.st_mtime_ns,
            })
        elif item.is_dir():
            dir_files = 0
            dir_bytes = 0
            for root, dirs, files in os.walk(str(item), followlinks=False):
                for d in dirs:
                    d_path = Path(root) / d
                    if d_path.is_symlink():
                        raise ValidationError(f"Nested symlink detected at {d_path}.")
                for f in files:
                    f_path = Path(root) / f
                    if f_path.is_symlink():
                        raise ValidationError(f"Nested symlink detected at {f_path}.")
                    f_stat = f_path.lstat()
                    dir_files += 1
                    dir_bytes += f_stat.st_size

            total_files += dir_files
            total_bytes += dir_bytes
            entries_meta.append({
                "name": item.name,
                "type": "dir",
                "files_count": dir_files,
                "bytes": dir_bytes,
                "mtime_ns": item_stat.st_mtime_ns,
            })

    return total_files, total_bytes, entries_meta


class MusicStorageMigrator:
    def __init__(
        self,
        music_root: Path,
        family_music_link: Path,
        journal_path: Optional[Path] = None,
    ):
        self.music_root = music_root.resolve()
        self.family_music_link = family_music_link
        self.family_share_dir = family_music_link.parent
        self.journal_path = (
            journal_path.resolve()
            if journal_path
            else self.music_root / ".split_music_journal.json"
        )
        self.lock_path = Path(str(self.journal_path) + ".lock")
        self._lock_fd: Optional[int] = None

    def acquire_lock(self) -> None:
        """Acquire an exclusive non-blocking file lock."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise ConflictError(
                f"Could not acquire exclusive migration lock on {self.lock_path}. "
                "Another migration process may be running."
            ) from e

    def release_lock(self) -> None:
        """Release the exclusive file lock."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> MusicStorageMigrator:
        self.acquire_lock()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release_lock()

    def _load_journal(self) -> Optional[Dict[str, Any]]:
        if not self.journal_path.exists():
            return None
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("tool") != TOOL_NAME or data.get("version") != JOURNAL_VERSION:
                raise ConflictError(
                    f"Foreign or incompatible journal found at {self.journal_path}: {data}"
                )
            if Path(data.get("music_root", "")).resolve() != self.music_root:
                raise ConflictError(
                    f"Journal music_root mismatch: expected {self.music_root}, got {data.get('music_root')}"
                )
            return data
        except json.JSONDecodeError as e:
            raise ConflictError(f"Corrupted journal file at {self.journal_path}") from e

    def _write_journal(self, data: Dict[str, Any]) -> None:
        tmp_journal = self.journal_path.with_name(
            f".tmp_{self.journal_path.name}_{uuid.uuid4().hex}"
        )
        with open(tmp_journal, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_journal, self.journal_path)

    def plan(self, legacy_destination: Optional[str] = None) -> Dict[str, Any]:
        """Generate a reviewable plan without mutating anything."""
        if not self.music_root.exists() or not self.music_root.is_dir():
            raise PreconditionError(f"Music root does not exist or is not a directory: {self.music_root}")

        if not self.family_music_link.is_symlink():
            raise PreconditionError(
                f"Family music link is not a symlink: {self.family_music_link}"
            )

        current_link_target = Path(os.readlink(str(self.family_music_link)))
        if not current_link_target.is_absolute():
            current_link_target = (self.family_share_dir / current_link_target).resolve()
        else:
            current_link_target = current_link_target.resolve()

        family_target = self.music_root / "Family"
        astro_target = self.music_root / "Astro"

        # Check existing journal if any
        existing_journal = self._load_journal()
        if existing_journal and existing_journal.get("stage") == "COMPLETED":
            return {
                "status": "ALREADY_COMPLETED",
                "journal": existing_journal,
            }

        # Check for destination collision if not already journaled
        if family_target.exists() and not (existing_journal and family_target.is_dir()):
            raise ConflictError(
                f"Target {family_target} already exists prior to migration!"
            )
        if astro_target.exists() and not (existing_journal and astro_target.is_dir()):
            raise ConflictError(
                f"Target {astro_target} already exists prior to migration!"
            )

        # Inventory legacy entries
        total_files, total_bytes, entries = scan_directory(self.music_root)
        planned_entries = [
            e for e in entries if e["name"] not in ("Family", "Astro")
        ]

        # Calculate planned counts
        planned_files = sum(e["files_count"] for e in planned_entries)
        planned_bytes = sum(e["bytes"] for e in planned_entries)

        plan_data = {
            "version": JOURNAL_VERSION,
            "tool": TOOL_NAME,
            "created_at": time.time(),
            "music_root": str(self.music_root),
            "family_music_link": str(self.family_music_link),
            "current_link_target": str(current_link_target),
            "target_family_dir": str(family_target),
            "target_astro_dir": str(astro_target),
            "legacy_destination": legacy_destination,
            "planned_entries": planned_entries,
            "total_legacy_entries": len(planned_entries),
            "total_legacy_files": planned_files,
            "total_legacy_bytes": planned_bytes,
        }
        return plan_data

    def apply(self, legacy_destination: str, assert_quiesced: bool = False) -> Dict[str, Any]:
        """Execute the planned migration."""
        if not assert_quiesced:
            raise PreconditionError(
                "Root operator precondition unsatisfied: FileBrowser and all upload services "
                "must be quiesced before running migration. Re-run with --assert-quiesced."
            )

        if legacy_destination not in ("family", "private"):
            raise ValidationError(
                f"Invalid legacy_destination '{legacy_destination}'. Must be strictly 'family' or 'private'."
            )

        journal = self._load_journal()
        if journal:
            if journal.get("stage") == "COMPLETED":
                # Verify post-conditions hold
                self._verify_completed_state(journal)
                return journal
            # Resume existing incomplete journal
            return self.resume(assert_quiesced=assert_quiesced)

        # Generate fresh plan
        plan = self.plan(legacy_destination=legacy_destination)
        if plan.get("status") == "ALREADY_COMPLETED":
            return plan["journal"]

        # Stage 1: Commit initial plan to journal
        journal = {
            "version": JOURNAL_VERSION,
            "tool": TOOL_NAME,
            "created_at": time.time(),
            "stage": "PLAN_COMMITTED",
            "music_root": str(self.music_root),
            "family_music_link": str(self.family_music_link),
            "original_link_target": plan["current_link_target"],
            "target_family_dir": plan["target_family_dir"],
            "target_astro_dir": plan["target_astro_dir"],
            "legacy_destination": legacy_destination,
            "planned_entries": plan["planned_entries"],
            "total_legacy_entries": plan["total_legacy_entries"],
            "total_legacy_files": plan["total_legacy_files"],
            "total_legacy_bytes": plan["total_legacy_bytes"],
            "migrated_entries": [],
        }
        self._write_journal(journal)

        # Stage 2: Create directories safely
        family_dir = Path(journal["target_family_dir"])
        astro_dir = Path(journal["target_astro_dir"])
        family_dir.mkdir(parents=True, exist_ok=True)
        astro_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(family_dir), 0o755)
        os.chmod(str(astro_dir), 0o700)

        journal["stage"] = "DIRECTORIES_CREATED"
        self._write_journal(journal)

        # Stage 3: Atomically narrow family_share/Music symlink to Music/Family
        # This MUST happen BEFORE moving any legacy items (especially if destination is private)
        self._atomically_retarget_link(self.family_music_link, family_dir)

        journal["stage"] = "LINK_NARROWED"
        self._write_journal(journal)

        # Stage 4: Move planned top-level entries into legacy destination
        dest_dir = family_dir if legacy_destination == "family" else astro_dir
        self._move_entries(journal, dest_dir)

        # Stage 5: Verification & completion
        self._verify_completed_state(journal)
        journal["stage"] = "COMPLETED"
        journal["completed_at"] = time.time()
        self._write_journal(journal)

        return journal

    def resume(self, assert_quiesced: bool = False) -> Dict[str, Any]:
        """Resume an incomplete migration from journal."""
        if not assert_quiesced:
            raise PreconditionError(
                "Root operator precondition unsatisfied: FileBrowser and all upload services "
                "must be quiesced before running resume. Re-run with --assert-quiesced."
            )

        journal = self._load_journal()
        if not journal:
            raise ConflictError(f"No journal found to resume at {self.journal_path}")

        if journal.get("stage") == "COMPLETED":
            self._verify_completed_state(journal)
            return journal

        family_dir = Path(journal["target_family_dir"])
        astro_dir = Path(journal["target_astro_dir"])
        family_dir.mkdir(parents=True, exist_ok=True)
        astro_dir.mkdir(parents=True, exist_ok=True)

        # Fail-closed invariant: ensure family link is narrowed to Music/Family
        current_target = Path(os.readlink(str(self.family_music_link))).resolve()
        if current_target != family_dir.resolve():
            self._atomically_retarget_link(self.family_music_link, family_dir)
            journal["stage"] = "LINK_NARROWED"
            self._write_journal(journal)

        legacy_destination = journal["legacy_destination"]
        dest_dir = family_dir if legacy_destination == "family" else astro_dir

        self._move_entries(journal, dest_dir)

        self._verify_completed_state(journal)
        journal["stage"] = "COMPLETED"
        journal["completed_at"] = time.time()
        self._write_journal(journal)

        return journal

    def _atomically_retarget_link(self, link_path: Path, new_target: Path) -> None:
        """Atomically replace a symlink using temporary link in same directory."""
        parent_dir = link_path.parent
        tmp_link = parent_dir / f".tmp_{link_path.name}_{uuid.uuid4().hex}"
        try:
            os.symlink(str(new_target), str(tmp_link))
            os.replace(str(tmp_link), str(link_path))
        except Exception:
            if tmp_link.is_symlink() or tmp_link.exists():
                try:
                    tmp_link.unlink()
                except OSError:
                    pass
            raise

    def _move_entries(self, journal: Dict[str, Any], dest_dir: Path) -> None:
        """Rename planned top-level entries into destination directory."""
        migrated: List[str] = journal.get("migrated_entries", [])
        for entry in journal["planned_entries"]:
            name = entry["name"]
            if name in migrated:
                continue

            src = self.music_root / name
            dst = dest_dir / name

            if dst.exists():
                if not src.exists():
                    # Already moved in previous attempt
                    migrated.append(name)
                    journal["migrated_entries"] = migrated
                    self._write_journal(journal)
                    continue
                raise ConflictError(
                    f"Collision during move: destination {dst} already exists while {src} still present!"
                )

            if not src.exists():
                raise ValidationError(
                    f"Planned entry {src} is missing from music root during migration!"
                )

            # Atomic directory/file rename within same filesystem
            os.rename(str(src), str(dst))
            migrated.append(name)
            journal["migrated_entries"] = migrated
            self._write_journal(journal)

    def _verify_completed_state(self, journal: Dict[str, Any]) -> None:
        """Verify file counts, byte totals, and symlink target."""
        family_dir = Path(journal["target_family_dir"])
        astro_dir = Path(journal["target_astro_dir"])
        legacy_dest = journal["legacy_destination"]
        dest_dir = family_dir if legacy_dest == "family" else astro_dir

        # 1. Verify link points to Music/Family
        if not self.family_music_link.is_symlink():
            raise ValidationError(f"{self.family_music_link} is not a symlink")
        link_target = Path(os.readlink(str(self.family_music_link))).resolve()
        if link_target != family_dir.resolve():
            raise ValidationError(
                f"Link target {link_target} does not match expected {family_dir.resolve()}"
            )

        # 2. Verify all planned top entries are present in dest_dir
        for entry in journal["planned_entries"]:
            name = entry["name"]
            target_path = dest_dir / name
            if not target_path.exists():
                raise ValidationError(f"Expected entry {target_path} is missing!")
            orig_src = self.music_root / name
            if orig_src.exists():
                raise ValidationError(f"Planned entry {orig_src} still exists in root!")

        # 3. Verify total regular files and bytes in dest_dir
        actual_files, actual_bytes, _ = scan_directory(dest_dir)
        expected_files = journal["total_legacy_files"]
        expected_bytes = journal["total_legacy_bytes"]

        if actual_files != expected_files:
            raise ValidationError(
                f"File count mismatch in {dest_dir}: expected {expected_files}, got {actual_files}"
            )
        if actual_bytes != expected_bytes:
            raise ValidationError(
                f"Byte total mismatch in {dest_dir}: expected {expected_bytes}, got {actual_bytes}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split mobile music storage into Family and Astro directories."
    )
    parser.add_argument(
        "--music-root",
        type=Path,
        default=DEFAULT_MUSIC_ROOT,
        help="Path to Music storage root directory (default: /storage/emulated/0/Music)",
    )
    parser.add_argument(
        "--family-music-link",
        type=Path,
        default=DEFAULT_FAMILY_LINK,
        help="Path to family_share/Music symlink (default: /data/data/com.termux/files/home/family_share/Music)",
    )
    parser.add_argument(
        "--journal-path",
        type=Path,
        default=None,
        help="Custom journal file path (default: <music_root>/.split_music_journal.json)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # plan command
    plan_p = subparsers.add_parser("plan", help="Inspect and display migration plan.")
    plan_p.add_argument(
        "--legacy-destination",
        choices=["family", "private"],
        default=None,
        help="Intended legacy destination for review (family or private).",
    )

    # apply command
    apply_p = subparsers.add_parser("apply", help="Execute migration.")
    apply_p.add_argument(
        "--legacy-destination",
        choices=["family", "private"],
        required=True,
        help="Explicit destination for existing legacy tracks (family or private). No default.",
    )
    apply_p.add_argument(
        "--assert-quiesced",
        action="store_true",
        default=False,
        help="Assert that Root operator has quiesced FileBrowser and active upload services.",
    )

    # resume command
    resume_p = subparsers.add_parser("resume", help="Resume an interrupted migration.")
    resume_p.add_argument(
        "--assert-quiesced",
        action="store_true",
        default=False,
        help="Assert that Root operator has quiesced FileBrowser and active upload services.",
    )

    # status command
    subparsers.add_parser("status", help="Inspect migration status and journal.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    migrator = MusicStorageMigrator(
        music_root=args.music_root,
        family_music_link=args.family_music_link,
        journal_path=args.journal_path,
    )

    try:
        with migrator:
            if args.command == "plan":
                plan = migrator.plan(legacy_destination=args.legacy_destination)
                print(json.dumps(plan, indent=2))
                return 0

            elif args.command == "apply":
                res = migrator.apply(
                    legacy_destination=args.legacy_destination,
                    assert_quiesced=args.assert_quiesced,
                )
                print(json.dumps({"status": "SUCCESS", "stage": res.get("stage")}, indent=2))
                return 0

            elif args.command == "resume":
                res = migrator.resume(assert_quiesced=args.assert_quiesced)
                print(json.dumps({"status": "SUCCESS", "stage": res.get("stage")}, indent=2))
                return 0

            elif args.command == "status":
                plan = migrator.plan()
                print(json.dumps(plan, indent=2))
                return 0

    except MigrationError as e:
        print(f"Migration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
