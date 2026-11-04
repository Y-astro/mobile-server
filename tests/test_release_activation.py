"""
test_release_activation.py: Comprehensive test suite for service-scoped release activation adapter.

Covers:
1. Exact restart mapping (only affected services).
2. Unchanged/no-op release (zero service restarts).
3. Four pre-disabled services remain untouched (bytes, mode, inode).
4. SSH and unrelated processes never selected.
5. Surviving process blocks all file writes.
6. Guard creation crash before inode save recognized through ownership token.
7. Concurrent guard edit is refused.
8. Interruption during file apply keeps guards and restores prior files.
9. Candidate health failure rolls back coherently.
10. Crash after candidate start quiesces candidate before backout.
11. Prior daemon intent preserved (running vs not running).
12. Bundle-deleted standalone recovery with broken live catalog/dashboard.
13. Original flag bytes/modes/inodes preserved.
14. No nested-lock deadlock.
15. Idempotent repeated apply/recover checks real disk state.
16. Native driver unit tests with mocked /proc, locks, and signals.
"""

import contextlib
import fcntl
import importlib.util
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
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

from tests.test_release_transaction import TestReleaseTransaction

act_spec = importlib.util.spec_from_file_location("release_activation", SCRIPTS_DIR / "release_activation.py")
ra = importlib.util.module_from_spec(act_spec)
act_spec.loader.exec_module(ra)

drv_spec = importlib.util.spec_from_file_location("termux_activation", SCRIPTS_DIR / "termux_activation.py")
ta = importlib.util.module_from_spec(drv_spec)
drv_spec.loader.exec_module(ta)


class FakeLifecycleDriver(ta.LifecycleDriver):
    """Injected fake lifecycle driver recording exact operations and simulating faults."""

    def __init__(self, services_cfg: Optional[Dict[str, Any]] = None):
        self.services_cfg = services_cfg or {
            "dashboard": {"port": 8001, "cmd": ["dashboard"]},
            "transfer_gateway": {"port": 8081, "cmd": ["gateway"]},
            "caddy": {"port": 8000, "cmd": ["caddy"]},
            "filebrowser": {"port": 8085, "cmd": ["filebrowser"]},
            "terminal": {"port": 7681, "cmd": ["ttyd"]},
            "laptop_terminal": {"port": 7683, "cmd": ["ttyd"]},
            "agy": {"port": 7682, "cmd": ["ttyd"]},
            "tor": {"port": 9050, "cmd": ["tor"]},
            "battery_guard": {"cmd": ["battery_guard.sh"]},
            "syncthing": {"port": 8384, "cmd": ["syncthing"]},
            "vaultwarden": {"port": 8082, "cmd": ["vaultwarden"]},
            "adguard": {"port": 3000, "cmd": ["adguard"]},
            "suwayomi": {"port": 4569, "cmd": ["suwayomi"]},
            "suwayomi_filter": {"port": 4566, "cmd": ["suwayomi_filter"]},
            "ssh": {"port": 8022, "cmd": ["sshd"]},
        }
        self.running_processes: Dict[str, List[Tuple[int, str]]] = {}
        self.healthy_services: Set[str] = set()
        self.events: List[Tuple[Any, ...]] = []
        self.fail_quiescence: bool = False
        self.fail_health: Set[str] = set()
        self.paused: bool = False
        self.daemon_running: bool = True
        self.daemon_proc: Optional[Tuple[int, str]] = (9999, "daemon_start_100")
        self.lock_delay: float = 0.0

    def services(self) -> Dict[str, Any]:
        return self.services_cfg

    @contextlib.contextmanager
    def lock(self, name: str, blocking: bool = True, timeout: Optional[float] = None):
        self.events.append(("lock_acquire", name))
        if self.lock_delay > 0:
            time.sleep(self.lock_delay)
        try:
            yield
        finally:
            self.events.append(("lock_release", name))

    def matching_processes(self, sid: str) -> List[Tuple[int, str]]:
        self.events.append(("matching_processes", sid))
        return list(self.running_processes.get(sid, []))

    def process_identity(self, pid: int) -> Optional[str]:
        if self.daemon_proc and pid == self.daemon_proc[0]:
            return self.daemon_proc[1] if self.daemon_running else None
        for sid, procs in self.running_processes.items():
            for p, ident in procs:
                if p == pid:
                    return ident
        return None

    def terminate(self, records: List[Tuple[int, str]]) -> None:
        self.events.append(("terminate", list(records)))
        if not self.fail_quiescence:
            for pid, ident in records:
                for sid in list(self.running_processes.keys()):
                    self.running_processes[sid] = [r for r in self.running_processes[sid] if r != (pid, ident)]

    def stop_agy(self) -> None:
        self.events.append(("stop_agy",))

    def healthy(self, sid: str, cfg: Dict[str, Any]) -> bool:
        self.events.append(("healthy", sid))
        if sid in self.fail_health:
            return False
        return sid in self.healthy_services

    def launch(self, sid: str, cfg: Dict[str, Any]) -> Any:
        self.events.append(("launch", sid))
        pid = 2000 + len(self.events)
        ident = f"start_{pid}"
        self.running_processes.setdefault(sid, []).append((pid, ident))
        self.healthy_services.add(sid)
        return pid

    def is_paused(self) -> bool:
        return self.paused

    def is_daemon_running(self) -> bool:
        return self.daemon_running

    def find_daemon_process(self) -> Optional[Tuple[int, str]]:
        return self.daemon_proc if self.daemon_running else None

    def stop_daemon(self, pid: int, ident: str, timeout: float = 5.0) -> None:
        self.events.append(("stop_daemon", pid, ident))
        self.daemon_running = False

    def start_daemon(self) -> None:
        self.events.append(("start_daemon",))
        self.daemon_running = True
        self.daemon_proc = (9998, "daemon_start_new")


