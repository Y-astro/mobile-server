"""
test_release_activation.py: Focused test suite for service-scoped release activation adapter.

Covers:
1. Exact restart mapping (only affected services).
2. Unchanged/no-op release (zero service restarts).
3. Four pre-disabled services remain untouched (bytes, mode, inode, mtime).
4. SSH and unrelated processes never selected.
5. AGY launcher/runner/ttyd runtime-asset changes rejected fail-closed.
6. Surviving process blocks all file writes.
7. Guard creation crash before inode save recognized through ownership token.
8. Concurrent guard edit refused (content and mode tamper).
9. Pre-existing flag deletion and modification detected before tx.apply.
10. Interruption during file apply keeps guards and restores prior files.
11. Candidate health failure rolls back coherently.
12. Candidate survivor during rollback blocks tx.rollback and leaves guards.
13. Crash after candidate start quiesces candidate before backout.
14. Prior daemon intent preserved (running vs not running).
15. Decoy daemon rejection and ambiguous daemon refusal (multiple matching daemons).
16. Daemon lock held without matching PID refused fail-closed.
17. Preserved live daemon launch path restarts live script.
18. Standalone recovery with deleted bundle and broken live helpers.
19. Recovery asset hash corruption refusal before code execution.
20. Journal corruption / affected-services mismatch refusal.
21. Prior-service health failure during rollback retains recovery required state.
22. Strict lock ordering without deadlock.
23. Idempotent repeated apply/recover checks real disk state.
24. Native driver unit tests with mocked /proc, locks, and namespace path binding.
"""

import contextlib
import copy
import fcntl
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
import time
import unittest
from unittest import mock
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

def reviewed_16_catalog_source():
    """Construct the reviewed 16-service catalog by removing only the antra service."""
    source = (REPO_ROOT / "app/service_catalog.py").read_text()
    source = source.replace("    'antra': {},\n", "", 1)
    source = source.replace("        'antra': dict(cmd=[f'{p}/bin/python3', '-u', f'{h}/.local/lib/mobile-server/antra_worker.py']),\n", "", 1)
    old_end = '''            "open_url": "javascript:alert('Battery Guard monitors readings and sends alerts; it cannot control charging hardware.');",
            "open_label": "Battery Monitor",
            "can_toggle": True
        }
    }'''
    new_part = '''            "open_url": "javascript:alert('Battery Guard monitors readings and sends alerts; it cannot control charging hardware.');",
            "open_label": "Battery Monitor",
            "can_toggle": True
        },
        "antra": {
            "id": "antra",
            "name": "Antra Music",
            "category": "Media",
            "port": None,
            "pattern": "antra_worker",
            "icon": "music",
            "desc": "High-Resolution Music Downloader (FLAC 24-bit)",
            "open_url": "/antra/",
            "open_label": "Open Antra",
            "can_toggle": True
        }
    }'''
    source = source.replace(new_part, old_end, 1)
    assert hashlib.sha256(source.encode()).hexdigest() == ra.SUWAYOMI_FORK_FREE_CATALOG_SHA256
    return source


def prior_catalog_source():
    """Construct the reviewed prior 15 catalog by removing suwayomi_scheduler from the 16 catalog."""
    source = reviewed_16_catalog_source()
    line = "        'suwayomi_scheduler': dict(cmd=[f'{p}/bin/python3', f'{h}/bin/suwayomi_daily_update.py', '--foreground']),\n"
    assert source.count(line) == 1
    return source.replace(line, "", 1)


def install_prior_catalog(live, preconds):
    catalog = live / "home/.local/lib/mobile-server/service_catalog.py"
    catalog.write_text(prior_catalog_source())
    os.chmod(catalog, 0o600)
    sha, size = ra.rt.compute_file_sha256(catalog)
    entry = next(f for f in preconds["files"] if f["path"] == ra.SERVICE_CATALOG_REMOTE)
    entry.update(kind="file", sha256=sha, bytes=size, mode="0o600")


def _make_fixture(test_root: Path, modify_auth: bool = False, modify_boot: bool = False):
    import tests.test_release_transaction as trt
    builder = trt.TestReleaseTransaction()
    builder.test_root = test_root
    res = builder._build_test_fixture(
        modify_auth_in_candidate=modify_auth,
        modify_boot_script_in_candidate=modify_boot,
    )
    return res

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
        self.fail_rollback_quiescence: bool = False
        self.fail_candidate_health: Set[str] = set()
        self.fail_prior_health: Set[str] = set()
        self.launch_counts: Dict[str, int] = {}
        self.paused: bool = False
        self.daemon_running: bool = True
        self.daemon_proc: Optional[Tuple[int, str]] = (9999, "daemon_start_100")
        self.fail_find_daemon: bool = False

    def services(self) -> Dict[str, Any]:
        return self.services_cfg

    @contextlib.contextmanager
    def lock(self, name: str, blocking: bool = True, timeout: Optional[float] = None):
        self.events.append(("lock_acquire", name))
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
        for pid, ident in records:
            if pid < 2000 and self.fail_quiescence:
                continue
            if pid >= 2000 and self.fail_rollback_quiescence:
                continue
            for sid in list(self.running_processes.keys()):
                self.running_processes[sid] = [r for r in self.running_processes[sid] if r != (pid, ident)]

    def stop_agy(self) -> None:
        self.events.append(("stop_agy",))

    def healthy(self, sid: str, cfg: Dict[str, Any]) -> bool:
        self.events.append(("healthy", sid))
        count = self.launch_counts.get(sid, 0)
        if count <= 1:
            if sid in self.fail_candidate_health:
                return False
        else:
            if sid in self.fail_prior_health:
                return False
        return sid in self.healthy_services

    def launch(self, sid: str, cfg: Dict[str, Any]) -> Any:
        self.events.append(("launch", sid))
        self.launch_counts[sid] = self.launch_counts.get(sid, 0) + 1
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
        if self.fail_find_daemon:
            return None
        return self.daemon_proc if self.daemon_running else None

    def stop_daemon(self, pid: int, ident: str, timeout: float = 5.0) -> None:
        self.events.append(("stop_daemon", pid, ident))
        self.daemon_running = False

    def start_daemon(self) -> None:
        self.events.append(("start_daemon",))
        self.daemon_running = True
        self.daemon_proc = (9998, "daemon_start_new")

    def reload_lifecycle(self) -> None:
        self.events.append(("reload_lifecycle",))


