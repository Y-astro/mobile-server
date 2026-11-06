#!/usr/bin/env python3
"""Tests for mobile music storage migration tool (tools/split_music_storage.py).

Verifies:
- Plan generation on real filesystem structures without mutation.
- Explicit destination requirement (family vs. private).
- Precondition enforcement (quiesce assertion).
- Fail-closed privacy: family_share/Music link narrowed BEFORE moving private data.
- Preservation of sibling links (Documents, Download, Photos) in family_share.
- Zero data loss: file counts, byte totals, and directory structures match.
- Collision, foreign journal, and nested symlink refusal.
- Exclusive locking / concurrency prevention.
- Forward resumability after simulated interruption (link narrowed stage, mid-move stage).
- Idempotence on re-apply and re-resume.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from tools.split_music_storage import (
    ConflictError,
    MigrationError,
    MusicStorageMigrator,
    PreconditionError,
    ValidationError,
)


class TestMusicStorageMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.test_dir.name).resolve()

        # Simulated phone filesystem layout
        self.storage_dir = self.base_dir / "storage"
        self.music_root = self.storage_dir / "Music"
        self.music_root.mkdir(parents=True)

        self.family_share_dir = self.base_dir / "home" / "family_share"
        self.family_share_dir.mkdir(parents=True)

        # Create sibling targets and links in family_share
        self.docs_dir = self.storage_dir / "Documents"
        self.docs_dir.mkdir(parents=True)
        self.downloads_dir = self.storage_dir / "Download"
        self.downloads_dir.mkdir(parents=True)
        self.photos_dir = self.storage_dir / "Pictures"
        self.photos_dir.mkdir(parents=True)

        os.symlink(str(self.docs_dir), str(self.family_share_dir / "Documents"))
        os.symlink(str(self.downloads_dir), str(self.family_share_dir / "Download"))
        os.symlink(str(self.photos_dir), str(self.family_share_dir / "Photos"))

        # Family music link currently points to broad Music
        self.family_music_link = self.family_share_dir / "Music"
        os.symlink(str(self.music_root), str(self.family_music_link))

        # Populate legacy music entries (mix of loose files and directories)
        self.expected_files_count = 0
        self.expected_total_bytes = 0
        self.legacy_items = [
            (".thumbnails/thumb1.jpg", b"THUMBNAIL_BYTES"),
            ("Abhang panch goulani.mp3", b"FLAC_OR_MP3_CONTENT_1"),
            ("AbhishekRaghuram CDW630/track1.mp3", b"CARNATIC_MUSIC_AUDIO_TRACK_1"),
            ("AbhishekRaghuram CDW630/track2.mp3", b"CARNATIC_MUSIC_AUDIO_TRACK_2_LONG"),
            ("Ananya recordings/session1/take1.wav", b"RECORDING_AUDIO_SESSION_WAV_BYTES"),
            ("Ananya recordings/session1/take2.wav", b"RECORDING_AUDIO_SESSION_WAV_BYTES_2"),
            ("Movie songs/albumA/song1.flac", b"HIRES_FLAC_AUDIO_BYTES_SAMPLE_24BIT"),
            ("Movie songs/albumB/song2.flac", b"HIRES_FLAC_AUDIO_BYTES_SAMPLE_24BIT_B"),
            ("Music/subitem.mp3", b"SUBITEM_AUDIO"),
            ("Music classes RNT sir/lesson1.m4a", b"AUDIO_LESSON_CONTENT_DATA"),
            ("Music classes TVG sir/lesson2.m4a", b"AUDIO_LESSON_CONTENT_DATA_2"),
            ("Pavan Sharade/trackA.mp3", b"TRACK_AUDIO_DATA_SHARADE"),
            ("Pavan music/carnatic/song.flac", b"PAVAN_MUSIC_FLAC_DATA"),
            ("Rajkumar Bharathi-TVG sir comp/disc1/track01.mp3", b"COMPOSITION_DATA"),
            ("Yadu ringtones/alarm.ogg", b"RINGTONE_OGG_SAMPLE"),
            ("to learn/practice/scale.mp3", b"PRACTICE_SCALE_AUDIO_BYTES"),
        ]

        for rel_path, data in self.legacy_items:
            full_path = self.music_root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_bytes(data)
            self.expected_files_count += 1
            self.expected_total_bytes += len(data)

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_plan_does_not_mutate(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        plan = migrator.plan(legacy_destination="family")
        self.assertEqual(plan["total_legacy_files"], self.expected_files_count)
        self.assertEqual(plan["total_legacy_bytes"], self.expected_total_bytes)
        self.assertEqual(plan["total_legacy_entries"], 13)

        # Verify filesystem is untouched
        self.assertTrue((self.music_root / "Abhang panch goulani.mp3").exists())
        self.assertFalse((self.music_root / "Family").exists())
        self.assertFalse((self.music_root / "Astro").exists())
        self.assertEqual(
            os.readlink(str(self.family_music_link)), str(self.music_root)
        )

    def test_apply_requires_assert_quiesced(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        with self.assertRaises(PreconditionError) as ctx:
            migrator.apply(legacy_destination="family", assert_quiesced=False)
        self.assertIn("must be quiesced", str(ctx.exception))

    def test_apply_requires_valid_destination(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        with self.assertRaises(ValidationError) as ctx:
            migrator.apply(legacy_destination="invalid_dest", assert_quiesced=True)
        self.assertIn("Invalid legacy_destination", str(ctx.exception))

    def test_apply_family_destination_success(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        res = migrator.apply(legacy_destination="family", assert_quiesced=True)
        self.assertEqual(res["stage"], "COMPLETED")

        # 1. Family music link narrowed to Music/Family
        target_family = self.music_root / "Family"
        self.assertEqual(
            Path(os.readlink(str(self.family_music_link))).resolve(),
            target_family.resolve(),
        )

        # 2. Sibling links in family_share intact
        self.assertEqual(
            Path(os.readlink(str(self.family_share_dir / "Documents"))).resolve(),
            self.docs_dir.resolve(),
        )
        self.assertEqual(
            Path(os.readlink(str(self.family_share_dir / "Download"))).resolve(),
            self.downloads_dir.resolve(),
        )
        self.assertEqual(
            Path(os.readlink(str(self.family_share_dir / "Photos"))).resolve(),
            self.photos_dir.resolve(),
        )

        # 3. All items moved to Family
        self.assertTrue((target_family / "Abhang panch goulani.mp3").exists())
        self.assertTrue((target_family / "Movie songs/albumA/song1.flac").exists())
        self.assertFalse((self.music_root / "Abhang panch goulani.mp3").exists())

        # 4. Music/Astro exists, empty, 0700
        target_astro = self.music_root / "Astro"
        self.assertTrue(target_astro.exists())
        self.assertTrue(target_astro.is_dir())
        self.assertEqual(len(list(target_astro.iterdir())), 0)
        self.assertEqual(target_astro.stat().st_mode & 0o777, 0o700)

        # 5. Exact byte and file verification
        family_files = sum(len(files) for _, _, files in os.walk(str(target_family)))
        family_bytes = sum(
            (Path(root) / f).stat().st_size
            for root, _, files in os.walk(str(target_family))
            for f in files
        )
        self.assertEqual(family_files, self.expected_files_count)
        self.assertEqual(family_bytes, self.expected_total_bytes)

    def test_apply_private_destination_success(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        res = migrator.apply(legacy_destination="private", assert_quiesced=True)
        self.assertEqual(res["stage"], "COMPLETED")

        target_family = self.music_root / "Family"
        target_astro = self.music_root / "Astro"

        # 1. Family link narrowed to Music/Family
        self.assertEqual(
            Path(os.readlink(str(self.family_music_link))).resolve(),
            target_family.resolve(),
        )

        # 2. Family folder is empty: 0 files, 0 bytes
        self.assertTrue(target_family.exists())
        self.assertEqual(len(list(target_family.iterdir())), 0)

        # 3. Astro folder contains all legacy items
        self.assertTrue((target_astro / "Abhang panch goulani.mp3").exists())
        self.assertTrue((target_astro / "Movie songs/albumA/song1.flac").exists())

        astro_files = sum(len(files) for _, _, files in os.walk(str(target_astro)))
        astro_bytes = sum(
            (Path(root) / f).stat().st_size
            for root, _, files in os.walk(str(target_astro))
            for f in files
        )
        self.assertEqual(astro_files, self.expected_files_count)
        self.assertEqual(astro_bytes, self.expected_total_bytes)

    def test_collision_refusal_before_narrowing(self) -> None:
        # Create a conflicting file named "Family" in music_root
        (self.music_root / "Family").write_text("collision")

        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        with self.assertRaises(ConflictError) as ctx:
            migrator.apply(legacy_destination="family", assert_quiesced=True)
        self.assertRegex(str(ctx.exception), "already exists|Unsafe migration destination")

        # Verify link was NOT retargeted and no files were moved
        self.assertEqual(
            os.readlink(str(self.family_music_link)), str(self.music_root)
        )
        self.assertTrue((self.music_root / "Abhang panch goulani.mp3").exists())

    def test_nested_symlink_refusal(self) -> None:
        # Create a forbidden nested symlink in music storage
        escape_link = self.music_root / "Movie songs" / "escape_symlink"
        os.symlink(str(self.base_dir), str(escape_link))

        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        with self.assertRaises(ValidationError) as ctx:
            migrator.plan(legacy_destination="family")
        self.assertIn("Nested symlink detected", str(ctx.exception))

    def test_foreign_journal_refusal(self) -> None:
        journal_path = self.family_share_dir.parent / ".local/state/mobile-server/music-storage/journal.json"
        journal_path.parent.mkdir(parents=True)
        foreign_data = {
            "version": 999,
            "tool": "alien_tool",
            "music_root": "/other/path",
        }
        journal_path.write_text(json.dumps(foreign_data))
        journal_path.chmod(0o600)

        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        with self.assertRaises(ConflictError) as ctx:
            migrator.apply(legacy_destination="family", assert_quiesced=True)
        self.assertIn("Foreign or incompatible migration journal", str(ctx.exception))

    def test_exclusive_lock_prevents_concurrency(self) -> None:
        migrator1 = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        migrator2 = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )

        with migrator1:
            with self.assertRaises(ConflictError) as ctx:
                with migrator2:
                    pass
            self.assertIn("Could not acquire exclusive migration lock", str(ctx.exception))

    def test_fault_after_link_narrowed_and_resume(self) -> None:
        """Simulate interruption right after link is narrowed but before any moves."""
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )

        # Perform stages 1, 2, 3 manually to simulate crash after link narrowing
        plan = migrator.plan(legacy_destination="private")
        family_dir = self.music_root / "Family"
        astro_dir = self.music_root / "Astro"
        family_dir.mkdir()
        astro_dir.mkdir()

        # Narrow link
        migrator._atomically_retarget_link(self.family_music_link, family_dir)

        # Write journal at LINK_NARROWED stage
        journal = {
            "version": 1,
            "tool": "split_music_storage",
            "created_at": 1000.0,
            "stage": "LINK_NARROWED",
            "music_root": str(self.music_root),
            "family_music_link": str(self.family_music_link),
            "original_link_target": str(self.music_root),
            "target_family_dir": str(family_dir),
            "target_astro_dir": str(astro_dir),
            "legacy_destination": "private",
            "planned_entries": plan["planned_entries"],
            "total_legacy_entries": plan["total_legacy_entries"],
            "total_legacy_files": plan["total_legacy_files"],
            "total_legacy_bytes": plan["total_legacy_bytes"],
            "migrated_entries": [],
        }
        migrator._write_journal(journal)

        # Verify fail-closed invariant: link is narrow, zero private data exposed
        self.assertEqual(
            Path(os.readlink(str(self.family_music_link))).resolve(),
            family_dir.resolve(),
        )
        self.assertEqual(len(list(family_dir.iterdir())), 0)

        # Now resume
        res = migrator.resume(assert_quiesced=True)
        self.assertEqual(res["stage"], "COMPLETED")

        # Verify all legacy items moved to Astro
        astro_files = sum(len(files) for _, _, files in os.walk(str(astro_dir)))
        self.assertEqual(astro_files, self.expected_files_count)
        self.assertEqual(len(list(family_dir.iterdir())), 0)

    def test_fault_mid_move_and_resume(self) -> None:
        """Simulate interruption mid-way through moving entries."""
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )

        plan = migrator.plan(legacy_destination="family")
        family_dir = self.music_root / "Family"
        astro_dir = self.music_root / "Astro"
        family_dir.mkdir()
        astro_dir.mkdir()

        migrator._atomically_retarget_link(self.family_music_link, family_dir)

        # Move only first 3 planned entries
        planned = plan["planned_entries"]
        moved_names = []
        for entry in planned[:3]:
            name = entry["name"]
            os.rename(str(self.music_root / name), str(family_dir / name))
            moved_names.append(name)

        journal = {
            "version": 1,
            "tool": "split_music_storage",
            "created_at": 1000.0,
            "stage": "LINK_NARROWED",
            "music_root": str(self.music_root),
            "family_music_link": str(self.family_music_link),
            "original_link_target": str(self.music_root),
            "target_family_dir": str(family_dir),
            "target_astro_dir": str(astro_dir),
            "legacy_destination": "family",
            "planned_entries": planned,
            "total_legacy_entries": plan["total_legacy_entries"],
            "total_legacy_files": plan["total_legacy_files"],
            "total_legacy_bytes": plan["total_legacy_bytes"],
            "migrated_entries": moved_names,
        }
        migrator._write_journal(journal)

        # Resume from partial state
        res = migrator.resume(assert_quiesced=True)
        self.assertEqual(res["stage"], "COMPLETED")

        # Verify all entries now in family_dir
        family_files = sum(len(files) for _, _, files in os.walk(str(family_dir)))
        family_bytes = sum(
            (Path(root) / f).stat().st_size
            for root, _, files in os.walk(str(family_dir))
            for f in files
        )
        self.assertEqual(family_files, self.expected_files_count)
        self.assertEqual(family_bytes, self.expected_total_bytes)

    def test_idempotent_reapply(self) -> None:
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        res1 = migrator.apply(legacy_destination="family", assert_quiesced=True)
        self.assertEqual(res1["stage"], "COMPLETED")

        # Re-apply
        res2 = migrator.apply(legacy_destination="family", assert_quiesced=True)
        self.assertEqual(res2["stage"], "COMPLETED")

        # Re-resume
        res3 = migrator.resume(assert_quiesced=True)
        self.assertEqual(res3["stage"], "COMPLETED")

    def test_file_contents_preserved_verbatim(self) -> None:
        """Verify that every single byte of every file is preserved exactly."""
        migrator = MusicStorageMigrator(
            music_root=self.music_root,
            family_music_link=self.family_music_link,
        )
        migrator.apply(legacy_destination="family", assert_quiesced=True)

        target_family = self.music_root / "Family"
        for rel_path, expected_bytes in self.legacy_items:
            migrated_file = target_family / rel_path
            self.assertTrue(migrated_file.exists(), f"Missing file: {rel_path}")
            self.assertEqual(migrated_file.read_bytes(), expected_bytes)

    def interrupted_migration(self, destination="family"):
        from unittest.mock import patch
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        with patch.object(migrator, "_move_entries", side_effect=RuntimeError("power loss")):
            with self.assertRaises(RuntimeError):
                migrator.apply(destination, assert_quiesced=True)
        return migrator

    def test_resume_rejects_replaced_private_directory_symlink(self):
        migrator = self.interrupted_migration("private")
        outside = self.base_dir / "outside"
        outside.mkdir()
        (self.music_root / "Astro").rmdir()
        (self.music_root / "Astro").symlink_to(outside)
        with self.assertRaises(ConflictError):
            migrator.resume(assert_quiesced=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.family_music_link.resolve(), self.music_root / "Family")
        self.assertTrue((self.music_root / self.legacy_items[0][0]).exists())

    def test_resume_rejects_same_size_replacement_after_rename(self):
        from unittest.mock import patch
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        rename = os.rename
        def power_loss(source, target):
            rename(source, target)
            if Path(source).parent == self.music_root:
                raise OSError("power loss after rename")
        with patch("tools.split_music_storage.os.rename", side_effect=power_loss):
            with self.assertRaises(OSError):
                migrator.apply("family", assert_quiesced=True)
        moved = next((self.music_root / "Family").rglob("*"))
        if moved.is_dir():
            moved = next(path for path in moved.rglob("*") if path.is_file())
        original = moved.read_bytes()
        moved.unlink()
        moved.write_bytes(b"x" * len(original))
        with self.assertRaises(ConflictError):
            migrator.resume(assert_quiesced=True)
        self.assertTrue((self.music_root / "Abhang panch goulani.mp3").exists())

    def test_private_journal_and_lock_ignore_umask(self):
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        old_mask = os.umask(0o022)
        try:
            with migrator:
                migrator.apply("family", assert_quiesced=True)
        finally:
            os.umask(old_mask)
        self.assertFalse(migrator.journal_path.is_relative_to(self.music_root))
        self.assertEqual(migrator.journal_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(migrator.lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(migrator.journal_path.parent.stat().st_mode & 0o777, 0o700)

    def test_resume_rejects_journal_paths_and_traversal(self):
        migrator = self.interrupted_migration()
        original = json.loads(migrator.journal_path.read_text())
        outside = self.base_dir / "outside"
        outside.mkdir()
        for field, value in (("target_family_dir", str(outside)), ("family_music_link", str(outside / "Music"))):
            changed = dict(original, **{field: value})
            migrator._write_journal(changed)
            with self.assertRaises(ConflictError):
                migrator.resume(assert_quiesced=True)
        changed = json.loads(json.dumps(original))
        changed["planned_entries"][0]["name"] = "../sentinel"
        migrator._write_journal(changed)
        with self.assertRaises(ConflictError):
            migrator.resume(assert_quiesced=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_journal_symlink_refused_without_touching_sentinel(self):
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        migrator._prepare_state_directory()
        sentinel = self.base_dir / "sentinel"
        sentinel.write_bytes(b"KEEP")
        migrator.journal_path.symlink_to(sentinel)
        with self.assertRaises(ConflictError):
            migrator.apply("family", assert_quiesced=True)
        self.assertEqual(sentinel.read_bytes(), b"KEEP")
        self.assertFalse((self.music_root / "Family").exists())

    def test_resume_destination_is_immutable(self):
        migrator = self.interrupted_migration("family")
        with self.assertRaises(ConflictError):
            migrator.apply("private", assert_quiesced=True)
        self.assertEqual(list((self.music_root / "Astro").iterdir()), [])

    def test_final_music_root_symlink_refused(self):
        alias = self.storage_dir / "alias"
        alias.symlink_to(self.music_root, target_is_directory=True)
        migrator = MusicStorageMigrator(alias, self.family_music_link)
        with self.assertRaises(PreconditionError):
            migrator.apply("family", assert_quiesced=True)

    def test_legacy_hidden_journal_named_file_is_preserved_as_user_data(self):
        existing = self.music_root / ".split_music_journal.json"
        existing.write_bytes(b"existing user data")
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        migrator.apply("family", assert_quiesced=True)
        self.assertEqual((self.music_root / "Family" / existing.name).read_bytes(), b"existing user data")

    def test_completed_relative_family_link_is_valid(self):
        migrator = MusicStorageMigrator(self.music_root, self.family_music_link)
        migrator.apply("family", assert_quiesced=True)
        self.family_music_link.unlink()
        self.family_music_link.symlink_to(os.path.relpath(self.music_root / "Family", self.family_share_dir))
        migrator.resume(assert_quiesced=True)

    def test_cli_execution_plan_and_apply(self) -> None:
        """Test invoking the script via CLI argument parsing."""
        import subprocess

        # 1. Test plan command via CLI
        plan_proc = subprocess.run(
            [
                sys.executable,
                "tools/split_music_storage.py",
                "--music-root", str(self.music_root),
                "--family-music-link", str(self.family_music_link),
                "plan",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        plan_out = json.loads(plan_proc.stdout)
        self.assertEqual(plan_out["total_legacy_entries"], 13)

        # 2. Test apply without --assert-quiesced fails
        fail_proc = subprocess.run(
            [
                sys.executable,
                "tools/split_music_storage.py",
                "--music-root", str(self.music_root),
                "--family-music-link", str(self.family_music_link),
                "apply",
                "--legacy-destination", "family",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(fail_proc.returncode, 0)
        self.assertIn("must be quiesced", fail_proc.stderr)

        # 3. Test apply with --assert-quiesced succeeds
        apply_proc = subprocess.run(
            [
                sys.executable,
                "tools/split_music_storage.py",
                "--music-root", str(self.music_root),
                "--family-music-link", str(self.family_music_link),
                "apply",
                "--legacy-destination", "family",
                "--assert-quiesced",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        apply_out = json.loads(apply_proc.stdout)
        self.assertEqual(apply_out["stage"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
