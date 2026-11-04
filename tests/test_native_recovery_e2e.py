"""
tests/test_native_recovery_e2e.py: End-to-end integration proof for standalone native recovery.

Verifies:
1. Copied standalone recovery CLI (`recovery_lifecycle/release_activation.py recover`) runs
   in a clean subprocess without repository PYTHONPATH or cwd.
2. Real TermuxLifecycleDriver executes using verified preserved code and native /proc inspection.
3. Candidate process is accurately matched, selected, and terminated (verified dead via /proc).
4. Restored prior disk bytes, modes, and valid syntax are reinstated from transaction backup.
5. Inactive/pre-existing disabled flags and unrelated markers retain inode, mode, and bytes.
6. Temporary service guard flags are cleanly removed.
7. Prior temporary service is relaunched cleanly and verified healthy via process presence.
8. Second invocation of recover CLI demonstrates disk state idempotency.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import scripts.release_activation as ra
import scripts.release_transaction as rt
import scripts.termux_activation as ta
import tests.test_release_activation as tra

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


class NativeRecoveryE2ETest(unittest.TestCase):
    """Decisive native standalone recovery integration test exercising real Termux lifecycle."""

    def test_native_standalone_recovery_quiesces_restores_and_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            test_root = Path(td).resolve()
            bundle_dir, _, _, live_dir, tx_dir = tra._make_fixture(test_root)

            # Ensure namespace layout exists
            home_dir = live_dir / "home"
            usr_dir = live_dir / "usr"
            bin_dir = usr_dir / "bin"
            home_bin = home_dir / "bin"
            disabled_dir = home_dir / ".config/services_disabled"
            state_dir = home_dir / ".config/server_recovery"
            lib_dir = home_dir / ".local/lib/mobile-server"
            tmp_dir = usr_dir / "tmp"
            for p in (bin_dir, home_bin, disabled_dir, state_dir, lib_dir, tmp_dir):
                p.mkdir(parents=True, exist_ok=True)

            # Symlink temporary usr/bin/python3 to sys.executable
            py_symlink = bin_dir / "python3"
            if not py_symlink.exists():
                py_symlink.symlink_to(sys.executable)

            # Align untouched assets (gateway and boot script) to avoid collateral service restarts
            gw_bundle = bundle_dir / ".work/mobile-gateway"
            gw_live = home_bin / "mobile-gateway"
            gw_live.write_bytes(gw_bundle.read_bytes())
            os.chmod(gw_live, 0o700)

            boot_bundle = bundle_dir / "scripts/start-services.sh"
            boot_live = home_dir / ".termux/boot/start-services.sh"
            boot_live.write_bytes(boot_bundle.read_bytes())
            os.chmod(boot_live, 0o700)

            # Synthetic supervisor source: maintained supervisor with services() / healthy() overrides
            # Keeps actual matching_processes, process_identity, terminate, and launch primitives.
            orig_sup_code = (SCRIPTS_DIR / "server_supervisor.py").read_text(encoding="utf-8")
            supervisor_override = """

def services():
    return {
        "dashboard": {
            "port": 8001,
            "path": "/healthz",
            "cmd": [str(PREFIX / "bin/python3"), "-u", str(PREFIX / "bin/dashboard")],
        }
    }

def healthy(sid, cfg):
    # Process-presence health stub: verified via actual matching_processes
    return bool(matching_processes(sid))
