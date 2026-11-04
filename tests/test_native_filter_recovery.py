#!/usr/bin/env python3
"""
tests/test_native_filter_recovery.py: Native standalone recovery integration proof for suwayomi-filter profile.

Verifies:
1. Copied standalone recovery CLI (`recovery_lifecycle/release_activation.py recover`) runs
   in a clean subprocess without repository PYTHONPATH or cwd.
2. Real TermuxLifecycleDriver executes with verified preserved code and native /proc inspection.
3. Candidate filter process is matched and terminated (verified dead in /proc before prior bytes return).
4. Restored prior disk bytes and 0o755 permissions for suwayomi_graphql_filter.py reinstated.
5. Inactive/pre-existing disabled flags retain inode, mode, and bytes.
6. Temporary service guard flag for suwayomi_filter is cleanly removed.
7. Prior temporary suwayomi_filter process is relaunched cleanly and verified healthy.
8. Second invocation of recover CLI demonstrates idempotency without spurious restarts.
9. Cleanup terminates relaunched prior process cleanly.
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


class NativeFilterRecoveryTest(unittest.TestCase):
    """Native recovery proof for suwayomi-filter profile using real Termux lifecycle driver."""

    def test_native_filter_recovery_quiesces_candidate_restores_prior_and_restarts(self):
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

            # Align optional mobile-server helpers so only filter changes
            for helper in ("notes.py", "document_conversion.py"):
                bundle_helper = bundle_dir / "app" / helper
                live_helper = lib_dir / helper
                if bundle_helper.exists():
                    live_helper.write_bytes(bundle_helper.read_bytes())
                    os.chmod(live_helper, 0o600)

            # Synthetic supervisor source: maintained supervisor with services() / healthy() overrides
            # Keeps actual matching_processes, process_identity, terminate, and launch primitives.
            orig_sup_code = (SCRIPTS_DIR / "server_supervisor.py").read_text(encoding="utf-8")
            supervisor_override = """

def services():
    return {
        "suwayomi_filter": {
            "port": 4566,
            "path": "/healthz",
            "cmd": [str(PREFIX / "bin/python3"), "-u", str(HOME / "bin/suwayomi_graphql_filter.py")],
        }
    }

def healthy(sid, cfg):
    # Process-presence health stub: verified via actual matching_processes
    return bool(matching_processes(sid))