class TestReleaseActivation(unittest.TestCase):
    """Decisive test suite for service-scoped release activation adapter."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

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
            target_root = self.test_root

        bundle, manifest, preconds, live, tx_dir = _make_fixture(
            test_root=target_root,
            modify_auth=modify_auth,
            modify_boot=modify_boot,
        )

        if not modify_gateway:
            gw_live = live / "home/bin/mobile-gateway"
            gw_bundle = bundle / ".work/mobile-gateway"
            gw_bundle.write_bytes(gw_live.read_bytes())
            os.chmod(gw_bundle, 0o700)

        if modify_supervisor:
            (bundle / "scripts/server_supervisor.py").write_text("# candidate supervisor\n", encoding="utf-8")

        if unchanged_all:
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

    def _build_lifecycle_fixture(self):
        result = self._build_activation_fixture(unchanged_all=True)
        bundle, _, preconds, live, _ = result
        install_prior_catalog(live, preconds)
        (bundle / "app/service_catalog.py").write_text(reviewed_16_catalog_source(), encoding="utf-8")
        (bundle / "scripts/start_suwayomi.sh").write_text("# candidate launcher\n")

        # Controller tests simulate processes. Native capture and direct launch are
        # exercised separately by the copied-recovery subprocess integration proof.
        def capture(driver, namespace, entries, uid):
            return {
                "pid": 1001, "identity": "sw_1001",
                "argv": [str(namespace / "usr/bin/java"),
                         f"-Dsuwayomi.tachidesk.config.server.rootDir={namespace / 'home/suwayomi/data'}",
                         "-jar", str(namespace / "home/suwayomi/bin/Suwayomi-Server.jar")],
                "cwd": str(namespace / "home/suwayomi"),
                "env": {"HOME": str(namespace / "home"), "PREFIX": str(namespace / "usr")},
                **{field: next(e["original_sha256"] for e in entries if e["remote"] == remote)
                   for remote, field in ((ra.SUWAYOMI_LAUNCHER_REMOTE, "prior_launcher_sha256"),
                                         (ra.SERVICE_CATALOG_REMOTE, "prior_catalog_sha256"))},
            }
        patcher = mock.patch.object(ra, "capture_prior_backend_recipe", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ra, "_launch_prior_backend_from_recipe",
                                    side_effect=lambda recipe, driver, ns_root, tx_journal:
                                    driver.launch("suwayomi", driver.services()["suwayomi"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        def reload_lifecycle(driver):
            driver.events.append(("reload_lifecycle",))
            catalog = ra.load_catalog_services_from_bytes(
                (live / "home/.local/lib/mobile-server/service_catalog.py").read_bytes(),
                str(live / "home"), str(live / "usr"), "unit_reload")
            if "suwayomi_scheduler" in catalog:
                driver.services_cfg["suwayomi_scheduler"] = catalog["suwayomi_scheduler"]
            else:
                driver.services_cfg.pop("suwayomi_scheduler", None)
        patcher = mock.patch.object(FakeLifecycleDriver, "reload_lifecycle", reload_lifecycle)
        patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _build_jar_fixture(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)

        # Ensure launcher on live disk matches reviewed fork-free launcher
        launcher_src = (REPO_ROOT / "scripts/start_suwayomi.sh").read_bytes()
        live_launcher = live / "home/bin/start_suwayomi.sh"
        live_launcher.parent.mkdir(parents=True, exist_ok=True)
        live_launcher.write_bytes(launcher_src)
        os.chmod(live_launcher, 0o755)

        bundle_launcher = bundle / "scripts/start_suwayomi.sh"
        bundle_launcher.write_bytes(launcher_src)
        os.chmod(bundle_launcher, 0o755)

        l_sha, l_size = ra.rt.compute_file_sha256(live_launcher)
        for f in preconds["files"]:
            if f["path"] == ra.SUWAYOMI_LAUNCHER_REMOTE:
                f["kind"] = "file"
                f["sha256"] = l_sha
                f["bytes"] = l_size
                f["mode"] = "0o755"

        # Ensure catalog on live disk matches reviewed 16-service catalog
        catalog_src = reviewed_16_catalog_source().encode()
        live_catalog = live / "home/.local/lib/mobile-server/service_catalog.py"
        live_catalog.parent.mkdir(parents=True, exist_ok=True)
        live_catalog.write_bytes(catalog_src)
        os.chmod(live_catalog, 0o600)

        bundle_catalog = bundle / "app/service_catalog.py"
        bundle_catalog.write_bytes(catalog_src)
        os.chmod(bundle_catalog, 0o600)

        c_sha, c_size = ra.rt.compute_file_sha256(live_catalog)
        for f in preconds["files"]:
            if f["path"] == ra.SERVICE_CATALOG_REMOTE:
                f["kind"] = "file"
                f["sha256"] = c_sha
                f["bytes"] = c_size
                f["mode"] = "0o600"

        # Setup live JAR
        live_jar = live / "home/suwayomi/bin/Suwayomi-Server.jar"
        live_jar.parent.mkdir(parents=True, exist_ok=True)
        orig_jar_bytes = b"PK\x03\x04ORIGINAL_ACCEPTED_JAR_BYTES"
        live_jar.write_bytes(orig_jar_bytes)
        os.chmod(live_jar, 0o644)
        j_sha, j_size = ra.rt.compute_file_sha256(live_jar)

        preconds["files"].append({
            "path": ra.SUWAYOMI_JAR_REMOTE,
            "kind": "file",
            "sha256": j_sha,
            "bytes": j_size,
            "mode": "0o644",
            "uid": os.getuid(),
            "target_mode": "0o644",
        })

        # Setup candidate JAR in bundle
        candidate_jar = bundle / ra.DEFAULT_SUWAYOMI_JAR_LOCAL
        candidate_jar.parent.mkdir(parents=True, exist_ok=True)
        candidate_jar_bytes = b"PK\x03\x04CANDIDATE_PARALLEL_JAR_BYTES"
        candidate_jar.write_bytes(candidate_jar_bytes)
        os.chmod(candidate_jar, 0o644)

        return bundle, preconds, live, tx_dir

    def test_plan_reports_changed_destinations_and_affected_services_without_disk_writes(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        res = ra.plan(bundle, preconds, namespace_root=live)
        self.assertEqual(res["status"], "plan_ok")
        self.assertEqual(res["affected_services"], ["dashboard"])
        self.assertFalse(res["supervisor_affected"])
        self.assertEqual(list(tx_dir.iterdir()), [])

    def test_exact_restart_mapping_and_healthy_activation(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.running_processes["caddy"] = [(102, "start_102")]
        driver.healthy_services.add("dashboard")
        driver.healthy_services.add("caddy")

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["affected_services"], ["dashboard"])
        self.assertEqual(res["reactivated_services"], ["dashboard"])

        terminated = [e for e in driver.events if e[0] == "terminate"]
        self.assertEqual(len(terminated), 1)
        self.assertEqual(terminated[0][1], [(101, "start_101")])
        launched = [e for e in driver.events if e[0] == "launch"]
        self.assertEqual(launched, [("launch", "dashboard")])
        self.assertEqual(driver.running_processes["caddy"], [(102, "start_102")])

    def test_unchanged_release_causes_no_service_restarts(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["status"], "completed_noop_services")
        self.assertFalse(any(e[0] in ("terminate", "launch") for e in driver.events))
        self.assertEqual(driver.running_processes["dashboard"], [(101, "start_101")])

    def test_pre_disabled_services_remain_untouched_and_unstarted(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        dash_flag = disabled_dir / "dashboard"
        dash_flag.write_text("custom_preexisting_disable_bytes", encoding="utf-8")
        os.chmod(dash_flag, 0o644)
        orig_st = dash_flag.stat()

        driver = FakeLifecycleDriver()
        driver.running_processes.pop("dashboard", None)

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
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

    def test_agy_runtime_asset_modifications_refused(self):
        fake_entry = [{"remote": "/data/data/com.termux/files/home/bin/agy_session.sh", "action": ra.rt.ACTION_REPLACE}]
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.map_changed_destinations(fake_entry)
        self.assertIn("AGY launcher/runner/ttyd runtime-asset changes are prohibited", str(ctx.exception))

    def test_deliberately_paused_server_refuses_activation(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture()
        driver = FakeLifecycleDriver()
        driver.paused = True
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertIn("deliberately paused", str(ctx.exception))

    def test_surviving_process_blocks_all_file_writes(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_quiescence = True

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_auth_content = live_auth.read_text(encoding="utf-8")

        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)

        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_auth_content)
        guard_flag = live / "home/.config/services_disabled/dashboard"
        self.assertTrue(guard_flag.exists())

    def test_guard_creation_crash_before_inode_recognized_via_token(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()

        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)

        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        token = "release_guard:act_simulated_crash:dashboard"
        guard_file = disabled_dir / "dashboard"
        guard_file.write_text(f"RELEASE_GUARD_TOKEN={token}\nTX_ID=act_simulated_crash\nSID=dashboard\n", encoding="utf-8")
        os.chmod(guard_file, 0o600)

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

    def test_concurrent_guard_tamper_refused_before_apply(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        # Inode-preserving tamper after install_guards
        orig_install_guards = ra.install_guards
        def _tampering_install_guards(*args, **kwargs):
            active, disabled = orig_install_guards(*args, **kwargs)
            flag_path = disabled_dir / "dashboard"
            flag_path.write_text("TAMPERED_CONTENT_INODE_PRESERVED", encoding="utf-8")
            return active, disabled

        with unittest.mock.patch.object(ra, "install_guards", _tampering_install_guards):
            with self.assertRaises(ra.ConcurrencyError) as ctx:
                ra.apply(bundle, preconds, tx_dir, live, driver=driver)
            self.assertIn("content tampered concurrently", str(ctx.exception))

    def test_concurrent_guard_touch_refused_before_apply(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_auth_content = live_auth.read_text(encoding="utf-8")
        recorded_inode = [None]
        recorded_bytes = [None]

        orig_install_guards = ra.install_guards
        def _touching_install_guards(*args, **kwargs):
            active, disabled = orig_install_guards(*args, **kwargs)
            flag_path = disabled_dir / "dashboard"
            st = flag_path.lstat()
            recorded_inode[0] = st.st_ino
            recorded_bytes[0] = flag_path.read_bytes()
            # Deterministic touch with os.utime using a different ns, original bytes/inode unchanged
            os.utime(flag_path, ns=(st.st_atime_ns, st.st_mtime_ns + 100_000_000))
            return active, disabled

        with unittest.mock.patch.object(ra, "install_guards", _touching_install_guards):
            with self.assertRaises(ra.ConcurrencyError) as ctx:
                ra.apply(bundle, preconds, tx_dir, live, driver=driver)
            self.assertIn("timestamps modified concurrently", str(ctx.exception))

        # Refuses before file writes: live file unchanged
        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_auth_content)

        # Leaves marker: guard file exists with unchanged inode and bytes
        guard_path = disabled_dir / "dashboard"
        self.assertTrue(guard_path.exists())
        st_after = guard_path.lstat()
        self.assertEqual(st_after.st_ino, recorded_inode[0])
        self.assertEqual(guard_path.read_bytes(), recorded_bytes[0])

        # Activation journal recorded failure
        journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(journal["state"], ra.STATE_RECOVERY_REQUIRED)

    def test_pre_existing_flag_deletion_refused_before_apply(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        dash_flag = disabled_dir / "dashboard"
        dash_flag.write_text("preexisting", encoding="utf-8")

        driver = FakeLifecycleDriver()

        orig_install_guards = ra.install_guards
        def _deleting_install_guards(*args, **kwargs):
            active, disabled = orig_install_guards(*args, **kwargs)
            dash_flag.unlink() # Simulating external user deletion
            return active, disabled

        with unittest.mock.patch.object(ra, "install_guards", _deleting_install_guards):
            with self.assertRaises(ra.ConcurrencyError) as ctx:
                ra.apply(bundle, preconds, tx_dir, live, driver=driver)
            self.assertIn("vanished before file mutation", str(ctx.exception))

    def test_interruption_during_file_apply_restores_all_prior_files(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_content = live_auth.read_text(encoding="utf-8")

        def _failing_apply(self_tx, namespace_root=None):
            raise IOError("Simulated power loss / IO failure during apply")

        with unittest.mock.patch.object(ra.rt.ReleaseTransaction, "apply", _failing_apply):
            with self.assertRaises(IOError):
                ra.apply(bundle, preconds, tx_dir, live, driver=driver)

        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_content)
        self.assertFalse((live / "home/.config/services_disabled/dashboard").exists())

    def test_candidate_health_failure_rolls_back_coherently(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_candidate_health = {"dashboard"}

        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_content = live_auth.read_text(encoding="utf-8")

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2)

        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_content)
        journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(journal["state"], ra.STATE_ROLLED_BACK)

    def test_candidate_survivor_during_rollback_blocks_file_rollback(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_candidate_health = {"dashboard"}

        orig_terminate = driver.terminate
        def _surviving_terminate(records):
            driver.events.append(("terminate", list(records)))
            for pid, ident in records:
                if pid >= 2000:
                    continue # Candidate process survives rollback!
                for sid in list(driver.running_processes.keys()):
                    driver.running_processes[sid] = [r for r in driver.running_processes[sid] if r != (pid, ident)]

        driver.terminate = _surviving_terminate

        with self.assertRaises(ra.QuiescenceError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2)
        self.assertIn("survived termination during rollback", str(ctx.exception))

        # Journal is in RECOVERY_REQUIRED and safe guards remain installed
        journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(journal["state"], ra.STATE_RECOVERY_REQUIRED)
        guard_flag = live / "home/.config/services_disabled/dashboard"
        self.assertTrue(guard_flag.exists())

    def test_crash_after_candidate_start_quiesces_candidate_before_file_rollback(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
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

    def test_daemon_lock_held_without_matching_pid_refused(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_supervisor=True)
        driver = FakeLifecycleDriver()
        driver.daemon_running = True
        driver.fail_find_daemon = True # Daemon lock held but no PID located!

        with self.assertRaises(ra.QuiescenceError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertIn("exact process could not be identified", str(ctx.exception))

    def test_standalone_recovery_with_deleted_bundle_and_broken_live_modules(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        live_auth = live / "home/.local/lib/mobile-server/auth.py"
        orig_auth = live_auth.read_text(encoding="utf-8")

        # Candidate modifies service_catalog with broken syntax
        cat_file = bundle / "app/service_catalog.py"
        cat_file.write_text("raise SyntaxError('broken candidate')\n")
        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)

        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)
        recovery_hashes = ra.preserve_recovery_lifecycle_code(tx_dir, live)

        tx.apply(namespace_root=live)

        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        guard_file = disabled_dir / "dashboard"
        guard_file.write_text("PRE_EXISTING_DISABLED\n", encoding="utf-8")
        st = guard_file.stat()

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
            "flags": {
                "dashboard": {
                    "pre_existing": True,
                    "kind": "file",
                    "raw_link": None,
                    "dev": int(st.st_dev),
                    "ino": int(st.st_ino),
                    "mode": int(stat.S_IMODE(st.st_mode)),
                    "uid": int(st.st_uid),
                    "gid": int(st.st_gid),
                    "size": int(st.st_size),
                    "mtime_ns": int(st.st_mtime_ns),
                    "ctime_ns": int(st.st_ctime_ns),
                    "sha256": ra.compute_file_sha256(guard_file),
                    "intent": "preserve",
                }
            },
            "quiescence_records": {},
            "daemon_record": None,
            "recovery_assets": recovery_hashes,
        }
        ra.save_activation_journal(tx_dir, journal)

        shutil.rmtree(bundle)

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
        self.assertEqual(live_auth.read_text(encoding="utf-8"), orig_auth)

    def test_recovery_asset_hash_corruption_refused_before_execution(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        tx = ra.rt.prepare(bundle, ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True), preconds, tx_dir, live)
        recovery_hashes = ra.preserve_recovery_lifecycle_code(tx_dir, live)

        # Corrupt one preserved recovery asset
        (tx_dir / "recovery_lifecycle/server_supervisor.py").write_text("# tampered\n", encoding="utf-8")

        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_recovery_assets(tx_dir / "recovery_lifecycle", recovery_hashes, os.getuid())
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_journal_corruption_refused(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        tx = ra.rt.prepare(bundle, ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True), preconds, tx_dir, live)
        tx_journal = tx.load_journal()

        # Corrupt journal: omitted affected service
        corrupt_journal = {
            "version": 1,
            "state": ra.STATE_APPLIED,
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": [], # Missing dashboard!
            "supervisor_affected": False,
            "flags": {},
        }
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(corrupt_journal, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Affected services mismatch", str(ctx.exception))

    def test_prior_service_health_failure_retains_recovery_state(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.fail_candidate_health = {"dashboard"}
        driver.fail_prior_health = {"dashboard"} # Prior dashboard also fails health probe on relaunch!

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2)

        journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(journal["state"], ra.STATE_RECOVERY_REQUIRED)
        self.assertTrue((live / "home/.config/services_disabled/dashboard").exists())

    def test_strict_lock_ordering_no_deadlock(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        start_time = time.monotonic()
        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        elapsed = time.monotonic() - start_time
        self.assertLess(elapsed, 5.0)
        self.assertEqual(res["state"], ra.STATE_COMPLETED)

    def test_idempotent_repeated_status_and_recover(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.healthy_services.add("dashboard")

        res1 = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res1["state"], ra.STATE_COMPLETED)

        st = ra.status(tx_dir)
        self.assertEqual(st["activation_state"], ra.STATE_COMPLETED)

        rec = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(rec["state"], ra.STATE_COMPLETED)
        self.assertEqual(rec["status"], "idempotent_success")





    def test_root_probe_1_missing_flag_refused_before_signal_or_restore(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()

        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)
        tx.apply(live)

        driver.running_processes["dashboard"] = [(2001, "proc_dashboard_cand")]

        tx_id = "act_probe1"
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        token = f"release_guard:{tx_id}:dashboard"
        guard_file = disabled_dir / "dashboard"
        guard_file.write_text(f"RELEASE_GUARD_TOKEN={token}\nTX_ID={tx_id}\nSID=dashboard\n", encoding="utf-8")
        os.chmod(guard_file, 0o600)

        act_journal = {
            "version": 1,
            "state": ra.STATE_APPLIED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": tx_id,
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": False,
            "daemon_was_running": False,
            "flags": {},
            "quiescence_records": {"dashboard": [(1001, "proc_dashboard_orig")]},
            "daemon_record": None,
        }
        ra.save_activation_journal(tx_dir, act_journal)

        auth_cand = (live / "home/.local/lib/mobile-server/auth.py").read_text(encoding="utf-8")

        with self.assertRaises(ra.ActivationError):
            ra.recover(tx_dir, live, driver=driver)

        # No terminate events occurred
        term_events = [e for e in driver.events if e[0] == "terminate"]
        self.assertEqual(term_events, [])
        # Candidate file was NOT restored to preimage
        auth_after = (live / "home/.local/lib/mobile-server/auth.py").read_text(encoding="utf-8")
        self.assertEqual(auth_cand, auth_after)

    def test_presnapshot_claim_cannot_bypass_applied_file_transaction(self):
        """A corrupt empty snapshot must not bypass quiescence for published files."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        journal = ra.load_activation_journal(tx_dir)
        journal.update(state=ra.STATE_PREPARED, flags={})
        ra.save_activation_journal(tx_dir, journal)
        auth_path = live / "home/.local/lib/mobile-server/auth.py"
        candidate_bytes = auth_path.read_bytes()
        driver.events.clear()

        for operation in (ra.rollback, ra.recover):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ra.ActivationError, "PREPARED file transaction"):
                    operation(tx_dir, live, driver=driver)
                self.assertEqual(auth_path.read_bytes(), candidate_bytes)
                self.assertEqual(driver.events, [])
                self.assertEqual(ra.rt.ReleaseTransaction(tx_dir).load_journal()["state"], ra.rt.STATE_COMMITTED)

    def test_root_probe_2_post_success_explicit_rollback_recreates_guard_coherently(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()

        res_apply = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res_apply["state"], ra.STATE_COMPLETED)

        disabled_dir = live / "home/.config/services_disabled"
        self.assertFalse((disabled_dir / "dashboard").exists())

        res_rb = ra.rollback(tx_dir, live, driver=driver)
        self.assertEqual(res_rb["state"], ra.STATE_ROLLED_BACK)

        auth_path = live / "home/.local/lib/mobile-server/auth.py"
        self.assertIn("# Original live auth preimage", auth_path.read_text(encoding="utf-8"))
        self.assertIn("dashboard", driver.healthy_services)
        self.assertFalse((disabled_dir / "dashboard").exists())

    def test_flags_mapping_validation_and_typed_metadata_corruption(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)

        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)
        tx_journal = tx.load_journal()

        base_journal = {
            "version": 1,
            "state": ra.STATE_APPLIED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": "act_test_corrupt",
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": False,
            "daemon_was_running": False,
            "flags": {
                "dashboard": {
                    "pre_existing": False,
                    "intent": "guarded",
                    "token": "release_guard:act_test_corrupt:dashboard",
                    "guard_created": True,
                    "dev": 1234,
                    "ino": 5678,
                    "mode": 0o600,
                    "uid": os.getuid(),
                    "mtime_ns": 123456789,
                    "ctime_ns": 123456789,
                }
            },
        }

        # Base journal validates cleanly
        ra.validate_activation_journal(base_journal, tx_journal, live, tx_dir, os.getuid())

        # Extra flag
        bad1 = dict(base_journal)
        bad1["flags"] = dict(base_journal["flags"])
        bad1["flags"]["caddy"] = {
            "pre_existing": False,
            "intent": "guarded",
            "token": "release_guard:act_test_corrupt:caddy",
            "guard_created": True,
            "dev": 1234,
            "ino": 5679,
            "mode": 0o600,
            "uid": os.getuid(),
            "mtime_ns": 123456789,
            "ctime_ns": 123456789,
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad1, tx_journal, live, tx_dir, os.getuid())

        # Forged token
        bad2 = dict(base_journal)
        bad2["flags"] = {
            "dashboard": dict(base_journal["flags"]["dashboard"], token="forged_token_payload")
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad2, tx_journal, live, tx_dir, os.getuid())

        # Bad mode type
        bad3 = dict(base_journal)
        bad3["flags"] = {
            "dashboard": dict(base_journal["flags"]["dashboard"], mode="0600")
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad3, tx_journal, live, tx_dir, os.getuid())

        # Pre-existing flip without metadata
        bad4 = dict(base_journal)
        bad4["flags"] = {
            "dashboard": {"pre_existing": True}
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad4, tx_journal, live, tx_dir, os.getuid())

        # Bad mtime_ns type
        bad5 = dict(base_journal)
        bad5["flags"] = {
            "dashboard": dict(base_journal["flags"]["dashboard"], mtime_ns="123456789")
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad5, tx_journal, live, tx_dir, os.getuid())

        # Bad ctime_ns type (boolean)
        bad6 = dict(base_journal)
        bad6["flags"] = {
            "dashboard": dict(base_journal["flags"]["dashboard"], ctime_ns=True)
        }
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad6, tx_journal, live, tx_dir, os.getuid())

        # Missing mtime_ns
        bad7 = dict(base_journal)
        bad7_d = dict(base_journal["flags"]["dashboard"])
        del bad7_d["mtime_ns"]
        bad7["flags"] = {"dashboard": bad7_d}
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad7, tx_journal, live, tx_dir, os.getuid())

        # Missing ctime_ns
        bad8 = dict(base_journal)
        bad8_d = dict(base_journal["flags"]["dashboard"])
        del bad8_d["ctime_ns"]
        bad8["flags"] = {"dashboard": bad8_d}
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_journal(bad8, tx_journal, live, tx_dir, os.getuid())

    def test_remove_temporary_guards_refuses_touched_marker(self):
        live = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(live, ignore_errors=True))
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        tx_id = "act_test_rm_guard"
        sid = "dashboard"
        guard_path = disabled_dir / sid
        token = f"release_guard:{tx_id}:{sid}"
        payload = f"RELEASE_GUARD_TOKEN={token}\nTX_ID={tx_id}\nSID={sid}\n".encode("utf-8")
        guard_path.write_bytes(payload)
        os.chmod(guard_path, 0o600)

        st = guard_path.lstat()
        flags = {
            sid: {
                "pre_existing": False,
                "intent": "guarded",
                "token": token,
                "guard_created": True,
                "dev": int(st.st_dev),
                "ino": int(st.st_ino),
                "mode": 0o600,
                "uid": os.getuid(),
                "mtime_ns": int(st.st_mtime_ns),
                "ctime_ns": int(st.st_ctime_ns),
            }
        }

        # Deterministic touch with os.utime using different ns
        os.utime(guard_path, ns=(st.st_atime_ns, st.st_mtime_ns + 50_000_000))

        # Directly prove remove_temporary_guards refuses touched marker
        with self.assertRaises(ra.ConcurrencyError) as ctx:
            ra.remove_temporary_guards(disabled_dir, flags, os.getuid(), tx_id)
        self.assertIn("timestamps modified concurrently", str(ctx.exception))
        self.assertTrue(guard_path.exists())

    def test_remove_temporary_guards_malformed_metadata_fails_closed(self):
        live = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(live, ignore_errors=True))
        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)

        tx_id = "act_test_rm_malformed"
        sid = "dashboard"
        guard_path = disabled_dir / sid
        token = f"release_guard:{tx_id}:{sid}"
        payload = f"RELEASE_GUARD_TOKEN={token}\nTX_ID={tx_id}\nSID={sid}\n".encode("utf-8")
        guard_path.write_bytes(payload)
        os.chmod(guard_path, 0o600)

        st = guard_path.lstat()
        valid_flags = {
            sid: {
                "pre_existing": False,
                "intent": "guarded",
                "token": token,
                "guard_created": True,
                "dev": int(st.st_dev),
                "ino": int(st.st_ino),
                "mode": 0o600,
                "uid": os.getuid(),
                "mtime_ns": int(st.st_mtime_ns),
                "ctime_ns": int(st.st_ctime_ns),
            }
        }

        # Test various malformed metadata schemas:
        malformed_cases = [
            # Missing mtime_ns
            {k: v for k, v in valid_flags[sid].items() if k != "mtime_ns"},
            # Non-integer mtime_ns
            dict(valid_flags[sid], mtime_ns="not_an_int"),
            # Boolean mtime_ns
            dict(valid_flags[sid], mtime_ns=True),
            # Missing ctime_ns
            {k: v for k, v in valid_flags[sid].items() if k != "ctime_ns"},
            # None ctime_ns
            dict(valid_flags[sid], ctime_ns=None),
            # Missing ino
            dict(valid_flags[sid], ino=None),
            # Missing dev
            dict(valid_flags[sid], dev=None),
            # Bad mode
            dict(valid_flags[sid], mode=0o644),
            # Bad UID
            dict(valid_flags[sid], uid=os.getuid() + 9999),
            # Forged token
            dict(valid_flags[sid], token="forged_token"),
            # Non-boolean pre_existing
            dict(valid_flags[sid], pre_existing="false"),
        ]

        for bad_info in malformed_cases:
            with self.subTest(bad_info=bad_info):
                with self.assertRaises(ra.ActivationError):
                    ra.remove_temporary_guards(disabled_dir, {sid: bad_info}, os.getuid(), tx_id)
                # Fails closed: marker still on disk!
                self.assertTrue(guard_path.exists())

        # When valid metadata is supplied, removal succeeds
        ra.remove_temporary_guards(disabled_dir, valid_flags, os.getuid(), tx_id)
        self.assertFalse(guard_path.exists())

    def test_guard_removal_interruption_repeatable_recovery(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()

        manifest_entries = ra.rt._load_release_manifest_module().generate_manifest(bundle, require_gateway=True)
        tx = ra.rt.prepare(bundle, manifest_entries, preconds, tx_dir, live)
        tx.apply(live)
        tx.commit(live)

        driver.running_processes["dashboard"] = [(3001, "proc_dashboard_cand")]

        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        if (disabled_dir / "dashboard").exists():
            (disabled_dir / "dashboard").unlink()

        tx_id = "act_removal_interrupted"
        act_journal = {
            "version": 1,
            "state": ra.STATE_HEALTHY,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": tx_id,
            "namespace_root": str(live),
            "transaction_dir": str(tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["dashboard"],
            "supervisor_affected": False,
            "daemon_was_running": False,
            "flags": {
                "dashboard": {
                    "pre_existing": False,
                    "intent": "remove_guard",
                    "token": f"release_guard:{tx_id}:dashboard",
                    "guard_created": False,
                    "dev": None,
                    "ino": None,
                    "mode": 0o600,
                    "uid": os.getuid(),
                }
            },
        }
        ra.save_activation_journal(tx_dir, act_journal)

        res = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_ROLLED_BACK)
        term_events = [e for e in driver.events if e[0] == "terminate"]
        self.assertTrue(any((3001, "proc_dashboard_cand") in e[1] for e in term_events))
        auth_path = live / "home/.local/lib/mobile-server/auth.py"
        self.assertIn("# Original live auth preimage", auth_path.read_text(encoding="utf-8"))

        res2 = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(res2["state"], ra.STATE_ROLLED_BACK)
        self.assertEqual(res2["status"], "idempotent_success")

    def test_guard_creation_no_clobber_concurrent_marker_and_broken_symlink(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()

        res_apply = ra.apply(bundle, preconds, tx_dir, live, driver=driver)
        self.assertEqual(res_apply["state"], ra.STATE_COMPLETED)

        disabled_dir = live / "home/.config/services_disabled"
        # Simulate concurrent foreign marker appearing before explicit rollback
        marker = disabled_dir / "dashboard"
        marker.write_text("FOREIGN_ACTOR_MARKER\n", encoding="utf-8")

        with self.assertRaises(ra.ConcurrencyError):
            ra.rollback(tx_dir, live, driver=driver)

        self.assertEqual(marker.read_text(encoding="utf-8"), "FOREIGN_ACTOR_MARKER\n")

        # Now test broken symlink
        marker.unlink()
        marker.symlink_to("/nonexistent/broken/target")
        self.assertTrue(marker.is_symlink())
        self.assertFalse(marker.exists())

        with self.assertRaises(ra.ConcurrencyError):
            ra.rollback(tx_dir, live, driver=driver)

        self.assertTrue(marker.is_symlink())

    def test_candidate_process_appearing_during_unlocked_daemon_stop_gap(self):
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True, modify_supervisor=True)

        class InjectedCandidateDriver(FakeLifecycleDriver):
            def stop_daemon(self, pid, identity):
                super().stop_daemon(pid, identity)
                self.running_processes["dashboard"] = [(4001, "rogue_candidate_proc")]

        driver = InjectedCandidateDriver()
        driver.daemon_running = True
        driver.daemon_proc = (9999, "daemon_test_proc")

        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver)

        act = ra.load_activation_journal(tx_dir)
        self.assertEqual(act["state"], ra.STATE_RECOVERY_REQUIRED)

    def test_validate_profile_strictness(self):
        """Strict profile validation: known profiles pass; unknown/nonstring/null failclosed."""
        self.assertEqual(ra.validate_profile("general"), "general")
        self.assertEqual(ra.validate_profile("suwayomi-filter"), "suwayomi-filter")
        self.assertEqual(ra.validate_profile("suwayomi-lifecycle"), "suwayomi-lifecycle")

        for bad in (None, 123, True, False, "", "unknown", "suwayomi", "filter", ["general"]):
            with self.assertRaises(ra.ActivationError):
                ra.validate_profile(bad)

    def test_default_general_profile_refuses_all_suwayomi_changes(self):
        """Default general profile must continue refusing any changed Suwayomi script."""
        sw_scripts = (
            "start_suwayomi.sh",
            "suwayomi_daily_update.py",
            "suwayomi_graphql_filter.py",
            "suwayomi_update.sh",
        )
        for sw in sw_scripts:
            fake_entry = [{"remote": f"/data/data/com.termux/files/home/bin/{sw}", "action": ra.rt.ACTION_REPLACE}]
            with self.assertRaises(ra.ActivationError) as ctx:
                ra.map_changed_destinations(fake_entry, profile="general")
            self.assertIn("Suwayomi launcher/filter/scheduler sources must be unchanged", str(ctx.exception))

            # Also verify default argument profile=general behaves identically
            with self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(fake_entry)

    def test_suwayomi_filter_profile_plan_and_apply_success(self):
        """Profile suwayomi-filter allows only suwayomi_graphql_filter.py; supervisor unaffected."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        orig_filter = (live / "home/bin/suwayomi_graphql_filter.py").read_text(encoding="utf-8")
        candidate_code = "# CANDIDATE_FILTER_STUB\nprint('candidate filter')\n"
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text(candidate_code, encoding="utf-8")

        # Plan check
        plan_res = ra.plan(bundle, preconds, namespace_root=live, profile="suwayomi-filter")
        self.assertEqual(plan_res["status"], "plan_ok")
        self.assertEqual(plan_res["profile"], "suwayomi-filter")
        self.assertEqual(plan_res["affected_services"], ["suwayomi_filter"])
        self.assertFalse(plan_res["supervisor_affected"])
        self.assertEqual(len(plan_res["changed_destinations"]), 1)
        self.assertEqual(plan_res["changed_destinations"][0]["remote"], ra.SUWAYOMI_FILTER_REMOTE)

        # Apply check
        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_filter"] = [(1234, "start_1234")]
        driver.running_processes["dashboard"] = [(1000, "start_1000")]
        driver.healthy_services.add("suwayomi_filter")
        driver.healthy_services.add("dashboard")

        apply_res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-filter")
        self.assertEqual(apply_res["state"], ra.STATE_COMPLETED)
        self.assertEqual(apply_res["profile"], "suwayomi-filter")
        self.assertEqual(apply_res["affected_services"], ["suwayomi_filter"])
        self.assertEqual(apply_res["reactivated_services"], ["suwayomi_filter"])

        # File on live disk changed to candidate
        self.assertEqual((live / "home/bin/suwayomi_graphql_filter.py").read_text(encoding="utf-8"), candidate_code)

        # Candidate was quiesced then restarted; dashboard was NOT touched; supervisor was NOT touched
        terminated = [e for e in driver.events if e[0] == "terminate"]
        self.assertEqual(len(terminated), 1)
        self.assertEqual(terminated[0][1], [(1234, "start_1234")])

        launched = [e for e in driver.events if e[0] == "launch"]
        self.assertEqual(launched, [("launch", "suwayomi_filter")])

        self.assertFalse(any(e[0] in ("stop_daemon", "start_daemon") for e in driver.events))
        self.assertEqual(driver.running_processes["dashboard"], [(1000, "start_1000")])

        # Status check
        st = ra.status(tx_dir)
        self.assertEqual(st["activation_state"], ra.STATE_COMPLETED)
        self.assertEqual(st["profile"], "suwayomi-filter")
        self.assertEqual(st["affected_services"], ["suwayomi_filter"])
        self.assertFalse(st["supervisor_affected"])

    def test_suwayomi_filter_profile_noop(self):
        """Zero changes under suwayomi-filter profile is a completed noop release."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_filter"] = [(1234, "start_1234")]

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-filter")
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["status"], "completed_noop_services")
        self.assertEqual(res["profile"], "suwayomi-filter")
        self.assertEqual(res["affected_services"], [])
        self.assertFalse(any(e[0] in ("terminate", "launch", "stop_daemon", "start_daemon") for e in driver.events))

    def test_suwayomi_filter_profile_refuses_forbidden_single_and_mixed_changes(self):
        """suwayomi-filter profile rejects any single or mixed change outside suwayomi_graphql_filter.py."""
        forbidden_singles = [
            # Auth
            [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/auth.py", "action": ra.rt.ACTION_REPLACE}],
            # Launcher
            [{"remote": "/data/data/com.termux/files/home/bin/start_suwayomi.sh", "action": ra.rt.ACTION_REPLACE}],
            # Scheduler
            [{"remote": "/data/data/com.termux/files/home/bin/suwayomi_daily_update.py", "action": ra.rt.ACTION_REPLACE}],
            [{"remote": "/data/data/com.termux/files/home/bin/suwayomi_update.sh", "action": ra.rt.ACTION_REPLACE}],
            # Caddy
            [{"remote": "/data/data/com.termux/files/home/.config/caddy/Caddyfile", "action": ra.rt.ACTION_REPLACE}],
            # Catalog
            [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/service_catalog.py", "action": ra.rt.ACTION_REPLACE}],
            # CLI
            [{"remote": "/data/data/com.termux/files/usr/bin/server-user", "action": ra.rt.ACTION_REPLACE}],
            # Gateway
            [{"remote": "/data/data/com.termux/files/home/bin/mobile-gateway", "action": ra.rt.ACTION_REPLACE}],
            # Dashboard
            [{"remote": "/data/data/com.termux/files/usr/bin/dashboard", "action": ra.rt.ACTION_REPLACE}],
            # AGY
            [{"remote": "/data/data/com.termux/files/home/bin/agy_session.sh", "action": ra.rt.ACTION_REPLACE}],
        ]

        for entry in forbidden_singles:
            with self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(entry, profile="suwayomi-filter")

        # Mixed changes: filter + auth
        mixed_filter_auth = [
            {"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE},
            {"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/auth.py", "action": ra.rt.ACTION_REPLACE},
        ]
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(mixed_filter_auth, profile="suwayomi-filter")

        # Mixed changes: filter + launcher
        mixed_filter_launcher = [
            {"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE},
            {"remote": "/data/data/com.termux/files/home/bin/start_suwayomi.sh", "action": ra.rt.ACTION_REPLACE},
        ]
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(mixed_filter_launcher, profile="suwayomi-filter")

        # Multiple filter changes
        duplicate_filter = [
            {"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE},
            {"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE},
        ]
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(duplicate_filter, profile="suwayomi-filter")

    def test_filter_profile_requires_exact_destination_and_replace_action(self):
        for remote in (
            "/tmp/home/bin/suwayomi_graphql_filter.py",
            "/data/data/com.termux/files/other/home/bin/suwayomi_graphql_filter.py",
            "relative/home/bin/suwayomi_graphql_filter.py",
        ):
            with self.subTest(remote=remote), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(
                    [{"remote": remote, "action": ra.rt.ACTION_REPLACE}], profile="suwayomi-filter"
                )
        for action in (None, ra.rt.ACTION_CREATE, "REMOVE"):
            with self.subTest(action=action), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(
                    [{"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": action}], profile="suwayomi-filter"
                )

    def test_journal_profile_validation_forgery_and_mismatch(self):
        """Activation journal profile validation: legacy absence valid, invalid/forged refused."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text("# FILTER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_filter"] = [(1234, "start_1234")]
        driver.healthy_services.add("suwayomi_filter")

        ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-filter")

        # 1. Tamper journal with invalid profile
        jpath = tx_dir / "activation_journal.json"
        with open(jpath, "r", encoding="utf-8") as f:
            jdata = json.load(f)

        tx = ra.rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()

        # Invalid profile type / value
        for bad_prof in ("unknown", 123, None):
            jdata_bad = dict(jdata, profile=bad_prof)
            with self.assertRaises(ra.ActivationError):
                ra.validate_activation_journal(jdata_bad, tx_journal, live, tx_dir, os.getuid())

        # Forged profile 'general' on filter tx
        jdata_forged = dict(jdata, profile="general")
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(jdata_forged, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Suwayomi launcher/filter/scheduler sources must be unchanged", str(ctx.exception))

        # Forged affected_services
        jdata_mismatch = dict(jdata, affected_services=["dashboard"])
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(jdata_mismatch, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Affected services mismatch", str(ctx.exception))

    def test_legacy_journal_without_profile_treated_as_general(self):
        """Legacy activation journal without 'profile' field is validated as 'general'."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(modify_auth=True)
        driver = FakeLifecycleDriver()
        driver.running_processes["dashboard"] = [(101, "start_101")]
        driver.healthy_services.add("dashboard")

        ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="general")

        jpath = tx_dir / "activation_journal.json"
        with open(jpath, "r", encoding="utf-8") as f:
            jdata = json.load(f)

        # Remove profile field (simulate legacy journal)
        self.assertIn("profile", jdata)
        del jdata["profile"]

        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(jdata, f, indent=2)

        # Loading and validation must succeed
        loaded = ra.load_activation_journal(tx_dir)
        self.assertNotIn("profile", loaded)

        tx = ra.rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()
        # Must not raise:
        ra.validate_activation_journal(loaded, tx_journal, live, tx_dir, os.getuid())

        # Rollback using legacy journal must succeed
        res = ra.rollback(tx_dir, live, driver=driver)
        self.assertEqual(res["state"], ra.STATE_ROLLED_BACK)

    def test_suwayomi_filter_profile_health_failure_backout(self):
        """Candidate filter health failure quiesces candidate before restoring prior file and service."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        live_filter = live / "home/bin/suwayomi_graphql_filter.py"
        orig_filter = live_filter.read_text(encoding="utf-8")
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text("# BROKEN_FILTER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_filter"] = [(1234, "start_1234")]
        # Candidate health will fail
        driver.fail_candidate_health.add("suwayomi_filter")

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2, profile="suwayomi-filter")

        # Rollback executed: prior file content restored
        self.assertEqual(live_filter.read_text(encoding="utf-8"), orig_filter)

        # Flags state coherent: temporary guards removed
        guard_path = live / "home/.config/services_disabled/suwayomi_filter"
        self.assertFalse(guard_path.exists())

        # Check journal state
        act_journal = ra.load_activation_journal(tx_dir)
        self.assertEqual(act_journal["state"], ra.STATE_ROLLED_BACK)

    def test_suwayomi_filter_profile_repeated_recovery_idempotency(self):
        """Repeated recovery of suwayomi_filter activation is safe and idempotent."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        live_filter = live / "home/bin/suwayomi_graphql_filter.py"
        orig_filter = live_filter.read_text(encoding="utf-8")
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text("# CANDIDATE_FILTER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_filter"] = [(1234, "start_1234")]
        driver.healthy_services.add("suwayomi_filter")

        ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-filter")

        # First rollback
        rec1 = ra.rollback(tx_dir, live, driver=driver)
        self.assertEqual(rec1["state"], ra.STATE_ROLLED_BACK)
        self.assertEqual(rec1["status"], "rolled_back")
        self.assertEqual(live_filter.read_text(encoding="utf-8"), orig_filter)

        # Second recovery must be idempotent
        rec2 = ra.recover(tx_dir, live, driver=driver)
        self.assertEqual(rec2["state"], ra.STATE_ROLLED_BACK)
        self.assertEqual(rec2["status"], "idempotent_success")

    def test_cli_profile_choices(self):
        """CLI choices: --profile suwayomi-filter succeeds; invalid profile fails."""
        bundle, manifest, preconds, live, tx_dir = self._build_activation_fixture(unchanged_all=True)
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text("# FILTER\n", encoding="utf-8")
        preconds_file = self.test_root / "preconds.json"
        preconds_file.write_text(json.dumps(preconds), encoding="utf-8")

        # Valid profile on plan
        proc_ok = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "release_activation.py"),
                "plan",
                "--bundle",
                str(bundle),
                "--preconditions",
                str(preconds_file),
                "--namespace-root",
                str(live),
                "--profile",
                "suwayomi-filter",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc_ok.returncode, 0, f"CLI plan failed: {proc_ok.stderr}")
        res = json.loads(proc_ok.stdout)
        self.assertEqual(res["profile"], "suwayomi-filter")
        self.assertEqual(res["affected_services"], ["suwayomi_filter"])

        # Valid suwayomi-lifecycle profile on plan: restore filter to match preconditions, modify launcher & candidate catalog
        (bundle / "scripts/suwayomi_graphql_filter.py").write_text((live / "home/bin/suwayomi_graphql_filter.py").read_text(encoding="utf-8"), encoding="utf-8")
        (bundle / "scripts/start_suwayomi.sh").write_text("# CANDIDATE_LAUNCHER\n", encoding="utf-8")
        install_prior_catalog(live, preconds)
        preconds_file.write_text(json.dumps(preconds), encoding="utf-8")
        (bundle / "app/service_catalog.py").write_text(reviewed_16_catalog_source(), encoding="utf-8")
        proc_lc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "release_activation.py"),
                "plan",
                "--bundle",
                str(bundle),
                "--preconditions",
                str(preconds_file),
                "--namespace-root",
                str(live),
                "--profile",
                "suwayomi-lifecycle",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc_lc.returncode, 0, f"CLI lifecycle plan failed: {proc_lc.stderr}")
        res_lc = json.loads(proc_lc.stdout)
        self.assertEqual(res_lc["profile"], "suwayomi-lifecycle")
        self.assertIn("suwayomi", res_lc["affected_services"])

        # Invalid profile choice
        proc_bad = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "release_activation.py"),
                "plan",
                "--bundle",
                str(bundle),
                "--preconditions",
                str(preconds_file),
                "--profile",
                "invalid-profile",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(proc_bad.returncode, 0)
        self.assertIn("invalid choice", proc_bad.stderr)

    def test_suwayomi_lifecycle_profile_allowed_paths_and_scope(self):
        """Profile suwayomi-lifecycle allows exact 3 owned paths; derives exact service scope."""
        # 1. All 3 allowed paths replaced
        all_3_entries = [
            {"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE},
            {"remote": ra.SUPERVISOR_REMOTE, "action": ra.rt.ACTION_REPLACE},
            {"remote": ra.SERVICE_CATALOG_REMOTE, "action": ra.rt.ACTION_REPLACE},
        ]
        affected, sup_aff, changed = ra.map_changed_destinations(all_3_entries, profile="suwayomi-lifecycle")
        self.assertEqual(affected, {"dashboard", "suwayomi", "suwayomi_scheduler"})
        self.assertTrue(sup_aff)
        self.assertEqual(changed, [ra.SUWAYOMI_LAUNCHER_REMOTE, ra.SUPERVISOR_REMOTE, ra.SERVICE_CATALOG_REMOTE])

        # 2. Individual paths
        # Launcher only -> suwayomi
        aff, sup, _ = ra.map_changed_destinations(
            [{"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE}],
            profile="suwayomi-lifecycle",
        )
        self.assertEqual(aff, {"suwayomi"})
        self.assertFalse(sup)

        # Supervisor only -> supervisor
        aff, sup, _ = ra.map_changed_destinations(
            [{"remote": ra.SUPERVISOR_REMOTE, "action": ra.rt.ACTION_REPLACE}],
            profile="suwayomi-lifecycle",
        )
        self.assertEqual(aff, set())
        self.assertTrue(sup)

        # Catalog only -> dashboard, suwayomi_scheduler, supervisor
        aff, sup, _ = ra.map_changed_destinations(
            [{"remote": ra.SERVICE_CATALOG_REMOTE, "action": ra.rt.ACTION_REPLACE}],
            profile="suwayomi-lifecycle",
        )
        self.assertEqual(aff, {"dashboard", "suwayomi_scheduler"})
        self.assertTrue(sup)

        # 3. Disallowed paths rejected
        forbidden_remotes = [
            ra.SUWAYOMI_FILTER_REMOTE,
            "/data/data/com.termux/files/home/bin/suwayomi_daily_update.py",
            "/data/data/com.termux/files/home/bin/suwayomi_update.sh",
            "/data/data/com.termux/files/home/.local/lib/mobile-server/auth.py",
            "/data/data/com.termux/files/home/.config/caddy/Caddyfile",
            "/data/data/com.termux/files/usr/bin/dashboard",
            "/data/data/com.termux/files/home/bin/mobile-gateway",
            "/data/data/com.termux/files/home/bin/agy_session.sh",
        ]
        for remote in forbidden_remotes:
            with self.subTest(remote=remote), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(
                    [{"remote": remote, "action": ra.rt.ACTION_REPLACE}],
                    profile="suwayomi-lifecycle",
                )

        # 4. Disallowed actions rejected (only ACTION_REPLACE allowed)
        for action in (None, ra.rt.ACTION_CREATE, "REMOVE"):
            with self.subTest(action=action), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(
                    [{"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": action}],
                    profile="suwayomi-lifecycle",
                )

    def test_suwayomi_lifecycle_wrong_profiles_and_mixed_changes(self):
        """Profile cross-contamination refused: general refuses launcher/catalog; filter refuses launcher; lifecycle refuses filter."""
        # General profile refuses launcher
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(
                [{"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE}],
                profile="general",
            )

        # suwayomi-filter refuses launcher
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(
                [{"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE}],
                profile="suwayomi-filter",
            )

        # suwayomi-lifecycle refuses filter
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(
                [{"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE}],
                profile="suwayomi-lifecycle",
            )

        # Mixed filter + launcher refused under suwayomi-lifecycle
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(
                [
                    {"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE},
                    {"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": ra.rt.ACTION_REPLACE},
                ],
                profile="suwayomi-lifecycle",
            )

    def test_exact_15_to_16_vs_decoy_runtime_changes(self):
        """Exact 15->16 candidate runtime contract verified; decoys, reordering, and wrong profiles rejected."""
        import app.service_catalog as sc
        prior_services = ra.load_catalog_services_from_bytes(prior_catalog_source().encode(), "/home", "/usr", "test_prior")
        self.assertEqual(len(prior_services), 15)

        # 1. Exact valid candidate 16 services
        valid_cand = dict(prior_services)
        valid_cand["suwayomi_scheduler"] = {
            "cmd": ["/usr/bin/python3", "/home/bin/suwayomi_daily_update.py", "--foreground"]
        }
        # Passes validation
        ta.validate_catalog_transition(
            prior_services=prior_services,
            cand_services=valid_cand,
            home_str="/home",
            prefix_str="/usr",
            profile="suwayomi-lifecycle",
        )

        unknown_attribute = {sid: dict(value) for sid, value in valid_cand.items()}
        unknown_attribute["dashboard"]["unexpected_runtime_switch"] = True
        with self.assertRaises(RuntimeError):
            ta.validate_catalog_transition(prior_services, unknown_attribute, "/home", "/usr", "suwayomi-lifecycle")

        # 2. Decoy commands rejected
        for bad_cmd in (
            ["/usr/bin/python3", "/home/bin/suwayomi_daily_update.py", "--now"],
            ["/usr/bin/python3", "/home/bin/suwayomi_daily_update.py", "--status"],
            ["/usr/bin/python3", "/home/bin/suwayomi_daily_update.py", "--daemon"],
            ["/usr/bin/python3", "/home/bin/suwayomi_update.sh"],
        ):
            bad_cand = dict(prior_services)
            bad_cand["suwayomi_scheduler"] = {"cmd": bad_cmd}
            with self.subTest(bad_cmd=bad_cmd), self.assertRaises(RuntimeError) as ctx:
                ta.validate_catalog_transition(
                    prior_services=prior_services,
                    cand_services=bad_cand,
                    home_str="/home",
                    prefix_str="/usr",
                    profile="suwayomi-lifecycle",
                )
            self.assertIn("cmd drift", str(ctx.exception))

        # 3. Decoy extra attributes rejected (no port, no path, etc.)
        for extra_key, extra_val in (("port", 4567), ("path", "/healthz"), ("protocol", "http")):
            bad_cand = dict(prior_services)
            bad_cand["suwayomi_scheduler"] = {
                "cmd": ["/usr/bin/python3", "/home/bin/suwayomi_daily_update.py", "--foreground"],
                extra_key: extra_val,
            }
            with self.subTest(extra_key=extra_key), self.assertRaises(RuntimeError) as ctx:
                ta.validate_catalog_transition(
                    prior_services=prior_services,
                    cand_services=bad_cand,
                    home_str="/home",
                    prefix_str="/usr",
                    profile="suwayomi-lifecycle",
                )
            self.assertIn("unexpected attributes", str(ctx.exception))

        # 4. Altered prior service rejected
        altered_prior = dict(valid_cand)
        altered_prior["caddy"] = dict(altered_prior["caddy"], port=9999)
        with self.assertRaises(RuntimeError) as ctx:
            ta.validate_catalog_transition(
                prior_services=prior_services,
                cand_services=altered_prior,
                home_str="/home",
                prefix_str="/usr",
                profile="suwayomi-lifecycle",
            )
        self.assertIn("runtime drift", str(ctx.exception))

        # 5. Missing or reordered prior service rejected
        reordered_keys = list(prior_services.keys())
        reordered_keys[0], reordered_keys[1] = reordered_keys[1], reordered_keys[0]
        reordered_cand = {k: prior_services[k] for k in reordered_keys}
        reordered_cand["suwayomi_scheduler"] = valid_cand["suwayomi_scheduler"]
        with self.assertRaises(RuntimeError) as ctx:
            ta.validate_catalog_transition(
                prior_services=prior_services,
                cand_services=reordered_cand,
                home_str="/home",
                prefix_str="/usr",
                profile="suwayomi-lifecycle",
            )
        self.assertIn("first 15 keys mismatch", str(ctx.exception))

        # 6. Wrong profile rejected: 16 services under general or suwayomi-filter
        for prof in ("general", "suwayomi-filter"):
            with self.subTest(prof=prof), self.assertRaises(RuntimeError) as ctx:
                ta.validate_catalog_transition(
                    prior_services=prior_services,
                    cand_services=valid_cand,
                    home_str="/home",
                    prefix_str="/usr",
                    profile=prof,
                )
            self.assertIn("order mismatch", str(ctx.exception))

        # 7. 15 services under suwayomi-lifecycle rejected
        with self.assertRaises(RuntimeError) as ctx:
            ta.validate_catalog_transition(
                prior_services=prior_services,
                cand_services=prior_services,
                home_str="/home",
                prefix_str="/usr",
                profile="suwayomi-lifecycle",
            )
        self.assertIn("requires exact 16 candidate services", str(ctx.exception))

    def test_suwayomi_lifecycle_pre_existing_scheduler_refused(self):
        """Initial 15->16 transition refuses any pre-existing owned scheduler process."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# LAUNCHER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi_scheduler"] = [(4444, "sched_start_4444")]

        with self.assertRaises(ra.QuiescenceError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")
        self.assertIn("Pre-existing suwayomi_scheduler process found", str(ctx.exception))

    def test_suwayomi_lifecycle_concurrent_scheduler_appearance_refused(self):
        """Concurrent scheduler appearance before file mutation is detected and refused under lock."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# LAUNCHER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        orig_install_guards = ra.install_guards

        def _spawning_install_guards(*args, **kwargs):
            active, disabled = orig_install_guards(*args, **kwargs)
            # Concurrently spawn scheduler
            driver.running_processes["suwayomi_scheduler"] = [(5555, "sched_start_5555")]
            return active, disabled

        with unittest.mock.patch.object(ra, "install_guards", _spawning_install_guards):
            with self.assertRaises(ra.QuiescenceError) as ctx:
                ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")
            self.assertIn("appeared concurrently", str(ctx.exception))

    def test_suwayomi_lifecycle_pre_disabled_marker_preserved(self):
        """Pre-existing disabled marker for suwayomi_scheduler is preserved and unstarted."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# LAUNCHER\n", encoding="utf-8")

        disabled_dir = live / "home/.config/services_disabled"
        disabled_dir.mkdir(parents=True, exist_ok=True)
        sched_flag = disabled_dir / "suwayomi_scheduler"
        sched_flag.write_text("MANUALLY_DISABLED\n", encoding="utf-8")
        os.chmod(sched_flag, 0o600)

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.running_processes["dashboard"] = [(1002, "dash_1002")]
        driver.healthy_services.update({"suwayomi", "dashboard"})

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        # suwayomi_scheduler was NOT launched because it was pre-disabled
        launched = [e[1] for e in driver.events if e[0] == "launch"]
        self.assertNotIn("suwayomi_scheduler", launched)
        # Marker retained
        self.assertTrue(sched_flag.exists())
        self.assertEqual(sched_flag.read_text(encoding="utf-8"), "MANUALLY_DISABLED\n")

    def test_suwayomi_lifecycle_survivor_candidate_blocks_file_writes(self):
        """Candidate process surviving termination blocks file writes and marks RECOVERY_REQUIRED."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# CANDIDATE_LAUNCHER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.fail_quiescence = True

        live_launcher = live / "home/bin/start_suwayomi.sh"
        orig_launcher = live_launcher.read_text(encoding="utf-8")

        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")

        # Live file untouched
        self.assertEqual(live_launcher.read_text(encoding="utf-8"), orig_launcher)
        act = ra.load_activation_journal(tx_dir)
        self.assertEqual(act["state"], ra.STATE_RECOVERY_REQUIRED)

    def test_suwayomi_lifecycle_forged_or_omitted_transition_metadata_fails_closed(self):
        """Activation journal with forged/omitted transition metadata fails closed."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# LAUNCHER\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.healthy_services.add("suwayomi")

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")
        self.assertEqual(res["state"], ra.STATE_COMPLETED)

        # Test validation failures on mutated journal
        journal = ra.load_activation_journal(tx_dir)
        tx = ra.rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()

        # 1. Missing transition field
        bad_j1 = dict(journal)
        del bad_j1["transition"]
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j1, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Missing or invalid transition metadata", str(ctx.exception))

        # 2. Forged transition type
        bad_j2 = dict(journal, transition=dict(journal["transition"], type="forged_type"))
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j2, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Invalid transition type", str(ctx.exception))

        # 3. Forged candidate_service_count
        bad_j3 = dict(journal, transition=dict(journal["transition"], candidate_service_count=17))
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j3, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Invalid candidate service count", str(ctx.exception))

        # 4. Forged new_services
        bad_j4 = dict(journal, transition=dict(journal["transition"], new_services=["wrong_service"]))
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j4, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Invalid new services", str(ctx.exception))

        # 5. Tampered recovery_hashes
        bad_j5 = dict(journal, transition=dict(journal["transition"], recovery_hashes={"bad": "hash"}))
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j5, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Transition recovery_hashes", str(ctx.exception))

    def test_lifecycle_recovery_requires_bound_native_backend_recipe(self):
        bundle, _, preconds, live, tx_dir = self._build_lifecycle_fixture()
        driver = FakeLifecycleDriver()
        ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-lifecycle")
        journal = ra.load_activation_journal(tx_dir)
        transaction = ra.rt.ReleaseTransaction(tx_dir).load_journal()
        cases = []
        missing = json.loads(json.dumps(journal))
        missing.pop("prior_backend_recipe")
        cases.append(missing)
        for field, value in (("argv", ["/bin/sh", "-c", "true"]),
                             ("cwd", "/tmp"), ("env", {"HOME": "/other", "PREFIX": str(live / "usr")}),
                             ("prior_launcher_sha256", "0" * 64), ("pid", True)):
            bad = json.loads(json.dumps(journal))
            bad["prior_backend_recipe"][field] = value
            cases.append(bad)
        for bad in cases:
            with self.subTest(recipe=bad.get("prior_backend_recipe")):
                with self.assertRaises(ra.ActivationError):
                    ra.validate_activation_journal(bad, transaction, live, tx_dir, os.getuid())

    def test_suwayomi_lifecycle_rollback_never_relaunches_scheduler(self):
        """Rollback restores prior files and relaunches prior services, NEVER launching scheduler."""
        bundle, manifest, preconds, live, tx_dir = self._build_lifecycle_fixture()
        (bundle / "scripts/start_suwayomi.sh").write_text("# LAUNCHER\n", encoding="utf-8")
        (bundle / "scripts/server_supervisor.py").write_text("# SUPERVISOR\n", encoding="utf-8")

        driver = FakeLifecycleDriver()
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.running_processes["dashboard"] = [(1002, "dash_1002")]
        driver.healthy_services.update({"suwayomi", "dashboard", "suwayomi_scheduler"})

        # Fail candidate health for suwayomi
        driver.fail_candidate_health.add("suwayomi")

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.2, profile="suwayomi-lifecycle")

        # Rollback was executed:
        # Check services launched during rollback: suwayomi_scheduler must NOT be launched!
        rollback_launches = [e[1] for e in driver.events if e[0] == "launch"]
        self.assertNotIn("suwayomi_scheduler", rollback_launches)

    def test_suwayomi_jar_profile_scope_and_replace_only(self):
        """Profile suwayomi-jar allows ONLY exact JAR path with REPLACE; affects ONLY suwayomi."""
        # 1. Exact valid JAR replace entry
        entries = [{"remote": ra.SUWAYOMI_JAR_REMOTE, "action": ra.rt.ACTION_REPLACE}]
        affected, sup_aff, changed = ra.map_changed_destinations(entries, profile="suwayomi-jar")
        self.assertEqual(affected, {"suwayomi"})
        self.assertFalse(sup_aff)
        self.assertEqual(changed, [ra.SUWAYOMI_JAR_REMOTE])

        # 2. Disallowed actions (CREATE, DELETE, etc.)
        for act in (ra.rt.ACTION_CREATE, "REMOVE", None):
            with self.subTest(action=act), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations([{"remote": ra.SUWAYOMI_JAR_REMOTE, "action": act}], profile="suwayomi-jar")

        # 3. Disallowed paths
        for remote in (
            ra.SUWAYOMI_FILTER_REMOTE,
            ra.SUWAYOMI_LAUNCHER_REMOTE,
            ra.SUPERVISOR_REMOTE,
            ra.SERVICE_CATALOG_REMOTE,
            "/data/data/com.termux/files/home/bin/mobile-gateway",
            "/data/data/com.termux/files/usr/bin/dashboard",
        ):
            with self.subTest(remote=remote), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations([{"remote": remote, "action": ra.rt.ACTION_REPLACE}], profile="suwayomi-jar")

        # 4. Mixed changes rejected
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(
                [
                    {"remote": ra.SUWAYOMI_JAR_REMOTE, "action": ra.rt.ACTION_REPLACE},
                    {"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": ra.rt.ACTION_REPLACE},
                ],
                profile="suwayomi-jar",
            )

    def test_suwayomi_jar_refused_by_general_filter_lifecycle_profiles(self):
        """General, filter, and lifecycle profiles continue forbidding changed JAR."""
        entries = [{"remote": ra.SUWAYOMI_JAR_REMOTE, "action": ra.rt.ACTION_REPLACE}]
        for prof in ("general", "suwayomi-filter", "suwayomi-lifecycle"):
            with self.subTest(profile=prof), self.assertRaises(ra.ActivationError):
                ra.map_changed_destinations(entries, profile=prof)

    def test_suwayomi_jar_requires_current_16_runtime_and_fork_free_launcher(self):
        """Refuses legacy forking launcher, legacy 15 catalog, or non-foreground scheduler."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        sched_cmd = [f"{str(live / 'usr')}/bin/python3", f"{str(live / 'home')}/bin/suwayomi_daily_update.py", "--foreground"]

        # 1. Mismatched live launcher (e.g. legacy forking launcher)
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": sched_cmd}
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.healthy_services.add("suwayomi")

        bad_l_bytes = b"#!/bin/sh\n# LEGACY_FORKING_LAUNCHER\nexec java ...\n"
        (live / "home/bin/start_suwayomi.sh").write_bytes(bad_l_bytes)
        (bundle / "scripts/start_suwayomi.sh").write_bytes(bad_l_bytes)
        bad_l_sha, bad_l_size = ra.rt.compute_file_sha256(live / "home/bin/start_suwayomi.sh")
        for f in preconds["files"]:
            if f["path"] == ra.SUWAYOMI_LAUNCHER_REMOTE:
                f["sha256"] = bad_l_sha
                f["bytes"] = bad_l_size
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-jar")
        self.assertIn("Live launcher hash mismatch", str(ctx.exception))

        # Restore launcher
        clean_l_bytes = (REPO_ROOT / "scripts/start_suwayomi.sh").read_bytes()
        (live / "home/bin/start_suwayomi.sh").write_bytes(clean_l_bytes)
        (bundle / "scripts/start_suwayomi.sh").write_bytes(clean_l_bytes)
        clean_l_sha, clean_l_size = ra.rt.compute_file_sha256(live / "home/bin/start_suwayomi.sh")
        for f in preconds["files"]:
            if f["path"] == ra.SUWAYOMI_LAUNCHER_REMOTE:
                f["sha256"] = clean_l_sha
                f["bytes"] = clean_l_size
        shutil.rmtree(tx_dir)
        tx_dir.mkdir(parents=True, exist_ok=True)

        # 2. Mismatched live catalog (e.g. legacy 15 catalog)
        bad_c_bytes = b"# TAMPERED_CATALOG\n"
        (live / "home/.local/lib/mobile-server/service_catalog.py").write_bytes(bad_c_bytes)
        (bundle / "app/service_catalog.py").write_bytes(bad_c_bytes)
        bad_c_sha, bad_c_size = ra.rt.compute_file_sha256(live / "home/.local/lib/mobile-server/service_catalog.py")
        for f in preconds["files"]:
            if f["path"] == ra.SERVICE_CATALOG_REMOTE:
                f["sha256"] = bad_c_sha
                f["bytes"] = bad_c_size
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-jar")
        self.assertIn("Live catalog hash mismatch", str(ctx.exception))

        # Restore catalog
        clean_c_bytes = reviewed_16_catalog_source().encode()
        (live / "home/.local/lib/mobile-server/service_catalog.py").write_bytes(clean_c_bytes)
        (bundle / "app/service_catalog.py").write_bytes(clean_c_bytes)
        clean_c_sha, clean_c_size = ra.rt.compute_file_sha256(live / "home/.local/lib/mobile-server/service_catalog.py")
        for f in preconds["files"]:
            if f["path"] == ra.SERVICE_CATALOG_REMOTE:
                f["sha256"] = clean_c_sha
                f["bytes"] = clean_c_size
        shutil.rmtree(tx_dir)
        tx_dir.mkdir(parents=True, exist_ok=True)

        # 3. Old 15 services (missing suwayomi_scheduler)
        driver_15 = FakeLifecycleDriver()
        driver_15.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver_15.healthy_services.add("suwayomi")
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver_15, profile="suwayomi-jar")
        self.assertIn("requires exact 16 managed services", str(ctx.exception))
        shutil.rmtree(tx_dir)
        tx_dir.mkdir(parents=True, exist_ok=True)

        # 4. Foreground scheduler cmd drift
        driver_drift = FakeLifecycleDriver()
        driver_drift.services_cfg["suwayomi_scheduler"] = {"cmd": ["/usr/bin/python3", "bad_script.py"]}
        driver_drift.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver_drift.healthy_services.add("suwayomi")
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.apply(bundle, preconds, tx_dir, live, driver=driver_drift, profile="suwayomi-jar")
        self.assertIn("command drift", str(ctx.exception))

    def test_disabled_backend_reader_refuses_jar_write(self):
        bundle, preconditions, live, transaction = self._build_jar_fixture()
        marker = live / "home/.config/services_disabled/suwayomi"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"keep-disabled")
        before = (live / "home/suwayomi/bin/Suwayomi-Server.jar").read_bytes()
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": [str(live / "usr/bin/python3"), str(live / "home/bin/suwayomi_daily_update.py"), "--foreground"]}
        driver.running_processes["suwayomi"] = [(1001, "manual-reader")]
        with self.assertRaisesRegex(ra.ActivationError, "Disabled backend is still running"):
            ra.apply(bundle, preconditions, transaction, live, driver=driver, profile="suwayomi-jar")
        self.assertEqual((live / "home/suwayomi/bin/Suwayomi-Server.jar").read_bytes(), before)
        self.assertEqual(marker.read_bytes(), b"keep-disabled")
        self.assertFalse(any(event[0] in ("launch", "terminate") for event in driver.events))

    def test_disabled_backend_appearing_before_apply_blocks_write_and_backout(self):
        bundle, preconditions, live, transaction = self._build_jar_fixture()
        marker = live / "home/.config/services_disabled/suwayomi"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"keep-disabled")
        before = (live / "home/suwayomi/bin/Suwayomi-Server.jar").read_bytes()
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": [str(live / "usr/bin/python3"), str(live / "home/bin/suwayomi_daily_update.py"), "--foreground"]}
        original_lock = driver.lock
        service_locks = 0

        @contextlib.contextmanager
        def appearing_reader(name, *args, **kwargs):
            nonlocal service_locks
            with original_lock(name, *args, **kwargs):
                if name == "services":
                    service_locks += 1
                    if service_locks == 2:
                        driver.running_processes["suwayomi"] = [(1001, "late-manual-reader")]
                yield

        driver.lock = appearing_reader
        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconditions, transaction, live, driver=driver, profile="suwayomi-jar")
        self.assertEqual((live / "home/suwayomi/bin/Suwayomi-Server.jar").read_bytes(), before)
        self.assertEqual(marker.read_bytes(), b"keep-disabled")
        self.assertEqual(ra.load_activation_journal(transaction)["state"], ra.STATE_RECOVERY_REQUIRED)
        self.assertEqual(driver.running_processes["suwayomi"], [(1001, "late-manual-reader")])

    def test_suwayomi_jar_successful_apply(self):
        """Successful activation: old Java quiesced, JAR replaced, new Java launched healthy, supervisor unaffected."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        sched_cmd = [f"{str(live / 'usr')}/bin/python3", f"{str(live / 'home')}/bin/suwayomi_daily_update.py", "--foreground"]
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": sched_cmd}
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.healthy_services.add("suwayomi")

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-jar")
        self.assertEqual(res["state"], ra.STATE_COMPLETED)
        self.assertEqual(res["profile"], "suwayomi-jar")
        self.assertEqual(res["affected_services"], ["suwayomi"])

        # Check live JAR was replaced with candidate bytes
        live_jar = live / "home/suwayomi/bin/Suwayomi-Server.jar"
        self.assertEqual(live_jar.read_bytes(), b"PK\x03\x04CANDIDATE_PARALLEL_JAR_BYTES")
        self.assertEqual(stat.S_IMODE(live_jar.stat().st_mode), 0o644)

        # Verify supervisor was NOT restarted
        self.assertNotIn(("stop_daemon",), [e[:1] for e in driver.events])
        self.assertNotIn(("start_daemon",), [e[:1] for e in driver.events])

    def test_suwayomi_jar_survivor_java_blocks_write(self):
        """Survivor Java process blocks file write and sets RECOVERY_REQUIRED."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        sched_cmd = [f"{str(live / 'usr')}/bin/python3", f"{str(live / 'home')}/bin/suwayomi_daily_update.py", "--foreground"]
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": sched_cmd}
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.fail_quiescence = True

        live_jar = live / "home/suwayomi/bin/Suwayomi-Server.jar"
        orig_bytes = live_jar.read_bytes()

        with self.assertRaises(ra.QuiescenceError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-jar")

        # Live file untouched
        self.assertEqual(live_jar.read_bytes(), orig_bytes)
        act = ra.load_activation_journal(tx_dir)
        self.assertEqual(act["state"], ra.STATE_RECOVERY_REQUIRED)

    def test_suwayomi_jar_candidate_health_fail_rolls_back_coherently(self):
        """Candidate health failure stops candidate, restores old JAR, restarts backend with fork-free launcher."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        sched_cmd = [f"{str(live / 'usr')}/bin/python3", f"{str(live / 'home')}/bin/suwayomi_daily_update.py", "--foreground"]
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": sched_cmd}
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        # Candidate health check fails
        driver.fail_candidate_health.add("suwayomi")

        live_jar = live / "home/suwayomi/bin/Suwayomi-Server.jar"
        orig_bytes = live_jar.read_bytes()

        with self.assertRaises(ra.HealthCheckError):
            ra.apply(bundle, preconds, tx_dir, live, driver=driver, health_timeout=0.1, profile="suwayomi-jar")

        # Live JAR restored to original
        self.assertEqual(live_jar.read_bytes(), orig_bytes)
        self.assertEqual(stat.S_IMODE(live_jar.stat().st_mode), 0o644)

        # Journal rolled back
        act = ra.load_activation_journal(tx_dir)
        self.assertEqual(act["state"], ra.STATE_ROLLED_BACK)

        # Backend relaunched during rollback
        launches = [e[1] for e in driver.events if e[0] == "launch"]
        self.assertEqual(launches, ["suwayomi", "suwayomi"])

    def test_suwayomi_jar_journal_forgery_refused(self):
        """Forged journal profile, affected_services, or mode fails closed."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        sched_cmd = [f"{str(live / 'usr')}/bin/python3", f"{str(live / 'home')}/bin/suwayomi_daily_update.py", "--foreground"]
        driver = FakeLifecycleDriver()
        driver.services_cfg["suwayomi_scheduler"] = {"cmd": sched_cmd}
        driver.running_processes["suwayomi"] = [(1001, "sw_1001")]
        driver.healthy_services.add("suwayomi")

        res = ra.apply(bundle, preconds, tx_dir, live, driver=driver, profile="suwayomi-jar")
        self.assertEqual(res["state"], ra.STATE_COMPLETED)

        journal = ra.load_activation_journal(tx_dir)
        tx = ra.rt.ReleaseTransaction(tx_dir)
        tx_journal = tx.load_journal()

        # 1. Forged affected_services
        bad_j1 = dict(journal)
        bad_j1["affected_services"] = ["suwayomi", "dashboard"]
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j1, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Affected services mismatch", str(ctx.exception))

        # 2. Forged supervisor_affected
        bad_j2 = dict(journal)
        bad_j2["supervisor_affected"] = True
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(bad_j2, tx_journal, live, tx_dir, os.getuid())
        self.assertIn("Supervisor affected mismatch", str(ctx.exception))

        # 3. Mutated entry target_mode
        bad_tx = dict(tx_journal)
        bad_tx["entries"] = copy.deepcopy(tx_journal["entries"])
        for e in bad_tx["entries"]:
            if e.get("dest_path") == ra.SUWAYOMI_JAR_REMOTE or e.get("remote") == ra.SUWAYOMI_JAR_REMOTE:
                e["target_mode"] = "0o700"
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.validate_activation_journal(journal, bad_tx, live, tx_dir, os.getuid())
        self.assertIn("target mode mismatch", str(ctx.exception))

    def test_suwayomi_jar_plan_validation(self):
        """Dry plan returns affected_services == ['suwayomi'], total_count == 44; bad preconditions rejected."""
        bundle, preconds, live, tx_dir = self._build_jar_fixture()
        res = ra.plan(bundle, preconds, namespace_root=live, profile="suwayomi-jar")
        self.assertEqual(res["status"], "plan_ok")
        self.assertEqual(res["profile"], "suwayomi-jar")
        self.assertEqual(res["affected_services"], ["suwayomi"])
        self.assertFalse(res["supervisor_affected"])
        # The historical 44-file release grows when new owned assets are admitted.
        self.assertEqual(res["total_count"], len(preconds["files"]))
        self.assertEqual(res["unchanged_count"], res["total_count"] - 1)

        # Symlink precondition rejected
        bad_preconds = copy.deepcopy(preconds)
        for f in bad_preconds["files"]:
            if f["path"] == ra.SUWAYOMI_JAR_REMOTE:
                f["kind"] = "symlink"
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.plan(bundle, bad_preconds, namespace_root=live, profile="suwayomi-jar")
        self.assertIn("must be a regular file", str(ctx.exception))

        # Non-0644 mode precondition rejected
        bad_mode_preconds = copy.deepcopy(preconds)
        for f in bad_mode_preconds["files"]:
            if f["path"] == ra.SUWAYOMI_JAR_REMOTE:
                f["mode"] = "0o755"
        with self.assertRaises(ra.ActivationError) as ctx:
            ra.plan(bundle, bad_mode_preconds, namespace_root=live, profile="suwayomi-jar")
        self.assertIn("Precondition mode mismatch", str(ctx.exception))


    
class TestTermuxLifecycleDriverUnit(unittest.TestCase):
    """Unit tests for TermuxLifecycleDriver using mocked filesystem and proc."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()
        self.live_sup = self.root / "home/bin/server_supervisor.py"
        self.live_sup.parent.mkdir(parents=True, exist_ok=True)
        self.live_sup.write_text("# fake live supervisor\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_proc_process_identity(self):
        pid_dir = self.proc_root / "1234"
        pid_dir.mkdir()
        stat_line = "1234 (python3) S 1 1234 1234 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 543210 1000 200"
        (pid_dir / "stat").write_text(stat_line)

        driver = ta.TermuxLifecycleDriver(namespace_root=self.root, proc_root=self.proc_root)
        ident = driver.process_identity(1234)
        self.assertEqual(ident, "543210")

        zombie_dir = self.proc_root / "1235"
        zombie_dir.mkdir()
        zombie_stat = "1235 (python3) Z 1 1235 1235 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 543211 1000 200"
        (zombie_dir / "stat").write_text(zombie_stat)
        self.assertIsNone(driver.process_identity(1235))

    def test_backend_matching_rejects_sibling_java_and_shell_text(self):
        driver = ta.TermuxLifecycleDriver(namespace_root=self.root, proc_root=self.proc_root)
        jar = str(self.root / "home/suwayomi/bin/Suwayomi-Server.jar")
        commands = {
            5101: [str(self.root / "usr/bin/java"), "-jar", jar],
            5102: ["/other/usr/bin/java", "-jar", "/other/home/suwayomi/bin/Suwayomi-Server.jar"],
            5103: ["/usr/bin/java", "-Dproject=/tmp/suwayomi-upstream", "GradleDaemon"],
            5104: ["/bin/bash", "-c", f"cat {self.root / 'home/bin/start_suwayomi.sh'}"],
        }
        for pid, argv in commands.items():
            proc = self.proc_root / str(pid)
            proc.mkdir()
            (proc / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
            (proc / "stat").write_text(f"{pid} (java) S 1 {pid} {pid} 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 {pid}0 1000 200")
        driver._sup.matching_processes = lambda sid: [(pid, f"{pid}0") for pid in commands]
        self.assertEqual(driver.matching_processes("suwayomi"), [(5101, "51010")])

    def test_find_daemon_process_exact_and_decoy_rejection(self):
        # 1. Decoy process: python script running unrelated supervisor in /tmp
        decoy_dir = self.proc_root / "1111"
        decoy_dir.mkdir()
        decoy_stat = "1111 (python3) S 1 1111 1111 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 11110 1000 200"
        (decoy_dir / "stat").write_text(decoy_stat)
        (decoy_dir / "cmdline").write_bytes(b"/usr/bin/python3\0/tmp/other/server_supervisor.py\0--daemon\0")

        # 2. Exact approved process
        exact_dir = self.proc_root / "2222"
        exact_dir.mkdir()
        exact_stat = "2222 (python3) S 1 2222 2222 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 22220 1000 200"
        (exact_dir / "stat").write_text(exact_stat)
        cmdline = f"/usr/bin/python3\0-u\0{self.live_sup}\0--daemon\0"
        (exact_dir / "cmdline").write_bytes(cmdline.encode("utf-8"))

        driver = ta.TermuxLifecycleDriver(namespace_root=self.root, proc_root=self.proc_root)
        daemon_proc = driver.find_daemon_process()
        self.assertEqual(daemon_proc, (2222, "22220"))

    def test_find_daemon_process_ambiguity_refusal(self):
        # Two exact processes
        for pid in ("3331", "3332"):
            pdir = self.proc_root / pid
            pdir.mkdir()
            (pdir / "stat").write_text(f"{pid} (python3) S 1 {pid} {pid} 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 {pid}0 1000 200")
            cmdline = f"/usr/bin/python3\0{self.live_sup}\0--daemon\0"
            (pdir / "cmdline").write_bytes(cmdline.encode("utf-8"))

        driver = ta.TermuxLifecycleDriver(namespace_root=self.root, proc_root=self.proc_root)
        with self.assertRaises(ta.QuiescenceError) as ctx:
            driver.find_daemon_process()
        self.assertIn("Multiple matching supervisor daemon processes found", str(ctx.exception))

    def test_native_scheduler_matching_and_decoy_rejection(self):
        """Native suwayomi_scheduler matching matches foreground/daemon mode and rejects decoys."""
        sched_script = self.root / "home/bin/suwayomi_daily_update.py"
        sched_script.parent.mkdir(parents=True, exist_ok=True)
        sched_script.write_text("# fake scheduler script\n")

        driver = ta.TermuxLifecycleDriver(namespace_root=self.root, proc_root=self.proc_root)

        # 1. Valid foreground scheduler
        p1 = self.proc_root / "4001"
        p1.mkdir()
        (p1 / "stat").write_text("4001 (python3) S 1 4001 4001 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40010 1000 200")
        (p1 / "cmdline").write_bytes(f"/usr/bin/python3\0{sched_script}\0--foreground\0".encode("utf-8"))

        # 2. Valid daemon scheduler
        p2 = self.proc_root / "4002"
        p2.mkdir()
        (p2 / "stat").write_text("4002 (python3) S 1 4002 4002 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40020 1000 200")
        (p2 / "cmdline").write_bytes(f"/usr/bin/python3\0{sched_script}\0--daemon\0".encode("utf-8"))

        # 3. Decoy: -c decoy
        p3 = self.proc_root / "4003"
        p3.mkdir()
        (p3 / "stat").write_text("4003 (python3) S 1 4003 4003 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40030 1000 200")
        (p3 / "cmdline").write_bytes(b"/usr/bin/python3\0-c\0import time; time.sleep(60)\0--foreground\0")

        # 4. Decoy: -m decoy
        p4 = self.proc_root / "4004"
        p4.mkdir()
        (p4 / "stat").write_text("4004 (python3) S 1 4004 4004 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40040 1000 200")
        (p4 / "cmdline").write_bytes(b"/usr/bin/python3\0-m\0suwayomi_daily_update\0--foreground\0")

        # 5. Decoy: --now trigger
        p5 = self.proc_root / "4005"
        p5.mkdir()
        (p5 / "stat").write_text("4005 (python3) S 1 4005 4005 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40050 1000 200")
        (p5 / "cmdline").write_bytes(f"/usr/bin/python3\0{sched_script}\0--now\0".encode("utf-8"))

        # 6. Decoy: --status inquiry
        p6 = self.proc_root / "4006"
        p6.mkdir()
        (p6 / "stat").write_text("4006 (python3) S 1 4006 4006 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40060 1000 200")
        (p6 / "cmdline").write_bytes(f"/usr/bin/python3\0{sched_script}\0--status\0".encode("utf-8"))

        # 7. Zombie process
        p7 = self.proc_root / "4007"
        p7.mkdir()
        (p7 / "stat").write_text("4007 (python3) Z 1 4007 4007 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 40070 1000 200")
        (p7 / "cmdline").write_bytes(f"/usr/bin/python3\0{sched_script}\0--foreground\0".encode("utf-8"))

        matches = driver.matching_processes("suwayomi_scheduler")
        self.assertEqual(matches, [(4001, "40010"), (4002, "40020")])
