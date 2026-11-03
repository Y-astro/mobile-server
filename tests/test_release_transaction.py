"""
test_release_transaction.py: Comprehensive test suite for standalone file transaction and recovery.
"""

import copy
import errno
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
from unittest.mock import patch
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
            if not src_file.exists():
                raise FileNotFoundError(f"Missing required baseline source file: {src_file}")
            dst_file = bundle_dir / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        # Copy web assets without skipping
        web_dir = bundle_dir / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        for web_file in (REPO_ROOT / "web").glob("*"):
            if web_file.is_file():
                shutil.copy2(web_file, web_dir / web_file.name)

        # Copy flat app helpers (e.g. notes.py, document_conversion.py)
        app_dir = bundle_dir / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
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
                    "uid": current_uid,
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
                alias_st = alias_path.lstat()

                preconditions_files.append({
                    "path": remote,
                    "kind": "symlink",
                    "target": rt.KNOWN_BOOT_TARGET_REMOTE,
                    "raw_link": os.readlink(alias_path),
                    "dev": alias_st.st_dev,
                    "ino": alias_st.st_ino,
                    "target_sha256": rt.AUDITED_BOOT_SCRIPT_SHA256,
                    "target_bytes": rt.AUDITED_BOOT_SCRIPT_BYTES,
                    "target_mode": "0o700",
                    "uid": current_uid,
                })
                continue

            # Regular file in live root
            live_file = live_dir / rel_str
            live_file.parent.mkdir(parents=True, exist_ok=True)

            # Determine mode from explicit policy
            norm_remote = remote.replace("\\", "/")
            if norm_remote.endswith("/usr/bin/dashboard") or norm_remote.endswith("/home/bin/mobile-gateway"):
                target_mode = 0o700
            elif norm_remote.endswith("/home/bin/agy-runner-loop") or norm_remote.endswith("/usr/local/bin/agy-runner-loop"):
                target_mode = 0o755
            elif "/home/.local/lib/mobile-server/" in norm_remote:
                target_mode = 0o600
            elif "/usr/bin/" in norm_remote or "/home/bin/" in norm_remote or norm_remote.endswith("/start-services.sh") or norm_remote.endswith("/start_services.sh"):
                target_mode = 0o700
            else:
                target_mode = 0o600

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

        journal = tx.load_journal()
        entries = journal["entries"]
        last_entry = [e for e in entries if e["action"] != rt.ACTION_UNTOUCHED][-1]
        dest_disk, _, _ = rt.resolve_disk_path(last_entry["remote"], live_dir)

        real_os_replace = os.replace
        failed_once = False

        def mock_replace(src, dst):
            nonlocal failed_once
            if not failed_once and Path(dst).resolve() == dest_disk.resolve():
                failed_once = True
                raise OSError("Simulated disk write failure on target entrypoint")
            return real_os_replace(src, dst)

        with patch("os.replace", side_effect=mock_replace):
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
        os.chmod(notes_live, rt.normalize_mode(notes_entry["target_mode"]))

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
        self.assertEqual(auth_live.read_bytes(), orig_auth_content)
        self.assertEqual(stat.S_IMODE(auth_live.stat().st_mode), orig_auth_mode)
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

    def test_canonical_modes_explicit_policy(self):
        """Proof: Literal policy verification independent of target derivation logic."""
        policy_checks = [
            ("/data/data/com.termux/files/usr/bin/dashboard", 0o700),
            ("/data/data/com.termux/files/home/bin/mobile-gateway", 0o700),
            ("/data/data/com.termux/files/home/bin/agy-runner-loop", 0o755),
            ("/data/data/com.termux/files/usr/local/bin/agy-runner-loop", 0o755),
            ("/data/data/com.termux/files/home/bin/server_supervisor.py", 0o700),
            ("/data/data/com.termux/files/home/bin/battery_guard.sh", 0o700),
            ("/data/data/com.termux/files/home/bin/agy_session.sh", 0o700),
            ("/data/data/com.termux/files/home/bin/termux_session.sh", 0o700),
            ("/data/data/com.termux/files/home/bin/start_suwayomi.sh", 0o700),
            ("/data/data/com.termux/files/home/bin/suwayomi_daily_update.py", 0o700),
            ("/data/data/com.termux/files/home/.local/lib/mobile-server/auth.py", 0o600),
            ("/data/data/com.termux/files/home/.local/lib/mobile-server/limits.py", 0o600),
            ("/data/data/com.termux/files/home/.config/caddy/Caddyfile", 0o600),
            ("/data/data/com.termux/files/home/.config/ttyd/termux_index.html", 0o600),
            ("/data/data/com.termux/files/home/.config/tor/torrc", 0o600),
            ("/data/data/com.termux/files/web/login.html", 0o600),
            ("/data/data/com.termux/files/web/admin.html", 0o600),
        ]
        for remote_path, expected_mode in policy_checks:
            with self.subTest(remote_path=remote_path):
                self.assertEqual(
                    rt.get_canonical_target_mode(remote_path),
                    expected_mode,
                    f"Mode mismatch for {remote_path}: expected {oct(expected_mode)}, got {oct(rt.get_canonical_target_mode(remote_path))}",
                )

    def test_write_all_handling_partial_eintr_and_zero_write(self):
        """Proof: write_all handles partial writes, EINTR retries, and raises on zero-write/error."""
        test_file = self.test_root / "write_all_test.bin"
        payload = b"1234567890ABCDEF"

        # 1. Normal complete write
        fd = os.open(str(test_file), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            rt.write_all(fd, payload)
        finally:
            os.close(fd)
        self.assertEqual(test_file.read_bytes(), payload)

        # 2. Simulated partial writes and EINTR
        real_write = os.write
        call_count = 0

        def mock_write(w_fd, w_buf):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return real_write(w_fd, w_buf[:4])
            elif call_count == 2:
                raise InterruptedError("Interrupted system call")
            elif call_count == 3:
                return real_write(w_fd, w_buf[:6])
            else:
                return real_write(w_fd, w_buf)

        test_file2 = self.test_root / "write_all_test2.bin"
        fd2 = os.open(str(test_file2), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with patch("os.write", side_effect=mock_write):
                rt.write_all(fd2, payload)
        finally:
            os.close(fd2)
        self.assertEqual(test_file2.read_bytes(), payload)

        # 3. Simulated zero-write raises OSError(ENOSPC)
        def mock_zero_write(w_fd, w_buf):
            return 0

        test_file3 = self.test_root / "write_all_test3.bin"
        fd3 = os.open(str(test_file3), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with patch("os.write", side_effect=mock_zero_write):
                with self.assertRaises(OSError) as ctx:
                    rt.write_all(fd3, payload)
            self.assertEqual(ctx.exception.errno, errno.ENOSPC)
        finally:
            os.close(fd3)

    def test_write_atomic_json_never_publishes_corrupt_data(self):
        """Proof: write_atomic_json cleans up temporary files on write failure and never publishes corrupt JSON."""
        target_json = self.test_root / "test_atomic.json"
        target_json.write_text('{"intact": true}\n', encoding="utf-8")

        def mock_fail_write(w_fd, w_buf):
            raise OSError(errno.EIO, "Simulated disk write failure")

        with patch("os.write", side_effect=mock_fail_write):
            with self.assertRaises(OSError):
                rt.write_atomic_json(target_json, {"broken": True})

        # Destination file remains intact and uncorrupted
        self.assertEqual(target_json.read_text(encoding="utf-8"), '{"intact": true}\n')
        # No orphan temporary files in directory
        tmps = list(self.test_root.glob(".tmp_*"))
        self.assertEqual(len(tmps), 0)

    def test_pre_replace_sibling_validation_prevents_partial_overwrite(self):
        """Proof: Sibling size/hash mismatch fails BEFORE os.replace; target stays intact and rollback succeeds."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_bytes = auth_live.read_bytes()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        real_write_all = rt.write_all

        # Short write 16 bytes for auth.py sibling only
        def mock_write_all(w_fd, w_payload):
            if w_payload.startswith(b"# Candidate updated auth module"):
                real_write_all(w_fd, w_payload[:16])
            else:
                real_write_all(w_fd, w_payload)

        with patch.object(rt, "write_all", side_effect=mock_write_all):
            with self.assertRaises(rt.ApplyError) as ctx:
                tx.apply(namespace_root=live_dir)
            self.assertIn("Pre-replace sibling size verification failed", str(ctx.exception))

        # Destination auth.py was NEVER replaced with truncated bytes
        self.assertEqual(auth_live.read_bytes(), orig_auth_bytes)
        # Rollback succeeded cleanly, journal is ROLLED_BACK (not trapped in RECOVERY_REQUIRED)
        self.assertEqual(tx.status()["state"], rt.STATE_ROLLED_BACK)

    def test_fsync_dir_propagates_eio_and_handles_unsupported(self):
        """Proof: fsync_dir propagates EIO/ENOSPC and handles unsupported EINVAL."""
        # 1. EIO propagates
        def mock_fsync_eio(fd):
            raise OSError(errno.EIO, "Input/output error")

        with patch("os.fsync", side_effect=mock_fsync_eio):
            with self.assertRaises(OSError) as ctx:
                rt.fsync_dir(self.test_root)
            self.assertEqual(ctx.exception.errno, errno.EIO)

        # 2. EINVAL is handled cleanly
        def mock_fsync_einval(fd):
            raise OSError(errno.EINVAL, "Invalid argument (not supported)")

        with patch("os.fsync", side_effect=mock_fsync_einval):
            rt.fsync_dir(self.test_root)

    def test_namespace_override_mismatch_rejected(self):
        """Proof: Supplying a namespace root resolving differently from journal is rejected."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        foreign_ns = self.test_root / "foreign_live"
        foreign_ns.mkdir()

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.apply(namespace_root=foreign_ns)
        self.assertIn("does not match journal namespace root", str(ctx.exception))

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.commit(namespace_root=foreign_ns)
        self.assertIn("does not match journal namespace root", str(ctx.exception))

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.recover(namespace_root=foreign_ns)
        self.assertIn("does not match journal namespace root", str(ctx.exception))

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.rollback(namespace_root=foreign_ns)
        self.assertIn("does not match journal namespace root", str(ctx.exception))

    def test_idempotence_and_commit_refuses_drift(self):
        """Proof: Idempotent apply, commit, and recovery refuse live drift fail-closed."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        tx.apply(namespace_root=live_dir)

        # Foreign edit after apply
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        auth_live.write_text("# Foreign edit after apply\n", encoding="utf-8")

        # Calling apply again refuses to claim idempotent_success on drifted files
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("drift", str(ctx.exception).lower())

        # Calling commit refuses to bless drifted release
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.commit(namespace_root=live_dir)
        self.assertIn("drift", str(ctx.exception).lower())

    def test_recover_committed_noop_vs_explicit_rollback(self):
        """Proof: recover(COMMITTED) returns no-op; explicit rollback(COMMITTED) reverts to preimage."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_bytes = auth_live.read_bytes()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        tx.apply(namespace_root=live_dir)
        tx.commit(namespace_root=live_dir)

        # Candidate is installed
        self.assertIn("Candidate updated auth module", auth_live.read_text(encoding="utf-8"))

        # 1. Crash recovery on COMMITTED is a no-op that verifies candidate state
        rec_res = tx.recover(namespace_root=live_dir)
        self.assertEqual(rec_res["state"], rt.STATE_COMMITTED)
        self.assertEqual(rec_res["status"], "idempotent_success")
        self.assertIn("Candidate updated auth module", auth_live.read_text(encoding="utf-8"))

        # 2. Explicit rollback() reverts committed release to original preimage
        rb_res = tx.rollback(namespace_root=live_dir)
        self.assertEqual(rb_res["state"], rt.STATE_ROLLED_BACK)
        self.assertEqual(auth_live.read_bytes(), orig_auth_bytes)
        self.assertEqual(tx.status()["state"], rt.STATE_ROLLED_BACK)

    def test_recovery_preflight_refuses_mode_and_uid_drift(self):
        """Proof: Recovery preflight catches permission (chmod) and owner (UID) drift."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()
        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # Simulate in-progress apply
        journal = tx.load_journal()
        journal["state"] = rt.STATE_APPLYING
        tx.save_journal(journal)

        # 1. Mode drift on auth.py (e.g. chmod 0777)
        os.chmod(auth_live, 0o777)
        with self.assertRaises(rt.RecoveryError) as ctx:
            tx.recover(namespace_root=live_dir)
        self.assertIn("mode/UID drift", str(ctx.exception))

        # Reset mode
        os.chmod(auth_live, 0o600)

        # 2. UID drift: journal expects UID 99999
        journal = tx.load_journal()
        journal["state"] = rt.STATE_APPLYING
        journal["top_uid"] = 99999
        for e in journal["entries"]:
            e["original_uid"] = 99999
        tx.save_journal(journal)

        with self.assertRaises(rt.RecoveryError) as ctx:
            tx.recover(namespace_root=live_dir)
        self.assertIn("drift", str(ctx.exception).lower())

    def test_recovery_failure_updates_journal(self):
        """Proof: Unhandled recovery exception sets RECOVERY_REQUIRED and logs error in journal."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        tx.apply(namespace_root=live_dir)

        real_replace = os.replace

        # Mock os.replace during rollback of live target to simulate I/O failure
        def mock_replace_fail(src, dst):
            if str(live_dir) in str(dst):
                raise OSError(errno.EIO, "Simulated device I/O error during rollback")
            return real_replace(src, dst)

        with patch("os.replace", side_effect=mock_replace_fail):
            with self.assertRaises(rt.RollbackError):
                tx.rollback(namespace_root=live_dir)

        # Journal state reflects failure
        st = tx.status()
        self.assertEqual(st["state"], rt.STATE_RECOVERY_REQUIRED)
        self.assertIn("Simulated device I/O error", st["error"])

    def test_malformed_transaction_paths_rejected(self):
        """Proof: Path traversal or absolute paths in journal references are rejected fail-closed."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        # 1. Traversal in staged_file of a non-untouched candidate entry
        journal = tx.load_journal()
        cand_entry = next(e for e in journal["entries"] if e["action"] != rt.ACTION_UNTOUCHED)
        orig_staged = cand_entry["staged_file"]
        cand_entry["staged_file"] = "../outside/staged.bin"
        tx.save_journal(journal)

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("Refused traversal", str(ctx.exception))

        # Restore staged_file and test absolute path in backup_file
        cand_entry["staged_file"] = orig_staged
        rep_entry = next(e for e in journal["entries"] if e["action"] == rt.ACTION_REPLACE)
        rep_entry["backup_file"] = "/etc/shadow"
        tx.save_journal(journal)

        with self.assertRaises(rt.TransactionError) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("Refused absolute", str(ctx.exception))

    def test_two_release_boot_target_revision(self):
        """Proof: Fresh reviewed preconditions with updated boot target revision are accepted."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture(
            modify_boot_script_in_candidate=True
        )

        # In this scenario, live root already has a "v2" boot script from a previous release
        v2_boot_bytes = b"#!/bin/sh\n# Version 2 boot script\n"
        v2_sha = hashlib.sha256(v2_boot_bytes).hexdigest()
        boot_target_path = live_dir / "home/.termux/boot/start-services.sh"
        boot_target_path.write_bytes(v2_boot_bytes)

        # Fresh reviewed preconditions specify the v2 target hash
        boot_precond = next(f for f in preconds["files"] if f["path"] == rt.KNOWN_BOOT_ALIAS_REMOTE)
        boot_precond["target_sha256"] = v2_sha
        boot_precond["target_bytes"] = len(v2_boot_bytes)

        # Prepare and apply should succeed with fresh reviewed precondition
        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        tx.apply(namespace_root=live_dir)

        # Target updated to candidate
        self.assertIn(b"Candidate boot script", boot_target_path.read_bytes())
        # Symlink intact
        self.assertTrue((live_dir / "home/start_services.sh").is_symlink())

        # Rollback restores v2
        tx.rollback(namespace_root=live_dir)
        self.assertEqual(boot_target_path.read_bytes(), v2_boot_bytes)
        self.assertTrue((live_dir / "home/start_services.sh").is_symlink())

    def test_portable_preimage_contract(self):
        """Proof: Portable contract verification for production preimage metadata and baseline ownership."""
        baseline_path = REPO_ROOT / "config/deployment-files.json"
        self.assertTrue(baseline_path.exists())

        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline_entries = json.load(f)

        self.assertEqual(len(baseline_entries), 35)

        # Contract requirements
        start_services_entry = next((e for e in baseline_entries if e["local"] == "scripts/start-services.sh"), None)
        self.assertIsNotNone(start_services_entry)
        self.assertEqual(start_services_entry["remote"], "/data/data/com.termux/files/home/start_services.sh")
        self.assertEqual(start_services_entry["sha256"], rt.AUDITED_BOOT_SCRIPT_SHA256)

        # Portable verification of tracked baseline properties without host filesystem dependencies
        for entry in baseline_entries:
            self.assertTrue(entry["remote"].startswith(rt.TERMUX_ROOT_PREFIX))
            self.assertIn("sha256", entry)
            self.assertEqual(len(entry["sha256"]), 64)
            canonical_mode = rt.get_canonical_target_mode(entry["remote"])
            self.assertIn(canonical_mode, (0o700, 0o600, 0o755))

    def test_boot_alias_raw_target_drift_refused_before_live_writes(self):
        """Proof: Boot alias raw target drift (e.g. ./.termux vs .termux) refuses before live writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture(
            modify_auth_in_candidate=True
        )

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_bytes = auth_live.read_bytes()

        # Re-target alias symlink to ./.termux/boot/start-services.sh (reproduces root fault probe)
        alias_path = live_dir / "home/start_services.sh"
        alias_path.unlink()
        alias_path.symlink_to("./.termux/boot/start-services.sh")

        with self.assertRaises((rt.ApplyError, rt.PreconditionError)) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("raw link", str(ctx.exception).lower())

        # Assert no live writes occurred: auth.py unmodified, journal remains PREPARED
        self.assertEqual(auth_live.read_bytes(), orig_auth_bytes)
        status = tx.status()
        self.assertEqual(status["state"], rt.STATE_PREPARED)

    def test_boot_alias_recreated_new_inode_refused_before_live_writes(self):
        """Proof: Boot alias symlink recreated with a new inode refuses before live writes."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture(
            modify_auth_in_candidate=True
        )

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        auth_live = live_dir / "home/.local/lib/mobile-server/auth.py"
        orig_auth_bytes = auth_live.read_bytes()

        # Recreate alias symlink with same raw target text, yielding a new inode
        alias_path = live_dir / "home/start_services.sh"
        raw_target = os.readlink(alias_path)
        alias_path.unlink()
        alias_path.symlink_to(raw_target)

        with self.assertRaises((rt.ApplyError, rt.PreconditionError)) as ctx:
            tx.apply(namespace_root=live_dir)
        self.assertIn("inode", str(ctx.exception).lower())

        # Assert no live writes occurred
        self.assertEqual(auth_live.read_bytes(), orig_auth_bytes)
        self.assertEqual(tx.status()["state"], rt.STATE_PREPARED)

    def test_runtime_uid_mismatch_creates_zero_transaction_files(self):
        """Proof: Runtime UID mismatch refuses before any staging/backup/directory creation (0 files)."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        wrong_uid = os.getuid() + 999
        bad_preconds = copy.deepcopy(preconds)
        bad_preconds["uid"] = wrong_uid
        for f in bad_preconds["files"]:
            f["uid"] = wrong_uid

        # tx_dir does not exist yet
        clean_tx_dir = self.test_root / "fresh_tx_zero"
        self.assertFalse(clean_tx_dir.exists())

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=bad_preconds,
                transaction_dir=clean_tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("UID mismatch", str(ctx.exception))

        # Assert zero transaction files or directories were created on disk
        self.assertFalse(clean_tx_dir.exists())

    def test_uid_validation_types_and_values(self):
        """Proof: UID schema rejects booleans, negative integers, non-integers, and mismatches."""
        # Boolean UID rejected
        with self.assertRaises(rt.PreconditionError):
            rt.validate_uid(True, "test_uid")
        with self.assertRaises(rt.PreconditionError):
            rt.validate_uid(False, "test_uid")

        # Negative UID rejected
        with self.assertRaises(rt.PreconditionError):
            rt.validate_uid(-1, "test_uid")

        # String UID rejected
        with self.assertRaises(rt.PreconditionError):
            rt.validate_uid("10505", "test_uid")

        # None rejected
        with self.assertRaises(rt.PreconditionError):
            rt.validate_uid(None, "test_uid")

        # Valid non-negative integer accepted
        self.assertEqual(rt.validate_uid(0), 0)
        self.assertEqual(rt.validate_uid(10505), 10505)

        # parse_preconditions with missing UID on entry and no top_uid raises
        raw_precond = [{"path": "/data/data/com.termux/files/foo.txt", "kind": "file"}]
        with self.assertRaises(rt.PreconditionError):
            rt.parse_preconditions(raw_precond)

        # parse_preconditions with entry UID mismatch vs top_uid raises
        mismatched_precond = {
            "uid": 10505,
            "files": [{"path": "/data/data/com.termux/files/foo.txt", "kind": "file", "uid": 10506}],
        }
        with self.assertRaises(rt.PreconditionError):
            rt.parse_preconditions(mismatched_precond)

    def test_journal_schema_validation_rejects_corrupted_or_invalid(self):
        """Proof: Journal schema validation detects corrupted versions, states, modes, and counts."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        orig_journal = tx.load_journal()

        # 1. Invalid version (e.g. 2 or string)
        bad_j = copy.deepcopy(orig_journal)
        bad_j["version"] = 2
        rt.write_atomic_json(tx.journal_path, bad_j)
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.load_journal()
        self.assertIn("version", str(ctx.exception).lower())

        # 2. Empty entries list
        bad_j = copy.deepcopy(orig_journal)
        bad_j["entries"] = []
        rt.write_atomic_json(tx.journal_path, bad_j)
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.load_journal()
        self.assertIn("entries", str(ctx.exception).lower())

        # 3. Invalid state enum
        bad_j = copy.deepcopy(orig_journal)
        bad_j["state"] = "NON_EXISTENT_STATE"
        rt.write_atomic_json(tx.journal_path, bad_j)
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.load_journal()
        self.assertIn("state", str(ctx.exception).lower())

        # 4. Non-canonical target mode in journal
        bad_j = copy.deepcopy(orig_journal)
        bad_j["entries"][0]["target_mode"] = "0o777"
        rt.write_atomic_json(tx.journal_path, bad_j)
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.load_journal()
        self.assertIn("target mode", str(ctx.exception).lower())

        # 5. Duplicate destination in journal entries
        bad_j = copy.deepcopy(orig_journal)
        bad_j["entries"][1] = dict(bad_j["entries"][0])
        bad_j["candidate_manifest"][1] = dict(bad_j["candidate_manifest"][0])
        rt.write_atomic_json(tx.journal_path, bad_j)
        with self.assertRaises(rt.TransactionError) as ctx:
            tx.load_journal()
        self.assertIn("duplicate", str(ctx.exception).lower())

        # Restore valid journal
        rt.write_atomic_json(tx.journal_path, orig_journal)
        self.assertEqual(tx.load_journal()["version"], rt.JOURNAL_VERSION)

    def test_locked_namespace_change_under_lock_rejected(self):
        """Proof: Namespace root modification detected after acquiring lock raises TransactionError."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        tx = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )

        real_load_journal = tx.load_journal
        call_count = [0]

        def tamper_namespace_on_reload():
            j = real_load_journal()
            call_count[0] += 1
            if call_count[0] > 1:
                # Reload under lock returns drifted namespace
                j["namespace_root"] = str(self.test_root / "tampered_namespace")
            return j

        with patch.object(tx, "load_journal", side_effect=tamper_namespace_on_reload):
            with self.assertRaises(rt.TransactionError) as ctx:
                tx.apply(namespace_root=live_dir)
            self.assertIn("Namespace root changed under lock", str(ctx.exception))

    def test_prepare_refuses_non_empty_tx_dir_preserving_recovery(self):
        """Proof: prepare refuses to overwrite an existing non-empty transaction dir, preserving recovery."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture(
            modify_auth_in_candidate=True
        )

        tx1 = rt.prepare(
            bundle_root=bundle_dir,
            candidate_manifest=manifest,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live_dir,
        )
        self.assertEqual(tx1.status()["state"], rt.STATE_PREPARED)

        # Attempt to prepare again using the same non-empty tx_dir
        with self.assertRaises(rt.TransactionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest,
                preconditions=preconds,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
        self.assertIn("Transaction directory is not empty", str(ctx.exception))

        # Verify initial transaction remains intact and can be applied
        res = tx1.apply(namespace_root=live_dir)
        self.assertEqual(res["state"], rt.STATE_APPLIED)

    def test_prepare_lock_contention_raises_lock_error(self):
        """Proof: prepare acquires NamespaceLock and fails with TransactionLockError if contended."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        clean_tx = self.test_root / "tx_contended"

        # Hold namespace lock externally
        with rt.NamespaceLock(live_dir):
            with self.assertRaises(rt.TransactionLockError) as ctx:
                rt.prepare(
                    bundle_root=bundle_dir,
                    candidate_manifest=manifest,
                    preconditions=preconds,
                    transaction_dir=clean_tx,
                    namespace_root=live_dir,
                )
            self.assertIn("Namespace lock held by another process", str(ctx.exception))

    def test_temporary_file_cleanup_on_early_write_or_fsync_failure(self):
        """Proof: Early write or fsync failure cleans up temporary sibling without orphans."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        dest_file = live_dir / "home/test_cleanup_target.txt"
        dest_file.write_text("original content\n", encoding="utf-8")

        # Mock write_all to fail midway with EIO
        with patch.object(rt, "write_all", side_effect=OSError(errno.EIO, "Injected write error")):
            with self.assertRaises(OSError):
                rt.write_atomic_file(
                    dest_path=dest_file,
                    content=b"new payload\n",
                    mode=0o600,
                    expected_bytes=12,
                )

        # Target file content must be preserved
        self.assertEqual(dest_file.read_text(encoding="utf-8"), "original content\n")

        # No temporary files (.tmp_*) left in destination directory
        tmp_orphans = [p for p in dest_file.parent.glob(".tmp_*")]
        self.assertEqual(tmp_orphans, [])

    def test_write_all_bounded_blocking_io_retry(self):
        """Proof: write_all bounds BlockingIOError retries and propagates error instead of infinite loop."""
        r_fd, w_fd = os.pipe()
        try:
            # Mock os.write to simulate indefinite BlockingIOError
            with patch("os.write", side_effect=BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")):
                with self.assertRaises(BlockingIOError):
                    rt.write_all(w_fd, b"test", max_blocking_retries=3)
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_manifest_non_canonical_mode_rejected_in_prepare(self):
        """Proof: Manifest declaring non-canonical mode is rejected fail-closed before staging."""
        bundle_dir, manifest, preconds, live_dir, tx_dir = self._build_test_fixture()

        # Inject non-canonical mode 0777 into candidate manifest
        bad_manifest = copy.deepcopy(manifest)
        bad_manifest[0]["mode"] = "0o777"

        clean_tx = self.test_root / "tx_mode_fail"

        with self.assertRaises(rt.PreconditionError) as ctx:
            rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=bad_manifest,
                preconditions=preconds,
                transaction_dir=clean_tx,
                namespace_root=live_dir,
            )
        self.assertIn("non-canonical mode", str(ctx.exception))
        self.assertFalse(clean_tx.exists())


if __name__ == "__main__":
    unittest.main()