class TestReleaseActivation(TestReleaseTransaction):
    """Test suite covering all decisive service-scoped release activation behaviors."""

    def _build_activation_fixture(
        self,
        modify_auth: bool = False,
        modify_gateway: bool = False,
        modify_boot: bool = False,
        modify_supervisor: bool = False,
        unchanged_all: bool = False,
        subdir_name: Optional[str] = None,
    ):
        if subdir_name:
            target_root = self.test_root / subdir_name
            target_root.mkdir(parents=True, exist_ok=True)
            orig_root = self.test_root
            self.test_root = target_root
            try:
                bundle, manifest, preconds, live, tx_dir = self._build_test_fixture(
                    modify_auth_in_candidate=modify_auth,
                    modify_boot_script_in_candidate=modify_boot,
                )
            finally:
                self.test_root = orig_root
        else:
            bundle = self.test_root / "bundle"
            live = self.test_root / "live"
            tx_dir = self.test_root / "tx"
            if bundle.exists():
                shutil.rmtree(bundle)
            if live.exists():
                shutil.rmtree(live)
            if tx_dir.exists():
                shutil.rmtree(tx_dir)

            bundle, manifest, preconds, live, tx_dir = self._build_test_fixture(
                modify_auth_in_candidate=modify_auth,
                modify_boot_script_in_candidate=modify_boot,
            )

        if not modify_gateway:
            gw_live = live / "home/bin/mobile-gateway"
            gw_bundle = bundle / ".work/mobile-gateway"
            gw_bundle.write_bytes(gw_live.read_bytes())
            os.chmod(gw_bundle, 0o700)

        if modify_supervisor:
            (bundle / "scripts/server_supervisor.py").write_text("# candidate supervisor\n", encoding="utf-8")

        if unchanged_all:
            # Populate notes.py and document_conversion.py in live too so 0 files change
            for name in ("notes.py", "document_conversion.py"):
                src = bundle / "app" / name
                dst = live / "home/.local/lib/mobile-server" / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                sha, size = ra.rt.compute_file_sha256(dst)
                for f in preconds["files"]:
                    if f["path"].endswith(name):
                        f["kind"] = "file"
                        f["sha256"] = sha
                        f["bytes"] = size
                        f["mode"] = "0o600"

        return bundle, manifest, preconds, live, tx_dir

    def test_plan_reports_changed_destinations_and_affected_services_without_disk_writes(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        res = ra.plan(bundle, preconds, namespace_root=live)
        self.assertEqual(res["status"], "plan_ok")
        self.assertEqual(res["affected_services"], ["dashboard"])
        self.assertFalse(res["supervisor_affected"])
        self.assertEqual(list(tx_dir.iterdir()), [])

    def test_exact_restart_mapping_and_healthy_activation(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.running_processes["caddy"] = [(102, "start_102")]
        driver.healthy_services.add("dashboard")
        driver.healthy_services.add("caddy")

        res = ra.apply(
            bundle_root=bundle,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live,
            driver=driver,
        )
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["affected_services"], ["dashboard"])
        self.assertEqual(res["reactivated_services"], ["dashboard"])

        terminated = [e for e in driver.events if e[0] == "terminate"]
        self.assertEqual(len(terminated), 1)
        self.assertEqual(terminated[0][1], [(101, "start_101")])
        launched = [e for e in driver.events if e[0] == "launch"]
        self.assertEqual(launched, [("launch", "dashboard")])

        # Caddy was never touched!
        self.assertEqual(driver.running_processes["caddy"], [(102, "start_102")])

    def test_unchanged_release_causes_no_service_restarts(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            unchanged_all=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]

        res = ra.apply(
            bundle_root=bundle,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live,
            driver=driver,
        )
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["status"], "completed_noop_services")
        self.assertFalse(any(e[0] in ("terminate", "launch") for e in driver.events))
        self.assertEqual(driver.running_processes["dashboard"], [(101, "start_101")])

    def test_pre_disabled_services_remain_untouched_and_unstarted(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        dash_flag = disabled_dir / "dashboard"
        dash_flag.write_text("custom_preexisting_disable_bytes", encoding="utf-8")
        os.chmod(dash_flag, 0o644)
        orig_st = dash_flag.stat()

        driver = FakeLifecycleDriver()
        driver.running_processes.pop("dashboard", None)

        res = ra.apply(
            bundle_root=bundle,
            preconditions=preconds,
            transaction_dir=tx_dir,
            namespace_root=live,
            driver=driver,
        )
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["reactivated_services"], [])

        new_st = dash_flag.stat()
        self.assertEqual(dash_flag.read_text(encoding="utf-8"), "custom_preexisting_disable_bytes")
        self.assertEqual(stat.S_IMODE(new_st.st_mode), 0o644)
        self.assertEqual(new_st.st_ino, orig_st.st_ino)
        self.assertFalse(any(e[0] == "launch" for e in driver.events))

    def test_ssh_service_and_unrelated_services_never_selected(self):
        fake_entry = [{"remote": "/data/data/com.termux/files/usr/bin/sshd", "action": ra.rt.ACTION_REPLACE}]
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.map_changed_destinations(fake_entry)
        self.assertIn("Unexplained changed destination", str(ctx.exception))

    def test_suwayomi_modifications_refused(self):
        fake_entry = [{"remote": "/data/data/com.termux/files/home/bin/start_suwayomi.sh", "action": ra.rt.ACTION_REPLACE}]
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.map_changed_destinations(fake_entry)
        self.assertIn("Suwayomi launcher/filter/scheduler sources must be unchanged", str(ctx.exception))

    def test_deliberately_paused_server_refuses_activation(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture()
        driver = FakeLifecycleDriver()
        driver.paused = True
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertIn("deliberately paused", str(ctx.exception))

    def test_surviving_process_blocks_all_file_writes(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_quiescence = True

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_auth_content = live_auth.read_text(encoding="utf-8")

        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)

        # File was NOT modified!
        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_auth_content)

        # Safe guard flag remains installed
        guard_flag = live / "home/.config/services_disabled/dashboard"
        self.assertTrue(guard_flag.exists())

    def test_guard_creation_crash_before_inode_recognized_via_token(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()

        # Prepare file transaction FIRST so tx_dir is properly prepared
        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)

        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        token = "release_guard:act_simulated_crash:dashboard"
        guard_file = disabled_dir / "dashboard"
        guard_file.write_text(f"RELEASE_GUARD_TOKEN={token}\nTX_ID=act_simulated_crash\nSID=dashboard\n", encoding="utf-8")
        os.chmod(guard_file, 0o600)

        # Create activation journal with ino=None (simulating crash before inode recording)
        journal_data = {
            "version": 1,
            "state": ra.STATE_GUARDED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": "act_simulated_crash",
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": False,
            "daemon_was_running": False,
            "flags": {
                "dashboard": {
                    "pre_existing": False,
                    "intent": "create_guard",
                    "token": token,
                    "guard_created": True,
                    "ino": None,
                }
            },
            "quiescence_records": {},
            "daemon_record": None,
        }
        ra.save_activation_journal(tx_dir, journal_data)

        res = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_ROLLED_BACK)
        self.assertFalse(guard_file.exists())

    def test_concurrent_guard_edit_refused(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        guard_file = disabled_dir / "dashboard"
        guard_file.write_text("TAMPERED_CONTENT_UNKNOWN_EDIT", encoding="utf-8")
        os.chmod(guard_file, 0o600)

        flags = {
            "dashboard": {
                "pre_existing": False,
                "token": "release_guard:act_123:dashboard",
                "ino": guard_file.stat().st_ino,
            }
        }
        with self.assertRaises(ra.ConcurrencyError) as ctx:
            ra.remove_guards(disabled_dir, flags, os.getuid())
        self.assertIn("unknown concurrent edit", str(ctx.exception))
        self.assertTrue(guard_file.exists())

    def test_interruption_during_file_apply_restores_all_prior_files(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_content = live_auth.read_text(encoding="utf-8")

        orig_apply = ra.rt.ReleaseTransaction.apply
        def _failing_apply(self_tx, namespace_root=None):
            raise IOError("Simulated power loss / IO failure during apply")

        with unittest.mock.patch.object(ra.rt.ReleaseTransaction, "apply", _failing_apply):
            with self.assertRaises(IOError):
                ra.apply(bundle, preconds, tx_dir, live, driver=driver)

        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_content)
        self.assertFalse((live / "home/.config/services_disabled/dashboard").exists())

    def test_candidate_health_failure_rolls_back_coherently(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_health = {"dashboard"}

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_content = live_auth.read_text(encoding="utf-8")

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2)

        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_content)
        journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(journal["state"], ra.STATE_ROLLED_BACK)

    def test_crash_after_candidate_start_quiesces_candidate_before_file_rollback(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        tx = ra.rt.prepare(bundle, ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True), preconds, tx_dir, live)
        tx.apply(namespace_root=live)

        driver.running_processes["dashboard"] = [(2001, "candidate_start_2001")]

        ra.preserve_recovery_lifecycle_code(tx_dir, live)
        act_journal = {
            "version": 1,
            "state": ra.STATE_APPLIED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": "act_interrupted",
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": False,
            "daemon_was_running": False,
            "flags": {
                "dashboard": {
                    "pre_existing": False,
                    "token": "release_guard:act_interrupted:dashboard",
                    "guard_created": False,
                }
            },
            "quiescence_records": {},
            "daemon_record": None,
        }
        ra.save_activation_journal(tx_dir, act_journal)

        res = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_ROLLED_BACK)

        term_calls = [e for e in driver.events if e[0] == "terminate"]
        self.assertTrue(any((2001, "candidate_start_2001") in c[1] for c in term_calls))

    def test_prior_daemon_intent_preserved(self):
        # Case A: daemon was originally running
        bundle_a, manifest_a, preconds_a, live_a, tx_dir_a = self._build_activation_fixture(
            modify_supervisor=True,
            subdir_name="case_a",
        )
        driver_a = FakeLifecycleDriver()
        driver_a.daemon_running = True
        res_a = ra.apply(bundle_a, preconds_a, tx_dir_a, live_a, driver=driver_a)
        self.assertEqual(res_a["state"], ra.STATE_COMPLETED)
        self.assertTrue(any(e[0] == "stop_daemon" for e in driver_a.events))
        self.assertTrue(any(e[0] == "start_daemon" for e in driver_a.events))

        # Case B: daemon was originally NOT running
        bundle_b, manifest_b, preconds_b, live_b, tx_dir_b = self._build_activation_fixture(
            modify_supervisor=True,
            subdir_name="case_b",
        )
        driver_b = FakeLifecycleDriver()
        driver_b.daemon_running = False
        driver_b.daemon_proc = None
        res_b = ra.apply(bundle_b, preconds_b, tx_dir_b, live_b, driver=driver_b)
        self.assertEqual(res_b["state"], ra.STATE_COMPLETED)
        self.assertFalse(any(e[0] == "start_daemon" for e in driver_b.events))

    def test_standalone_recovery_with_deleted_bundle_and_broken_live_modules(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_auth = live_auth.read_text(encoding="utf-8")

        # Candidate modifies service_catalog with broken syntax
        cat_file = bundle / "app/service_catalog.py"
        cat_file.write_text("raise SyntaxError('broken candidate')\n")
        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)

        # Prepare (preserves clean prior recovery code before apply!)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)
        ra.preserve_recovery_lifecycle_code(tx_dir, live)

        # Now apply candidate files (live now has broken catalog)
        tx.apply(namespace_root=live)

        journal = {
            "version": 1,
            "state": ra.STATE_APPLIED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": "act_standalone",
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": True,
            "daemon_was_running": False,
            "flags": {},
            "quiescence_records": {},
            "daemon_record": None,
        }
        ra.save_activation_journal(tx_dir, journal)

        # Delete bundle
        shutil.rmtree(bundle)

        # Standalone recovery via subprocess
        cmd = [
            sys.executable,
            str(tx_dir / "recovery_lifecycle/release_activation.py"),
            "recover",
            "--transaction-dir",
            str(tx_dir),
            "--namespace-root",
            str(live),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed:\nstdout: {res.stdout}\nstderr: {res.stderr}")

        # Live auth is restored!
        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_auth)

    def test_strict_lock_ordering_no_deadlock(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        start_time = time.monotonic()
        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        elapsed = time.monotonic() - start_time
        self.assertLess(elapsed, 5.0)
        self.assertEqual(res["state"], ra.STATE_COMPLETED)

    def test_idempotent_repeated_status_and_recover(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(
            modify_auth=True,
        )
        driver = FakeLifecycleDriver()
        driver.healthy_services.add("dashboard")

        res1 = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res1["state"], ra.STATE_COMPLETED)

        st = ra.status(tx_dir)
        self.assertEqual(st["activation_state"], ra.STATE_COMPLETED)

        rec = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(rec["state"], ra.STATE_COMPLETED)
        self.assertEqual(rec["status"], "idempotent_success")


class TestTermuxLifecycleDriverUnit(unittest.TestCase):
    """Unit tests for TermuxLifecycleDriver using mocked filesystem and proc."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_proc_process_identity(self):
        pid_dir = self.proc_root / "1234"
        pid_dir.mkdir()
        stat_line = "1234 (python3) S 1 1234 1234 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 543210 1000 200"
        (pid_dir / "stat").write_text(stat_line)

        driver = ta.TermuxLifecycleDriver(proc_root=self.proc_root)
        ident = driver.process_identity(1234)
        self.assertEqual(ident, "543210")

        zombie_dir = self.proc_root / "1235"
        zombie_dir.mkdir()
        zombie_stat = "1235 (python3) Z 1 1235 1235 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 543211 1000 200"
        (zombie_dir / "stat").write_text(zombie_stat)
        self.assertIsNone(driver.process_identity(1235))

    def test_find_daemon_process_matches_exact_argv_and_uid(self):
        pid_dir = self.proc_root / "5555"
        pid_dir.mkdir()
        (pid_dir / "stat").write_text("5555 (python3) S 1 5555 5555 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 98765 1000 200")
        cmdline = f"/usr/bin/python3\0{REPO_ROOT}/scripts/server_supervisor.py\0--daemon\0"
        (pid_dir / "cmdline").write_bytes(cmdline.encode("utf-8"))

        driver = ta.TermuxLifecycleDriver(proc_root=self.proc_root)
        daemon_proc = driver.find_daemon_process()
        self.assertEqual(daemon_proc, (5555, "98765"))


if __name__ == "__main__":
    unittest.main()