"""
            synthetic_sup_source = orig_sup_code + supervisor_override
            live_sup_path = home_bin / "server_supervisor.py"
            live_sup_path.write_text(synthetic_sup_source, encoding="utf-8")
            os.chmod(live_sup_path, 0o755)
            bundle_sup_path = bundle_dir / "scripts/server_supervisor.py"
            bundle_sup_path.write_text(synthetic_sup_source, encoding="utf-8")
            os.chmod(bundle_sup_path, 0o755)

            # Prior live dashboard (valid benign loop)
            prior_dashboard_path = bin_dir / "dashboard"
            prior_dashboard_bytes = (
                b"#!/usr/bin/env python3\n"
                b"# PRIOR_LIVE_DASHBOARD_STUB\n"
                b"import time\n"
                b"while True:\n"
                b"    time.sleep(0.5)\n"
            )
            prior_dashboard_path.write_bytes(prior_dashboard_bytes)
            os.chmod(prior_dashboard_path, 0o755)
            prior_dashboard_mode = stat.S_IMODE(prior_dashboard_path.stat().st_mode)

            # Candidate dashboard (different candidate loop content)
            candidate_dashboard_path = bundle_dir / "app/dashboard.py"
            candidate_dashboard_bytes = (
                b"#!/usr/bin/env python3\n"
                b"# CANDIDATE_DASHBOARD_STUB\n"
                b"import time\n"
                b"while True:\n"
                b"    time.sleep(0.5)\n"
            )
            candidate_dashboard_path.write_bytes(candidate_dashboard_bytes)
            os.chmod(candidate_dashboard_path, 0o755)

            # Candidate deliberately publishes a syntactically broken catalog
            # Normal apply would reject this upfront during candidate preflight;
            # this fixture models recovery from an older or interrupted publication.
            candidate_catalog_path = bundle_dir / "app/service_catalog.py"
            candidate_broken_code = b"def syntax_error_in_candidate(:\n    pass\n"
            candidate_catalog_path.write_bytes(candidate_broken_code)

            # Prior valid service catalog
            prior_catalog_path = lib_dir / "service_catalog.py"
            prior_catalog_bytes = prior_catalog_path.read_bytes()
            prior_catalog_mode = stat.S_IMODE(prior_catalog_path.stat().st_mode)
            compile(prior_catalog_bytes, str(prior_catalog_path), "exec")  # Must be valid syntax

            # Prior auth preimage
            live_auth_path = lib_dir / "auth.py"
            prior_auth_bytes = live_auth_path.read_bytes()

            # Pre-existing disabled marker and unrelated marker in disabled_dir
            pre_disabled_flag = disabled_dir / "syncthing"
            pre_disabled_bytes = b"INTENTIONALLY_DISABLED_PRIOR\n"
            pre_disabled_flag.write_bytes(pre_disabled_bytes)
            os.chmod(pre_disabled_flag, 0o644)
            pre_disabled_stat = pre_disabled_flag.stat()
            orig_disabled_ino = pre_disabled_stat.st_ino
            orig_disabled_mode = stat.S_IMODE(pre_disabled_stat.st_mode)

            unrelated_marker = disabled_dir / "unrelated.marker"
            unrelated_marker_bytes = b"UNRELATED_MARKER_CONTENT\n"
            unrelated_marker.write_bytes(unrelated_marker_bytes)
            os.chmod(unrelated_marker, 0o644)
            unrelated_stat = unrelated_marker.stat()
            orig_unrelated_ino = unrelated_stat.st_ino
            orig_unrelated_mode = stat.S_IMODE(unrelated_stat.st_mode)

            # Regenerate candidate manifest recording the broken catalog and candidate dashboard
            rm = rt._load_release_manifest_module()
            manifest_entries = rm.generate_manifest(bundle_dir, require_gateway=True)

            # Build preconditions accurately from live disk state
            current_uid = os.getuid()
            precond_files = []
            absent_relative_paths = {
                "home/.local/lib/mobile-server/notes.py",
                "home/.local/lib/mobile-server/document_conversion.py",
            }
            for entry in manifest_entries:
                remote_dest = entry["remote"]
                rel_path = remote_dest[len(rt.TERMUX_ROOT_PREFIX):].lstrip("/")
                disk_file = live_dir / rel_path
                if rel_path in absent_relative_paths:
                    precond_files.append({"path": remote_dest, "kind": "absent", "uid": current_uid})
                elif disk_file.is_symlink():
                    st = disk_file.lstat()
                    boot_target = live_dir / "home/.termux/boot/start-services.sh"
                    target_sha, _ = rt.compute_file_sha256(boot_target)
                    precond_files.append({
                        "path": remote_dest,
                        "kind": "symlink",
                        "target": rt.KNOWN_BOOT_TARGET_REMOTE,
                        "raw_link": os.readlink(disk_file),
                        "dev": st.st_dev,
                        "ino": st.st_ino,
                        "target_sha256": target_sha,
                        "target_bytes": boot_target.stat().st_size,
                        "target_mode": "0o700",
                        "uid": current_uid,
                    })
                elif disk_file.exists():
                    sha, size = rt.compute_file_sha256(disk_file)
                    st = disk_file.stat()
                    precond_files.append({
                        "path": remote_dest,
                        "kind": "file",
                        "sha256": sha,
                        "bytes": size,
                        "mode": rt.mode_to_octal_str(stat.S_IMODE(st.st_mode)),
                        "uid": current_uid,
                    })
                else:
                    precond_files.append({"path": remote_dest, "kind": "absent", "uid": current_uid})

            preconditions_data = {"uid": current_uid, "files": precond_files}

            # 1. Real release transaction engine prepare
            tx = rt.prepare(
                bundle_root=bundle_dir,
                candidate_manifest=manifest_entries,
                preconditions=preconditions_data,
                transaction_dir=tx_dir,
                namespace_root=live_dir,
            )
            self.assertEqual(tx.load_journal().get("state"), rt.STATE_PREPARED)

            # 2. Preserve recovery lifecycle code in tx_dir/recovery_lifecycle
            recovery_hashes = ra.preserve_recovery_lifecycle_code(tx_dir, live_dir)
            self.assertEqual(set(recovery_hashes.keys()), set(ra.MANDATORY_RECOVERY_ASSETS))
            ra.validate_recovery_assets(tx_dir / "recovery_lifecycle", recovery_hashes, current_uid)

            # 3. Activation journal & guards setup
            tx_entries = tx.load_journal().get("entries", [])
            affected_services_set, supervisor_affected, _ = ra.map_changed_destinations(tx_entries)
            affected_services = sorted(list(affected_services_set))
            self.assertEqual(affected_services, ["dashboard"])
            self.assertTrue(supervisor_affected)

            uuid_hex = os.urandom(6).hex()
            tx_id = f"act_native_proof_{uuid_hex}"
            act_journal = {
                "version": ra.JOURNAL_VERSION,
                "state": ra.STATE_PREPARED,
                "created_at": time.time(),
                "updated_at": time.time(),
                "tx_id": tx_id,
                "namespace_root": str(live_dir),
                "transaction_dir": str(tx_dir),
                "top_uid": current_uid,
                "affected_services": affected_services,
                "supervisor_affected": supervisor_affected,
                "daemon_was_running": False,
                "flags": {},
                "quiescence_records": {},
                "daemon_record": None,
                "recovery_assets": recovery_hashes,
            }
            ra.save_activation_journal(tx_dir, act_journal)

            active_services, _ = ra.install_guards(
                disabled_dir=disabled_dir,
                affected_services=affected_services,
                tx_id=tx_id,
                uid=current_uid,
                journal=act_journal,
                tx_dir=tx_dir,
            )
            self.assertEqual(active_services, ["dashboard"])
            act_journal["state"] = ra.STATE_GUARDED
            ra.save_activation_journal(tx_dir, act_journal)

            # 4. Apply real candidate files to live namespace
            tx.apply(namespace_root=live_dir)
            self.assertEqual(tx.load_journal().get("state"), rt.STATE_APPLIED)
            act_journal["state"] = ra.STATE_APPLIED
            ra.save_activation_journal(tx_dir, act_journal)

            # Confirm candidate catalog is now on live disk and syntactically broken
            with self.assertRaises(SyntaxError):
                compile(prior_catalog_path.read_bytes(), str(prior_catalog_path), "exec")

            # Newly added helper was written by candidate apply
            notes_live = lib_dir / "notes.py"
            self.assertTrue(notes_live.exists())

            # 5. Start a real temporary candidate dashboard process using live files
            cand_cmd = [str(bin_dir / "python3"), "-u", str(bin_dir / "dashboard")]
            cand_proc = subprocess.Popen(
                cand_cmd,
                cwd=str(home_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            cand_pid = cand_proc.pid
            prior_pid = None
            driver_candidate = None

            try:
                # Allow candidate process to initialize and appear in /proc
                time.sleep(0.2)
                driver_candidate = ta.load_isolated_lifecycle(
                    recovery_dir=tx_dir / "recovery_lifecycle",
                    namespace_root=live_dir,
                    expected_hashes=recovery_hashes,
                )
                cand_ident = driver_candidate.process_identity(cand_pid)
                self.assertIsNotNone(cand_ident, "Candidate process identity must exist in /proc")

                # Verify actual matching_processes finds the candidate dashboard process
                initial_matches = driver_candidate.matching_processes("dashboard")
                self.assertEqual(
                    initial_matches,
                    [(cand_pid, cand_ident)],
                    "Preserved native lifecycle driver must match candidate dashboard",
                )

                # 6. Delete candidate bundle completely
                shutil.rmtree(bundle_dir)
                self.assertFalse(bundle_dir.exists(), "Candidate bundle must be completely removed")

                # 7. Execute copied standalone recovery CLI in a CLEAN subprocess
                # Repository PYTHONPATH and repository cwd are strictly unavailable.
                rec_cli = tx_dir / "recovery_lifecycle/release_activation.py"
                clean_env = {
                    "PATH": os.environ.get("PATH", ""),
                    "USER": os.environ.get("USER", "u0_a505"),
                    "HOME": str(home_dir),
                }
                recover_cmd = [
                    sys.executable,
                    str(rec_cli),
                    "recover",
                    "--transaction-dir",
                    str(tx_dir),
                    "--namespace-root",
                    str(live_dir),
                ]

                proc_run = subprocess.run(
                    recover_cmd,
                    cwd=str(test_root),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(
                    proc_run.returncode,
                    0,
                    f"Standalone recovery failed:\nSTDOUT:\n{proc_run.stdout}\nSTDERR:\n{proc_run.stderr}",
                )

                recovery_output = json.loads(proc_run.stdout.strip())
                self.assertEqual(recovery_output.get("state"), ra.STATE_ROLLED_BACK)
                self.assertEqual(recovery_output.get("status"), "rolled_back")

                # 8. Assert candidate process is gone
                cand_proc.wait(timeout=3.0)
                self.assertIsNotNone(cand_proc.poll(), "Candidate process must terminate during recovery")
                self.assertIsNone(
                    driver_candidate.process_identity(cand_pid),
                    "Candidate process identity must be gone from /proc",
                )

                # 9. Assert prior temporary dashboard was relaunched and is healthy
                driver_post = ta.TermuxLifecycleDriver(namespace_root=live_dir)
                post_matches = driver_post.matching_processes("dashboard")
                self.assertEqual(len(post_matches), 1, "Prior temporary dashboard must be running")
                prior_pid, prior_ident = post_matches[0]
                self.assertNotEqual(prior_pid, cand_pid, "Restored process PID must be distinct from candidate")
                self.assertIsNotNone(prior_ident)
                self.assertTrue(
                    driver_post.healthy("dashboard", driver_post.services()["dashboard"]),
                    "Prior service must report healthy",
                )

                # 10. Verify disk files and modes restored to prior preimage
                self.assertEqual(
                    prior_dashboard_path.read_bytes(),
                    prior_dashboard_bytes,
                    "Prior dashboard script bytes must be restored",
                )
                self.assertEqual(
                    stat.S_IMODE(prior_dashboard_path.stat().st_mode),
                    prior_dashboard_mode,
                    "Prior dashboard mode must be restored",
                )

                self.assertEqual(
                    prior_catalog_path.read_bytes(),
                    prior_catalog_bytes,
                    "Prior service catalog bytes must be restored",
                )
                self.assertEqual(
                    stat.S_IMODE(prior_catalog_path.stat().st_mode),
                    prior_catalog_mode,
                    "Prior service catalog mode must be restored",
                )
                compile(prior_catalog_path.read_bytes(), str(prior_catalog_path), "exec")  # Syntax valid

                self.assertEqual(live_auth_path.read_bytes(), prior_auth_bytes)

                # Created helper absent originally must be removed
                self.assertFalse(notes_live.exists(), "Candidate helper absent originally must be unlinked")

                # Temporary guard flag must be cleanly removed
                dash_guard = disabled_dir / "dashboard"
                self.assertFalse(dash_guard.exists(), "Temporary guard flag must be removed")

                # Pre-existing disabled marker and unrelated marker must retain bytes, mode, inode
                post_disabled_stat = pre_disabled_flag.stat()
                self.assertEqual(post_disabled_stat.st_ino, orig_disabled_ino)
                self.assertEqual(stat.S_IMODE(post_disabled_stat.st_mode), orig_disabled_mode)
                self.assertEqual(pre_disabled_flag.read_bytes(), pre_disabled_bytes)

                post_unrelated_stat = unrelated_marker.stat()
                self.assertEqual(post_unrelated_stat.st_ino, orig_unrelated_ino)
                self.assertEqual(stat.S_IMODE(post_unrelated_stat.st_mode), orig_unrelated_mode)
                self.assertEqual(unrelated_marker.read_bytes(), unrelated_marker_bytes)

                # 11. Repeat recovery CLI to verify idempotent disk state
                proc_repeat = subprocess.run(
                    recover_cmd,
                    cwd=str(test_root),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(proc_repeat.returncode, 0)
                repeat_output = json.loads(proc_repeat.stdout.strip())
                self.assertEqual(repeat_output.get("state"), ra.STATE_ROLLED_BACK)
                self.assertEqual(repeat_output.get("status"), "idempotent_success")

                # Prior process should still be running and healthy
                matches_after_repeat = driver_post.matching_processes("dashboard")
                self.assertEqual(matches_after_repeat, [(prior_pid, prior_ident)])

            finally:
                # Bounded cleanup by captured PID/starttime for both candidate and restored processes
                if cand_proc.poll() is None:
                    try:
                        cand_proc.kill()
                        cand_proc.wait(timeout=2.0)
                    except Exception:
                        pass

                if driver_candidate is not None:
                    # Discover only this temporary script's processes, even if a
                    # failed assertion prevented capturing the restored PID.
                    remaining = driver_candidate.matching_processes("dashboard")
                    driver_candidate.terminate(remaining)
                    surviving = [
                        record for record in remaining
                        if driver_candidate.process_identity(record[0]) == record[1]
                    ]
                    self.assertEqual(surviving, [], "Temporary processes survived identity-checked cleanup")


if __name__ == "__main__":
    unittest.main()