"""
            synthetic_sup_source = orig_sup_code + supervisor_override
            live_sup_path = home_bin / "server_supervisor.py"
            live_sup_path.write_text(synthetic_sup_source, encoding="utf-8")
            os.chmod(live_sup_path, 0o700)
            bundle_sup_path = bundle_dir / "scripts/server_supervisor.py"
            bundle_sup_path.write_text(synthetic_sup_source, encoding="utf-8")
            os.chmod(bundle_sup_path, 0o700)

            # Prior live suwayomi_graphql_filter.py (benign python loop)
            prior_filter_path = home_bin / "suwayomi_graphql_filter.py"
            prior_filter_bytes = (
                b"#!/usr/bin/env python3\n"
                b"# PRIOR_LIVE_SUWAYOMI_FILTER_STUB\n"
                b"import time\n"
                b"while True:\n"
                b"    time.sleep(0.5)\n"
            )
            prior_filter_path.write_bytes(prior_filter_bytes)
            os.chmod(prior_filter_path, 0o755)
            prior_filter_mode = stat.S_IMODE(prior_filter_path.stat().st_mode)

            # Candidate suwayomi_graphql_filter.py (different candidate python loop)
            candidate_filter_path = bundle_dir / "scripts/suwayomi_graphql_filter.py"
            candidate_filter_bytes = (
                b"#!/usr/bin/env python3\n"
                b"# CANDIDATE_SUWAYOMI_FILTER_STUB\n"
                b"import time\n"
                b"while True:\n"
                b"    time.sleep(0.5)\n"
            )
            candidate_filter_path.write_bytes(candidate_filter_bytes)
            os.chmod(candidate_filter_path, 0o755)

            # Pre-existing disabled marker and unrelated marker in disabled_dir
            pre_disabled_flag = disabled_dir / "syncthing"
            pre_disabled_bytes = b"INTENTIONALLY_DISABLED_PRIOR\n"
            pre_disabled_flag.write_bytes(pre_disabled_bytes)
            os.chmod(pre_disabled_flag, 0o644)
            pre_disabled_stat = pre_disabled_flag.stat()
            orig_disabled_ino = pre_disabled_stat.st_ino
            orig_disabled_mode = stat.S_IMODE(pre_disabled_stat.st_mode)

            # Regenerate candidate manifest recording the candidate filter
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

            # 3. Activation journal & guards setup for suwayomi-filter profile
            tx_entries = tx.load_journal().get("entries", [])
            affected_services_set, supervisor_affected, changed_dests = ra.map_changed_destinations(
                tx_entries,
                profile=ra.PROFILE_SUWAYOMI_FILTER,
            )
            affected_services = sorted(list(affected_services_set))
            self.assertEqual(affected_services, ["suwayomi_filter"])
            self.assertFalse(supervisor_affected)
            self.assertEqual(changed_dests, [ra.SUWAYOMI_FILTER_REMOTE])

            uuid_hex = os.urandom(6).hex()
            tx_id = f"act_filter_proof_{uuid_hex}"
            act_journal = {
                "version": ra.JOURNAL_VERSION,
                "profile": ra.PROFILE_SUWAYOMI_FILTER,
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
            self.assertEqual(active_services, ["suwayomi_filter"])
            act_journal["state"] = ra.STATE_GUARDED
            ra.save_activation_journal(tx_dir, act_journal)

            # 4. Apply real candidate files to live namespace
            tx.apply(namespace_root=live_dir)
            self.assertEqual(tx.load_journal().get("state"), rt.STATE_APPLIED)
            act_journal["state"] = ra.STATE_APPLIED
            ra.save_activation_journal(tx_dir, act_journal)

            # Confirm candidate filter is now on live disk
            self.assertEqual(prior_filter_path.read_bytes(), candidate_filter_bytes)

            # 5. Start a real temporary candidate filter process
            cand_cmd = [str(bin_dir / "python3"), "-u", str(home_bin / "suwayomi_graphql_filter.py")]
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
                self.assertIsNotNone(cand_ident, "Candidate filter process identity must exist in /proc")

                # Verify actual matching_processes finds the candidate filter
                initial_matches = driver_candidate.matching_processes("suwayomi_filter")
                self.assertEqual(
                    initial_matches,
                    [(cand_pid, cand_ident)],
                    "Preserved native lifecycle driver must match candidate filter",
                )

                # 6. Delete candidate bundle completely
                shutil.rmtree(bundle_dir)
                self.assertFalse(bundle_dir.exists(), "Candidate bundle must be completely removed")

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

                # 8. Assert candidate process is gone
                cand_proc.wait(timeout=3.0)
                self.assertIsNotNone(cand_proc.poll(), "Candidate filter process must terminate during recovery")
                self.assertIsNone(
                    driver_candidate.process_identity(cand_pid),
                    "Candidate filter process identity must be gone from /proc",
                )

                # 9. Assert prior temporary filter was relaunched and is healthy
                driver_post = ta.TermuxLifecycleDriver(namespace_root=live_dir)
                post_matches = driver_post.matching_processes("suwayomi_filter")
                self.assertEqual(len(post_matches), 1, "Prior temporary filter must be running")
                prior_pid, prior_ident = post_matches[0]
                self.assertNotEqual(prior_pid, cand_pid, "Restored filter PID must be distinct from candidate")
                self.assertIsNotNone(prior_ident)
                self.assertTrue(
                    driver_post.healthy("suwayomi_filter", driver_post.services()["suwayomi_filter"]),
                    "Prior filter must report healthy",
                )

                # 10. Verify disk files and modes restored to prior preimage
                self.assertEqual(
                    prior_filter_path.read_bytes(),
                    prior_filter_bytes,
                    "Prior filter script bytes must be restored",
                )
                self.assertEqual(
                    stat.S_IMODE(prior_filter_path.stat().st_mode),
                    prior_filter_mode,
                    "Prior filter mode must be restored",
                )

                # Pre-existing disabled marker retained
                self.assertTrue(pre_disabled_flag.exists())
                self.assertEqual(pre_disabled_flag.read_bytes(), pre_disabled_bytes)
                curr_dis_stat = pre_disabled_flag.stat()
                self.assertEqual(curr_dis_stat.st_ino, orig_disabled_ino)
                self.assertEqual(stat.S_IMODE(curr_dis_stat.st_mode), orig_disabled_mode)

                # Temporary guard flag must be cleanly removed
                filter_guard = disabled_dir / "suwayomi_filter"
                self.assertFalse(filter_guard.exists(), "Temporary guard flag must be removed")

                # 11. Idempotent repeated recovery
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

                # Prior filter process remains alive and healthy after repeated recovery
                matches_repeat = driver_post.matching_processes("suwayomi_filter")
                self.assertEqual(len(matches_repeat), 1)
                self.assertEqual(matches_repeat[0][0], prior_pid)

            finally:
                # Cleanup: ensure any remaining child processes are killed
                for proc, pid in ((cand_proc, cand_pid), (None, prior_pid)):
                    if pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                if cand_proc.poll() is None:
                    with contextlib.suppress(Exception):
                        cand_proc.kill()
                        cand_proc.wait(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
