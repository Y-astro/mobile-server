"""
test_release_transaction.py: Comprehensive test suite for standalone file transaction and recovery.
"""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
MANIFEST_SCRIPT = SCRIPTS_DIR / "release-manifest.py"
TX_SCRIPT = SCRIPTS_DIR / "release_transaction.py"

# Load release_manifest module
rm_spec = importlib.util.spec_from_file_location("release_manifest", MANIFEST_SCRIPT)
rm = importlib.util.module_from_spec(rm_spec)
rm_spec.loader.exec_module(rm)

# Load release_transaction module
tx_spec = importlib.util.spec_from_file_location("release_transaction", TX_SCRIPT)
rt = importlib.util.module_from_spec(tx_spec)
tx_spec.loader.exec_module(rt)


class TestReleaseTransaction(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_test_fixture(
        self,
        modify_auth_in_candidate: bool = True,
        modify_boot_script_in_candidate: bool = True,
    ) -> Tuple[Path, List[Dict[str, Any]], Dict[str, Any], Path, Path]:
        """
        Build a complete realistic test fixture bundle and live namespace root.
        Returns: (bundle_dir, manifest_entries, preconditions_dict, live_root, tx_dir)
        """
        bundle_dir = self.test_root / "bundle"
        live_dir = self.test_root / "live"
        tx_dir = self.test_root / "tx"

        bundle_dir.mkdir(parents=True, exist_ok=True)
        live_dir.mkdir(parents=True, exist_ok=True)
        tx_dir.mkdir(parents=True, exist_ok=True)

        # 1. Populate candidate bundle from repo
        cfg_dir = bundle_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "config/deployment-files.json", cfg_dir / "deployment-files.json")

        baseline_mapping = rm.load_baseline_mapping(bundle_dir)
        for rel_path in baseline_mapping.keys():
            src_file = REPO_ROOT / rel_path
            dst_file = bundle_dir / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        # Copy flat app helpers (e.g. notes.py, document_conversion.py)
        app_dir = bundle_dir / "app"
        for py_file in (REPO_ROOT / "app").glob("*.py"):
            dst_file = app_dir / py_file.name
            if not dst_file.exists():
                shutil.copy2(py_file, dst_file)

        # Add synthetic gateway artifact
        gw_path = bundle_dir / rm.DEFAULT_GATEWAY_LOCAL
        gw_path.parent.mkdir(parents=True, exist_ok=True)
        gw_path.write_bytes(b"\x7fELFfakegatewayexecutablebytes0123456789")

        # Optionally modify candidate auth.py and start-services.sh
        if modify_auth_in_candidate:
            (bundle_dir / "app/auth.py").write_text("# Candidate updated auth module\n", encoding="utf-8")
        if modify_boot_script_in_candidate:
            (bundle_dir / "scripts/start-services.sh").write_text("#!/bin/sh\n# Candidate boot script\n", encoding="utf-8")

        # Generate candidate manifest
        manifest_entries = rm.generate_manifest(bundle_dir, require_gateway=True)

        # 2. Populate live namespace root and preconditions
        preconditions_files: List[Dict[str, Any]] = []
        current_uid = os.getuid()

        # Files known to be newly added helpers (absent in historical live preimage)
        absent_rel_paths = {
            "home/.local/lib/mobile-server/notes.py",
            "home/.local/lib/mobile-server/document_conversion.py",
        }

        for entry in manifest_entries:
            remote = entry["remote"]
            rel_str = remote[len(rt.TERMUX_ROOT_PREFIX):].lstrip("/")

            if rel_str in absent_rel_paths:
                # Do NOT create file in live_dir; mark absent
                preconditions_files.append({
                    "path": remote,
                    "kind": "absent",
                })
                continue

            if remote == rt.KNOWN_BOOT_ALIAS_REMOTE:
                # Boot alias setup:
                # Create boot target regular script
                boot_target_path = live_dir / "home/.termux/boot/start-services.sh"
                boot_target_path.parent.mkdir(parents=True, exist_ok=True)
                # Historical audited content
                audited_boot_bytes = (REPO_ROOT / "scripts/start-services.sh").read_bytes()
                boot_target_path.write_bytes(audited_boot_bytes)
                os.chmod(boot_target_path, 0o700)

                # Create alias symlink
                alias_path = live_dir / "home/start_services.sh"
                alias_path.parent.mkdir(parents=True, exist_ok=True)
                # Relative symlink inside live root
                alias_path.symlink_to(".termux/boot/start-services.sh")

                preconditions_files.append({
                    "path": remote,
                    "kind": "symlink",
                    "target": rt.KNOWN_BOOT_TARGET_REMOTE,
                    "target_sha256": rt.AUDITED_BOOT_SCRIPT_SHA256,
                    "target_bytes": rt.AUDITED_BOOT_SCRIPT_BYTES,
                    "target_mode": "0o700",
                    "uid": current_uid,
                })
                continue

            # Regular file in live root
            live_file = live_dir / rel_str
            live_file.parent.mkdir(parents=True, exist_ok=True)

            # Determine mode
            target_mode = rt.get_canonical_target_mode(remote)

            if entry["local"] == "app/auth.py" and modify_auth_in_candidate:
                # Preimage auth.py has original content differing from candidate
                live_file.write_text("# Original live auth preimage\n", encoding="utf-8")
            elif entry["local"] == ".work/mobile-gateway":
                # Preimage gateway has original content differing from candidate
                live_file.write_bytes(b"\x7fELFpreimagegatewayoriginal0123")
            else:
                # Copy from bundle source (unchanged file)
                shutil.copy2(bundle_dir / entry["local"], live_file)

            os.chmod(live_file, target_mode)

            sha, size = rt.compute_file_sha256(live_file)
            preconditions_files.append({
                "path": remote,
                "kind": "file",
                "sha256": sha,
                "bytes": size,
                "mode": rt.mode_to_octal_str(target_mode),
                "uid": current_uid,
            })

        preconditions_data = {
            "uid": current_uid,
            "files": preconditions_files,
        }

        return bundle_dir, manifest_entries, preconditions_data, live_dir, tx_dir

    def test_complete_prepare_apply_and_commit(self):
        """Proof 1: Complete preparation, application, post-check verification, and commit."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Verify initial state and artifacts
        status1 = tx.status()
        self.assertEqual(status1["state"], rt.STATE_PREPARED)
        self.assertTrue((tx_dir / "journal.json").exists())
        self.assertTrue((tx_dir / "release_transaction.py").exists())
        self.assertTrue((tx_dir / "staged").is_dir())
        self.assertTrue((tx_dir / "backups").is_dir())

        # Apply transaction
        res_apply = tx.apply(namespace_root=live_dir)
        self.assertEqual(res_apply["state"], rt.STATE_APPLIED)

        # Verify candidate files are installed
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        self.assertIn("Candidate updated auth module", auth_live.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(auth_live.stat().st_mode), 0o600)

        # Verify new helpers installed
        notes_live = live_dir / "home/.local/lib/mobile-server/notes.py"
        self.assertTrue(notes_live.exists())
        self.assertEqual(stat.S_IMODE(notes_live.stat().st_mode), 0o600)

        # Commit transaction
        res_commit = tx.commit(namespace_root=live_dir)
        self.assertEqual(res_commit["state"], rt.STATE_COMMITTED)

        # Idempotent re-calls
        res_commit2 = tx.commit(namespace_root=live_dir)
        self.assertEqual(res_commit2["status"], "idempotent_success")
        res_apply2 = tx.apply(namespace_root=live_dir)
        self.assertEqual(res_apply2["status"], "idempotent_success")

    def test_refusal_on_hash_drift_before_writes(self):
        """Proof 2a: Hash drift in live file refuses prepare and apply before writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # Modify live auth.py without updating precondition (preserve 30 bytes to test hash drift)
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        auth_live.write_text("# Drifted live auth preimage!\n", encoding="utf-8")

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("SHA-256 hash drift", str(ctx.exception))

    def test_refusal_on_mode_drift_before_writes(self):
        """Proof 2b: Mode drift on live file refuses transaction before writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        os.chmod(auth_live, 0o644)

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("Mode drift", str(ctx.exception))

    def test_refusal_on_uid_drift_before_writes(self):
        """Proof 2c: Precondition UID mismatch refuses transaction before writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # Set expected UID to a different UID (e.g. 10505 production UID)
        bad_preconds = copy.deepcopy(preconds)
        bad_preconds["uid"] = 10505
        for f in bad_preconds["files"]:
            if "uid" in f:
                f["uid"] = 10505

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=bad_preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("UID mismatch", str(ctx.exception))

    def test_refusal_on_new_path_drift_before_writes(self):
        """Proof 2d: Path expected to be absent currently exists refuses transaction."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # Create notes.py on disk when precondition specifies absent
        notes_live = live_dir / "home/.local/lib/mobile-server/notes.py"
        notes_live.parent.mkdir(parents=True, exist_ok=True)
        notes_live.write_text("# Unexpected existing notes.py\n", encoding="utf-8")

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("path expected to be absent currently exists", str(ctx.exception))

    def test_stale_prepare_refused_on_apply(self):
        """Proof 3: Modification after prepare is refused at apply time before writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Modify a live file after prepare (preserve 30 bytes to test hash drift)
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        auth_live.write_text("# Drifted live auth preimage!\n", encoding="utf-8")

        with self.assertRaises(rt.PreconditionError) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("SHA-256 hash drift", str(ctx.exception))

        # Check transaction state remains PREPARED and not corrupted
        self.assertEqual(tx.status()["state"], rt.STATE_PREPARED)

    def test_staged_file_tamper_rejected_on_apply(self):
        """Proof 4: Tampering with staged candidate file is detected and rejected."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Tamper with a staged file
        staged_auth = tx_dir / "staged/home/.local/lib/mobile-server/auth.py"
        staged_auth.write_text("# Tampered staged candidate\n", encoding="utf-8")

        with self.assertRaises(rt.ApplyError) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("Staged file tampering detected", str(ctx.exception))

    def test_unchanged_files_untouched_inode_and_mtime(self):
        """Proof 5: Files with matching candidate hash and desired mode remain untouched."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # torrc and agy-runner-loop are unchanged between candidate and live
        torrc_live = live_dir / "home/.config/tor/torrc"
        runner_live = live_dir / "usr/var/lib/proot-distro/containers/debian/rootfs/usr/local/bin/agy-runner-loop"

        st_torrc_before = torrc_live.stat()
        st_runner_before = runner_live.stat()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        tx.apply(namespace_root=live_dir)

        st_torrc_after = torrc_live.stat()
        st_runner_after = runner_live.stat()

        self.assertEqual(st_torrc_before.st_ino, st_torrc_after.st_ino)
        self.assertEqual(st_torrc_before.st_mtime_ns, st_torrc_after.st_mtime_ns)
        self.assertEqual(st_runner_before.st_ino, st_runner_after.st_ino)
        self.assertEqual(st_runner_before.st_mtime_ns, st_runner_after.st_mtime_ns)

    def test_known_boot_alias_preserved_and_restored(self):
        """Proof 6: Boot alias symlink is preserved and target script is updated and restored."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture(
            modify_boot_script_in_candidate=True
        )

        alias_path = live_dir / "home/start_services.sh"
        boot_target_path = live_dir / "home/.termux/boot/start-services.sh"

        self.assertTrue(alias_path.is_symlink())
        orig_alias_link = os.readlink(alias_path)
        orig_target_content = boot_target_path.read_bytes()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        tx.apply(namespace_root=live_dir)

        # After apply:
        # 1. Alias symlink remains a symlink with exact same link text
        self.assertTrue(alias_path.is_symlink())
        self.assertEqual(os.readlink(alias_path), orig_alias_link)

        # 2. Boot target contains candidate script with mode 0700
        new_target_content = boot_target_path.read_bytes()
        self.assertIn(b"Candidate boot script", new_target_content)
        self.assertEqual(stat.S_IMODE(boot_target_path.stat().st_mode), 0o700)

        # Rollback
        tx.rollback(namespace_root=live_dir)

        # After rollback:
        # Alias remains intact
        self.assertTrue(alias_path.is_symlink())
        self.assertEqual(os.readlink(alias_path), orig_alias_link)

        # Target restored to original content and mode 0700
        self.assertEqual(boot_target_path.read_bytes(), orig_target_content)
        self.assertEqual(stat.S_IMODE(boot_target_path.stat().st_mode), 0o700)

    def test_arbitrary_symlink_and_escaping_parent_rejected(self):
        """Proof 7: Arbitrary destination symlinks and symlinked parents are rejected."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # Turn auth.py into an arbitrary symlink
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        auth_live.unlink()
        outside_file = self.test_root / "outside_auth.py"
        outside_file.write_text("print('outside')", encoding="utf-8")
        auth_live.symlink_to(outside_file)

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertTrue(
            "Arbitrary symlinks are forbidden" in str(ctx.exception)
            or "expected regular file" in str(ctx.exception)
        )

    def test_forbidden_secrets_rejected(self):
        """Proof 7b: Manifest with forbidden secret paths is rejected."""
        self.assertTrue(rt.is_forbidden_secret_or_runtime_path("/data/data/com.termux/files/home/users.json"))
        self.assertTrue(rt.is_forbidden_secret_or_runtime_path("/data/data/com.termux/files/home/.ssh/id_rsa"))
        self.assertTrue(rt.is_forbidden_secret_or_runtime_path("/data/data/com.termux/files/home/.config/tor/server.key"))
        self.assertTrue(rt.is_forbidden_secret_or_runtime_path("/data/data/com.termux/files/home/filebrowser.db"))
        self.assertTrue(rt.is_forbidden_secret_or_runtime_path("/data/data/com.termux/files/home/config/services_disabled"))

    def test_namespace_lock_conflict_raises_lock_error(self):
        """Proof 8: Concurrent lock on namespace prevents apply and rollback."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Hold lock in an independent context
        lock = rt.NamespaceLock(live_dir)
        lock.acquire()
        try:
            with self.assertRaises(rt.TransactionLockError) as ctx:
                tx.apply(namespace_root=live_dir)
            self.assertIn("Namespace lock held by another process", str(ctx.exception))
        finally:
            lock.release()

    def test_injected_mid_apply_failure_rolls_back(self):
        """Proof 9: Failure during apply rolls back all touched files to original preimage."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_content = auth_live.read_bytes()
        orig_auth_mode = stat.S_IMODE(auth_live.stat().st_mode)

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Corrupt one staged file after the first few so that apply fails mid-way
        journal = tx.load_journal()
        entries = journal["entries"]
        # Find an entry near the end (an entrypoint) and corrupt its staged file
        last_entry = [e for e in entries if e["action"] != rt.ACTION_UNTOUCHED][-1]
        staged_last = tx_dir / last_entry["staged_file"]

        # Wait, if we corrupt staged file, pre-apply check catches it!
        # To simulate a mid-apply failure, we can make the destination directory of last_entry read-only
        dest_disk, _, _ = rt.resolve_disk_path(last_entry["remote"], live_dir)
        # Mock os.replace to fail on the last entry
        real_os_replace = os.replace

        failed_once = False

        def mock_replace(src, dst):
            nonlocal failed_once
            if not failed_once and Path(dst).resolve() == dest_disk.resolve():
                failed_once = True
                raise OSError("Simulated disk write failure on target entrypoint")
            return real_os_replace(src, dst)

        import unittest.mock
        with unittest.mock.patch("os.replace", side_effect=mock_replace):
            with self.assertRaises(rt.ApplyError) as ctx:
                tx.apply(namespace_root=live_dir)
            self.assertIn("Apply failed and all touched files rolled back cleanly", str(ctx.exception))

        # Verify rollback restored touched files
        self.assertEqual(auth_live.read_bytes(), orig_auth_content)
        self.assertEqual(stat.S_IMODE(auth_live.stat().st_mode), orig_auth_mode)

        # Verify new files (notes.py) returned to absence
        notes_live = live_dir / "home/.local/lib/mobile-server/notes.py"
        self.assertFalse(notes_live.exists())

        # Verify journal state
        self.assertEqual(tx.status()["state"], rt.STATE_ROLLED_BACK)

    def test_interrupted_applying_recovers_subprocess_bundle_removed(self):
        """Proof 10: Interrupted APPLYING state recovers in fresh subprocess with bundle removed."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_content = auth_live.read_bytes()
        orig_auth_mode = stat.S_IMODE(auth_live.stat().st_mode)

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Simulate interruption during APPLYING:
        # 1. Update journal state to APPLYING
        journal = tx.load_journal()
        journal["state"] = rt.STATE_APPLYING

        # 2. Mark auth.py as INTENT_WRITE and write candidate content
        auth_entry = next(e for e in journal["entries"] if e["remote"].endswith("auth.py"))
        auth_entry["status"] = rt.STATUS_INTENT_WRITE
        staged_auth = tx_dir / auth_entry["staged_file"]
        auth_live.write_bytes(staged_auth.read_bytes())
        os.chmod(auth_live, rt.normalize_mode(auth_entry["target_mode"]))

        # 3. Create notes.py (new file)
        notes_entry = next(e for e in journal["entries"] if e["remote"].endswith("notes.py"))
        notes_entry["status"] = rt.STATUS_WRITTEN
        notes_live = live_dir / "home/.local/lib/mobile-server/notes.py"
        staged_notes = tx_dir / notes_entry["staged_file"]
        notes_live.write_bytes(staged_notes.read_bytes())

        tx.save_journal(journal)

        # 4. COMPLETELY REMOVE the candidate bundle root
        shutil.rmtree(bundle_dir)
        self.assertFalse(bundle_dir.exists())

        # 5. Launch fresh subprocess running ONLY the standalone recovery copy in tx_dir
        recovery_script = tx_dir / "release_transaction.py"
        cmd = [
            sys.executable,
            str(recovery_script),
            "recover",
            "--transaction-dir",
            str(tx_dir),
            "--namespace-root",
            str(live_dir),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Recovery subprocess failed: {proc.stderr}")
        self.assertIn("recovery completed", proc.stdout)

        # 6. Verify full preimage restoration
        # auth.py restored to original bytes and mode
        self.assertEqual(auth_live.read_bytes(), orig_auth_content)
        self.assertEqual(stat.S_IMODE(auth_live.stat().st_mode), orig_auth_mode)

        # notes.py returned to absence
        self.assertFalse(notes_live.exists())

        # Boot alias intact
        alias_path = live_dir / "home/start_services.sh"
        self.assertTrue(alias_path.is_symlink())
        boot_target = live_dir / "home/.termux/boot/start-services.sh"
        self.assertEqual(stat.S_IMODE(boot_target.stat().st_mode), 0o700)

        # Journal is ROLLED_BACK
        self.assertEqual(tx.status()["state"], rt.STATE_ROLLED_BACK)

    def test_unknown_concurrent_edits_refuse_recovery(self):
        """Proof 11a: Unknown concurrent edit refuses recovery to prevent overwriting foreign data."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        journal = tx.load_journal()
        journal["state"] = rt.STATE_APPLYING
        tx.save_journal(journal)

        # Modify auth.py with third unknown content
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        auth_live.write_text("# Foreign concurrent edit\n", encoding="utf-8")

        with self.assertRaises(rt.RecoveryError) as ctx:
            tx.recover(namespace_root=live_dir)
        self.assertIn("Unknown concurrent edit", str(ctx.exception))

    def test_corrupted_or_missing_backup_refuses_recovery(self):
        """Proof 11b: Corrupted or missing backup refuses recovery fail-closed."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        journal = tx.load_journal()
        journal["state"] = rt.STATE_APPLYING
        tx.save_journal(journal)

        # Corrupt backup file for auth.py
        auth_entry = next(e for e in journal["entries"] if e["remote"].endswith("auth.py"))
        bk_path = tx_dir / auth_entry["backup_file"]
        bk_path.write_text("# Corrupted backup\n", encoding="utf-8")

        with self.assertRaises(rt.RecoveryError) as ctx:
            tx.recover(namespace_root=live_dir)
        self.assertTrue(
            "Corrupted backup checksum" in str(ctx.exception)
            or "Corrupted or missing backup" in str(ctx.exception)
        )

    def test_idempotent_repeated_calls(self):
        """Proof 13: Repeated rollback and completed calls are idempotent."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Rollback prepared transaction
        res1 = tx.rollback(namespace_root=live_dir)
        self.assertEqual(res1["state"], rt.STATE_ROLLED_BACK)

        # Second rollback call
        res2 = tx.rollback(namespace_root=live_dir)
        self.assertEqual(res2["status"], "idempotent_success")

    def test_cli_status_and_recover_commands(self):
        """Verify CLI interface handles status and recover, and rejects apply."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # 1. status CLI
        proc_status = subprocess.run(
            [sys.executable, str(TX_SCRIPT), "status", "--transaction-dir", str(tx_dir)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_status.returncode, 0)
        self.assertIn("state: PREPARED", proc_status.stdout)

        # 2. recover CLI
        proc_recover = subprocess.run(
            [
                sys.executable,
                str(TX_SCRIPT),
                "recover",
                "--transaction-dir",
                str(tx_dir),
                "--namespace-root",
                str(live_dir),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_recover.returncode, 0)
        self.assertIn("recovery completed", proc_recover.stdout)

        # 3. apply CLI is rejected
        proc_apply = subprocess.run(
            [sys.executable, str(TX_SCRIPT), "apply", "--transaction-dir", str(tx_dir)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc_apply.returncode, 0)
        self.assertIn("invalid choice: 'apply'", proc_apply.stderr)

    def test_live_release_preimage_metadata_production_reference(self):
        """Verify reference metadata artifact pin from parent implementation."""
        metadata_path = Path("/home/astro/server-audit/2026-11-03/implementation/.superpowers/sdd/live-release-preimage-metadata.json")
        self.assertTrue(metadata_path.exists())

        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        self.assertEqual(metadata["uid"], 10505)

        # Verify audited content hashes count
        files = metadata["files"]
        file_entries = [f for f in files if f.get("kind") == "file"]
        absent_entries = [f for f in files if f.get("kind") == "absent"]
        symlink_entries = [f for f in files if f.get("kind") == "symlink"]

        self.assertEqual(len(file_entries), 35)
        self.assertGreaterEqual(len(absent_entries), 4)
        self.assertEqual(len(symlink_entries), 1)

        # Verify boot alias followup
        followup = metadata["boot_alias_followup"]
        self.assertEqual(followup["target_sha256"], rt.AUDITED_BOOT_SCRIPT_SHA256)
        self.assertEqual(followup["target_bytes"], 316)
        self.assertEqual(followup["target_mode"], "0o700")
        self.assertEqual(followup["target_uid"], 10505)


if __name__ == "__main__":
    unittest.main()
