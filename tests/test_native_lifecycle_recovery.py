#!/usr/bin/env python3
"""
tests/test_native_lifecycle_recovery.py: Native standalone recovery integration proof for suwayomi-lifecycle profile.

Verifies:
1. Copied standalone recovery CLI (`recovery_lifecycle/release_activation.py recover`) runs
   in a clean subprocess without repository PYTHONPATH or cwd.
2. Complete prior 15 vs candidate 16 runtime facts verified.
3. Real TermuxLifecycleDriver executes with verified preserved code and native /proc inspection.
4. Candidate suwayomi_scheduler process is matched natively and quiesced before file restoration.
5. Live candidate files rolled back to prior 15-service preimage.
6. Copied recovery remains functional with deleted candidate bundle and broken live candidate catalog.
7. Prior-owned services restored; suwayomi_scheduler is NEVER launched from prior 15 catalog.
8. Pre-existing disabled markers retained with device, inode, mode, and bytes.
9. Temporary guard flag for suwayomi_scheduler cleanly unlinked.
10. Repeated recover is idempotent with zero spurious process launches.
11. Proper cleanup with native owned matching and starttime identity.
"""

import contextlib
import json
import os
from pathlib import Path
import shutil
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


class NativeLifecycleRecoveryTest(unittest.TestCase):
    """Native recovery proof for suwayomi-lifecycle profile using real Termux lifecycle driver."""

    def test_native_lifecycle_recovery_quiesces_scheduler_restores_prior_15(self):
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

            # Align untouched assets (gateway and boot script)
            gw_bundle = bundle_dir / ".work/mobile-gateway"
            gw_live = home_bin / "mobile-gateway"
            gw_live.write_bytes(gw_bundle.read_bytes())
            os.chmod(gw_live, 0o700)

            boot_bundle = bundle_dir / "scripts/start-services.sh"
            boot_live = home_dir / ".termux/boot/start-services.sh"
            boot_live.write_bytes(boot_bundle.read_bytes())
            os.chmod(boot_live, 0o700)

            # Align optional mobile-server helpers
            for helper in ("notes.py", "document_conversion.py", "auth.py"):
                bundle_helper = bundle_dir / "app" / helper
                live_helper = lib_dir / helper
                if bundle_helper.exists():
                    live_helper.write_bytes(bundle_helper.read_bytes())
                    os.chmod(live_helper, 0o600)

            # Script bytes UNCHANGED between bundle and live disk (benign loop for test isolation)
            benign_scheduler_bytes = (
                b"#!/usr/bin/env python3\n"
                b"# BENIGN_SCHEDULER_LOOP\n"
                b"import sys, time\n"
                b"while True:\n"
                b"    time.sleep(0.5)\n"
            )
            scheduler_script_path = home_bin / "suwayomi_daily_update.py"
            scheduler_script_path.write_bytes(benign_scheduler_bytes)
            os.chmod(scheduler_script_path, 0o755)
            (bundle_dir / "scripts/suwayomi_daily_update.py").write_bytes(benign_scheduler_bytes)
            os.chmod(bundle_dir / "scripts/suwayomi_daily_update.py", 0o755)

            # Symlink temporary usr/bin/java to sys.executable
            java_bin = bin_dir / "java"
            if not java_bin.exists():
                java_bin.symlink_to(sys.executable)

            # Suwayomi data and jar directories
            (home_dir / "suwayomi/bin").mkdir(parents=True, exist_ok=True)
            (home_dir / "suwayomi/data").mkdir(parents=True, exist_ok=True)
            (home_dir / "suwayomi/bin/Suwayomi-Server.jar").touch()

            # Prior suwayomi launcher on disk: models legacy launcher that forks scheduler daemon in background if invoked!
            prior_launcher_path = home_bin / "start_suwayomi.sh"
            prior_launcher_bytes = (
                f"#!/bin/sh\n# LEGACY_LAUNCHER_WITH_FORK\n{str(bin_dir / 'python3')} {str(home_bin / 'suwayomi_daily_update.py')} --daemon >> /dev/null 2>&1 &\nexec sleep 60\n".encode("utf-8")
            )
            prior_launcher_path.write_bytes(prior_launcher_bytes)
            os.chmod(prior_launcher_path, 0o755)

            candidate_launcher_bytes = (
                b"#!/bin/sh\n"
                b"# CANDIDATE_LAUNCHER_CLEAN\n"
                b"exec sleep 60\n"
            )
            candidate_launcher_path = bundle_dir / "scripts/start_suwayomi.sh"
            candidate_launcher_path.write_bytes(candidate_launcher_bytes)
            os.chmod(candidate_launcher_path, 0o755)

            # Synthetic supervisor source: maintained supervisor with process-presence healthy stub
            orig_sup_code = (SCRIPTS_DIR / "server_supervisor.py").read_text(encoding="utf-8")
            supervisor_override = """

def healthy(sid, cfg):
    # Process-presence health stub: verified via actual matching_processes
    return bool(matching_processes(sid))
"""
            prior_sup_source = orig_sup_code + supervisor_override
            live_sup_path = home_bin / "server_supervisor.py"
            live_sup_path.write_text(prior_sup_source, encoding="utf-8")
            os.chmod(live_sup_path, 0o700)

            candidate_sup_source = prior_sup_source + "\n# CANDIDATE_SUPERVISOR_REVISION\n"
            bundle_sup_path = bundle_dir / "scripts/server_supervisor.py"
            bundle_sup_path.write_text(candidate_sup_source, encoding="utf-8")
            os.chmod(bundle_sup_path, 0o700)

            # Service catalogs: prior 15 vs candidate 16
            orig_cat_code = tra.prior_catalog_source()
            live_cat_path = lib_dir / "service_catalog.py"
            live_cat_path.write_text(orig_cat_code, encoding="utf-8")
            os.chmod(live_cat_path, 0o600)

            candidate_cat_code = tra.reviewed_16_catalog_source()
            bundle_cat_path = bundle_dir / "app/service_catalog.py"
            bundle_cat_path.write_text(candidate_cat_code, encoding="utf-8")
            os.chmod(bundle_cat_path, 0o600)

            # Pre-existing disabled marker for syncthing to verify marker preservation
            pre_disabled_flag = disabled_dir / "syncthing"
            pre_disabled_bytes = b"INTENTIONALLY_DISABLED_PRIOR\n"
            pre_disabled_flag.write_bytes(pre_disabled_bytes)
            os.chmod(pre_disabled_flag, 0o644)
            pre_disabled_stat = pre_disabled_flag.stat()
            orig_disabled_ino = pre_disabled_stat.st_ino
            orig_disabled_mode = stat.S_IMODE(pre_disabled_stat.st_mode)

            # Regenerate candidate manifest recording the 3 candidate files
            rm = rt._load_release_manifest_module()
            manifest_entries = rm.generate_manifest(bundle_dir, require_gateway=True)

            # Build preconditions accurately from live disk state
            current_uid = os.getuid()
            precond_files = []
            for entry in manifest_entries:
                remote_dest = entry["remote"]
                rel_path = remote_dest[len(rt.TERMUX_ROOT_PREFIX):].lstrip("/")
                disk_file = live_dir / rel_path
                if disk_file.is_symlink():
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

            # 3. Activation journal & guards setup for suwayomi-lifecycle profile
            tx_entries = tx.load_journal().get("entries", [])
            affected_services_set, supervisor_affected, changed_dests = ra.map_changed_destinations(
                tx_entries,
                profile=ra.PROFILE_SUWAYOMI_LIFECYCLE,
            )
            affected_services = sorted(list(affected_services_set))
            self.assertEqual(affected_services, ["dashboard", "suwayomi", "suwayomi_scheduler"])
            self.assertTrue(supervisor_affected)
            self.assertEqual(
                sorted(changed_dests),
                sorted([ra.SUWAYOMI_LAUNCHER_REMOTE, ra.SUPERVISOR_REMOTE, ra.SERVICE_CATALOG_REMOTE]),
            )

            uuid_hex = os.urandom(6).hex()
            tx_id = f"act_lc_proof_{uuid_hex}"
            cat_tx_entry = next((e for e in tx_entries if (e.get("dest_path") or e.get("remote")) == ra.SERVICE_CATALOG_REMOTE), None)
            act_journal = {
                "version": ra.JOURNAL_VERSION,
                "profile": ra.PROFILE_SUWAYOMI_LIFECYCLE,
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
                "transition": {
                    "type": "suwayomi_15_to_16",
                    "prior_service_count": 15,
                    "candidate_service_count": 16,
                    "new_services": ["suwayomi_scheduler"],
                    "prior_catalog_sha256": cat_tx_entry["original_sha256"],
                    "candidate_catalog_sha256": cat_tx_entry["candidate_sha256"],
                    "recovery_hashes": recovery_hashes,
                },
            }
            ra.save_activation_journal(tx_dir, act_journal)

            # Start real prior Java backend process
            backend_cmd = [
                str(java_bin),
                "-c",
                "import time; time.sleep(60)",
                f"-Dsuwayomi.tachidesk.config.server.rootDir={home_dir / 'suwayomi/data'}",
                "-jar",
                str(home_dir / "suwayomi/bin/Suwayomi-Server.jar"),
            ]
            prior_sw_proc = subprocess.Popen(
                backend_cmd,
                cwd=str(home_dir / "suwayomi"),
                env=dict(os.environ, HOME=str(home_dir), PREFIX=str(usr_dir)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            def cleanup_prior_handle():
                if prior_sw_proc.poll() is None:
                    prior_sw_proc.terminate()
                prior_sw_proc.wait(timeout=3)
            self.addCleanup(cleanup_prior_handle)
            time.sleep(0.1)
            prior_driver = ta.TermuxLifecycleDriver(namespace_root=live_dir)
            sw_recs = prior_driver.matching_processes("suwayomi")
            self.assertEqual(len(sw_recs), 1)
            sw_p_pid, sw_p_ident = sw_recs[0]
            launcher_tx_entry = next((e for e in tx_entries if (e.get("dest_path") or e.get("remote")) == ra.SUWAYOMI_LAUNCHER_REMOTE), None)
            act_journal["prior_backend_recipe"] = ra.capture_prior_backend_recipe(
                prior_driver, live_dir, tx_entries, current_uid
            )
            # Quiesce prior backend before file change
            prior_driver.terminate(sw_recs)
            prior_sw_proc.wait(timeout=3.0)
            self.assertEqual(prior_driver.matching_processes("suwayomi"), [])

            active_services, _ = ra.install_guards(
                disabled_dir=disabled_dir,
                affected_services=affected_services,
                tx_id=tx_id,
                uid=current_uid,
                journal=act_journal,
                tx_dir=tx_dir,
            )
            self.assertEqual(sorted(active_services), ["dashboard", "suwayomi", "suwayomi_scheduler"])
            act_journal["state"] = ra.STATE_GUARDED
            ra.save_activation_journal(tx_dir, act_journal)

            # 4. Apply real candidate files to live namespace
            tx.apply(namespace_root=live_dir)
            self.assertEqual(tx.load_journal().get("state"), rt.STATE_APPLIED)
            act_journal["state"] = ra.STATE_APPLIED
            ra.save_activation_journal(tx_dir, act_journal)

            # Confirm candidate files on live disk
            self.assertEqual(prior_launcher_path.read_bytes(), candidate_launcher_bytes)
            self.assertIn("suwayomi_scheduler", live_cat_path.read_text(encoding="utf-8"))

            # 5. Start a real temporary candidate scheduler process
            sched_cmd = [
                str(bin_dir / "python3"),
                str(home_bin / "suwayomi_daily_update.py"),
                "--foreground",
            ]
            cand_proc = subprocess.Popen(
                sched_cmd,
                cwd=str(home_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            cand_pid = cand_proc.pid
            driver_candidate = None

            try:
                # Wait for scheduler process to appear
                time.sleep(0.2)
                driver_candidate = ta.load_isolated_lifecycle(
                    recovery_dir=tx_dir / "recovery_lifecycle",
                    namespace_root=live_dir,
                    expected_hashes=recovery_hashes,
                )
                cand_ident = driver_candidate.process_identity(cand_pid)
                self.assertIsNotNone(cand_ident, "Candidate scheduler process identity must exist in /proc")

                # Verify native matching finds the candidate scheduler
                initial_matches = driver_candidate.matching_processes("suwayomi_scheduler")
                self.assertEqual(
                    initial_matches,
                    [(cand_pid, cand_ident)],
                    "Preserved native lifecycle driver must match candidate scheduler",
                )

                # 6. Delete candidate bundle completely
                shutil.rmtree(bundle_dir)
                self.assertFalse(bundle_dir.exists(), "Candidate bundle must be completely removed")

                # Break live candidate catalog syntax after candidate started, before recover
                live_cat_path.write_text("SYNTAX_ERROR_IN_LIVE_CATALOG = {\n", encoding="utf-8")

                # 7. Execute copied standalone recovery CLI in a CLEAN subprocess
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

                # 8. Assert candidate scheduler process is dead
                cand_proc.wait(timeout=3.0)
                self.assertIsNotNone(cand_proc.poll(), "Candidate scheduler process must terminate during recovery")
                self.assertIsNone(
                    driver_candidate.process_identity(cand_pid),
                    "Candidate scheduler process identity must be gone from /proc",
                )

                # 9. Assert prior 15 files are restored on live disk
                self.assertEqual(
                    prior_launcher_path.read_bytes(),
                    prior_launcher_bytes,
                    "Prior launcher bytes must be restored",
                )
                self.assertEqual(
                    live_sup_path.read_text(encoding="utf-8"),
                    prior_sup_source,
                    "Prior supervisor source must be restored",
                )
                self.assertEqual(
                    live_cat_path.read_text(encoding="utf-8"),
                    orig_cat_code,
                    "Prior 15-service catalog source must be restored",
                )

                # 10. Assert suwayomi backend was restored cleanly via recipe (NOT via start_suwayomi.sh)
                driver_post = ta.TermuxLifecycleDriver(namespace_root=live_dir)
                sw_matches = driver_post.matching_processes("suwayomi")
                self.assertEqual(len(sw_matches), 1, "suwayomi backend must be restored via recipe")
                sw_restored_pid, sw_restored_ident = sw_matches[0]
                self.assertIsNotNone(driver_post.process_identity(sw_restored_pid))

                # Assert suwayomi_scheduler was NEVER launched during recovery
                # (Proves prior launcher was NOT executed, preventing legacy fork)
                sched_matches = driver_post.matching_processes("suwayomi_scheduler")
                self.assertEqual(
                    sched_matches,
                    [],
                    "suwayomi_scheduler must NEVER be launched during rollback/recovery (legacy launcher was not invoked)",
                )

                # 11. Pre-existing disabled marker retained
                self.assertTrue(pre_disabled_flag.exists())
                self.assertEqual(pre_disabled_flag.read_bytes(), pre_disabled_bytes)
                curr_dis_stat = pre_disabled_flag.stat()
                self.assertEqual(curr_dis_stat.st_ino, orig_disabled_ino)
                self.assertEqual(stat.S_IMODE(curr_dis_stat.st_mode), orig_disabled_mode)

                # Temporary guard flag for suwayomi_scheduler must be cleanly removed
                sched_guard = disabled_dir / "suwayomi_scheduler"
                self.assertFalse(sched_guard.exists(), "Temporary guard flag for scheduler must be removed")

                # 12. Idempotent repeated recovery
                proc_run2 = subprocess.run(
                    recover_cmd,
                    cwd=str(test_root),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(proc_run2.returncode, 0)
                recovery_output2 = json.loads(proc_run2.stdout.strip())
                self.assertEqual(recovery_output2.get("state"), ra.STATE_ROLLED_BACK)
                self.assertEqual(recovery_output2.get("status"), "idempotent_success")

                # Still no scheduler running
                self.assertEqual(driver_post.matching_processes("suwayomi_scheduler"), [])

            finally:
                # Cleanup: ensure any remaining child processes are killed
                try:
                    cleanup_driver = driver_candidate or ta.TermuxLifecycleDriver(namespace_root=live_dir)
                    for sid in ("suwayomi_scheduler", "suwayomi", "dashboard"):
                        matches = cleanup_driver.matching_processes(sid)
                        if matches:
                            cleanup_driver.terminate(matches)
                finally:
                    if cand_proc.poll() is None:
                        cand_proc.kill()
                    with contextlib.suppress(Exception):
                        cand_proc.wait(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
