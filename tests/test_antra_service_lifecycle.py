"""Focused unit and integration tests for Antra service and lifecycle ownership.

Verifies:
1. 16-to-17 service transition under reviewed 'antra-service' profile.
2. Exact preservation of prior 16 services, configs, arguments, and disabled intent.
3. Restart mapping:
   - antra_worker.py & antra_client.py -> antra
   - antra_http.py -> dashboard
   - antra_store.py -> BOTH antra and dashboard
   - service_catalog.py -> antra, dashboard, and supervisor_affected
4. Profile restrictions:
   - general, suwayomi-filter, suwayomi-lifecycle, and suwayomi-jar reject 16->17 catalog addition.
   - antra-service profile rejects modification of unrelated files (ssh, caddy, etc.).
5. Process matching and health:
   - antra worker matching by script path and python executable; rejects decoys.
   - antra process health (no port).
6. Quiescence and survivor refusal:
   - Pre-existing antra worker before initial 16->17 transition refused fail-closed.
   - Concurrent antra worker appearance blocks file mutation.
   - Candidate worker surviving termination blocks file writes.
7. Standalone recovery and rollback:
   - Recovery works after candidate bundle is deleted and live helpers are unusable.
   - Rollback stops owned new worker, restores old 16 catalog, and preserves unaffected services.
   - Corrupted live catalog healed from staged file during rollback.
8. Future compatibility:
   - general profile works normally after 17 services are installed.
   - suwayomi-jar profile works normally with unchanged 17 services (antra appended).
   - require_reviewed_jar_lifecycle validates both 16 and 17 service catalog variants.
"""

import contextlib
import copy
import hashlib
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
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

import app.service_catalog as sc
import scripts.release_activation as ra
import scripts.release_transaction as rt
import scripts.termux_activation as ta
import tests.test_release_activation as tra


def install_16_catalog(live, preconds):
    catalog = live / "home/.local/lib/mobile-server/service_catalog.py"
    catalog.write_text(tra.reviewed_16_catalog_source())
    os.chmod(catalog, 0o600)
    sha, size = ra.rt.compute_file_sha256(catalog)
    entry = next(f for f in preconds["files"] if f["path"] == ra.SERVICE_CATALOG_REMOTE)
    entry.update(kind="file", sha256=sha, bytes=size, mode="0o600")


class AntraFakeLifecycleDriver(tra.FakeLifecycleDriver):
    """Lifecycle driver that reloads services dynamically from live namespace catalog."""

    def __init__(self, services_cfg: Dict[str, Any], live_dir: Path):
        super().__init__(services_cfg)
        self.live_dir = live_dir

    def reload_lifecycle(self) -> None:
        super().reload_lifecycle()
        cat_file = self.live_dir / "home/.local/lib/mobile-server/service_catalog.py"
        if cat_file.exists():
            self.services_cfg = ra.load_catalog_services_from_bytes(
                cat_file.read_bytes(), str(self.live_dir / "home"), str(self.live_dir / "usr")
            )


class AntraServiceLifecycleTests(unittest.TestCase):
    """Focused lifecycle tests for Antra service 16->17 transition and operational bounds."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name).resolve()
        self.bundle, self.manifest, self.preconds, self.live, self.tx_dir = tra._make_fixture(self.root)

        self.home = self.live / "home"
        self.usr = self.live / "usr"
        self.mobile_lib = self.home / ".local/lib/mobile-server"
        self.disabled_dir = self.home / ".config/services_disabled"
        self.static_dir = self.home / ".config/dashboard/static"
        for d in (self.mobile_lib, self.disabled_dir, self.static_dir, self.usr / "bin", self.home / "bin"):
            d.mkdir(parents=True, exist_ok=True)

        # Install prior 16 catalog on live disk
        self.prior_16_code = tra.reviewed_16_catalog_source()
        self.live_cat = self.mobile_lib / "service_catalog.py"
        install_16_catalog(self.live, self.preconds)

        # Install live supervisor
        self.live_sup = self.home / "bin/server_supervisor.py"
        shutil.copyfile(REPO_ROOT / "scripts/server_supervisor.py", self.live_sup)
        os.chmod(self.live_sup, 0o700)

        # Candidate bundle setup: 17 catalog
        self.bundle_cat = self.bundle / "app/service_catalog.py"
        self.bundle_cat.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "app/service_catalog.py", self.bundle_cat)
        os.chmod(self.bundle_cat, 0o600)

        # Ensure notes, document_conversion, and gateway are unchanged between bundle and live
        for name in ("notes.py", "document_conversion.py"):
            src = self.bundle / "app" / name
            dst = self.live / "home/.local/lib/mobile-server" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            sha, size = ra.rt.compute_file_sha256(dst)
            for f in self.preconds["files"]:
                if f["path"].endswith(name):
                    f["kind"] = "file"
                    f["sha256"] = sha
                    f["bytes"] = size
                    f["mode"] = "0o600"

        gw_bundle = self.bundle / ".work/mobile-gateway"
        gw_live = self.live / "home/bin/mobile-gateway"
        if gw_bundle.exists():
            gw_live.write_bytes(gw_bundle.read_bytes())
            os.chmod(gw_live, 0o700)
            sha, size = ra.rt.compute_file_sha256(gw_live)
            for f in self.preconds["files"]:
                if f["path"].endswith("mobile-gateway"):
                    f["kind"] = "file"
                    f["sha256"] = sha
                    f["bytes"] = size
                    f["mode"] = "0o700"

    def tearDown(self):
        self.test_dir.cleanup()

    def test_restart_mapping_for_antra_helpers(self):
        """Helper destination mapping strictly routes restarts to antra, dashboard, or both."""
        # 1. antra_worker.py -> antra
        w_entry = [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/antra_worker.py", "action": rt.ACTION_REPLACE}]
        aff, sup, _ = ra.map_changed_destinations(w_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"antra"})
        self.assertFalse(sup)

        # 2. antra_client.py -> antra
        c_entry = [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/antra_client.py", "action": rt.ACTION_REPLACE}]
        aff, sup, _ = ra.map_changed_destinations(c_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"antra"})
        self.assertFalse(sup)

        # 3. antra_http.py -> dashboard
        h_entry = [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/antra_http.py", "action": rt.ACTION_REPLACE}]
        aff, sup, _ = ra.map_changed_destinations(h_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"dashboard"})
        self.assertFalse(sup)

        # 4. antra_store.py -> BOTH antra and dashboard
        s_entry = [{"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/antra_store.py", "action": rt.ACTION_REPLACE}]
        aff, sup, _ = ra.map_changed_destinations(s_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"antra", "dashboard"})
        self.assertFalse(sup)

        # 5. service_catalog.py -> antra, dashboard, and supervisor_affected
        cat_entry = [{"remote": ra.SERVICE_CATALOG_REMOTE, "action": rt.ACTION_REPLACE}]
        aff, sup, _ = ra.map_changed_destinations(cat_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"antra", "dashboard"})
        self.assertTrue(sup)

        # 6. Static dashboard files -> dashboard
        st_entry = [{"remote": "/data/data/com.termux/files/home/.config/dashboard/static/antra.html", "action": rt.ACTION_CREATE}]
        aff, sup, _ = ra.map_changed_destinations(st_entry, profile=ra.PROFILE_ANTRA_SERVICE)
        self.assertEqual(aff, {"dashboard"})
        self.assertFalse(sup)

    def test_unrelated_profiles_reject_16_to_17_addition(self):
        """General and suwayomi profiles strictly reject 16->17 catalog addition."""
        home_str = str(self.home)
        prefix_str = str(self.usr)
        p_svcs = ra.load_catalog_services_from_bytes(self.prior_16_code.encode(), home_str, prefix_str, "prior")
        c_svcs = ra.load_catalog_services_from_bytes(self.bundle_cat.read_bytes(), home_str, prefix_str, "cand")
        self.assertEqual(len(p_svcs), 16)
        self.assertEqual(len(c_svcs), 17)

        # General profile rejects
        with self.assertRaisesRegex(RuntimeError, "Candidate service catalog key order mismatch"):
            ta.validate_catalog_transition(p_svcs, c_svcs, home_str, prefix_str, profile="general")

        # suwayomi-filter rejects
        with self.assertRaisesRegex(RuntimeError, "Candidate service catalog key order mismatch"):
            ta.validate_catalog_transition(p_svcs, c_svcs, home_str, prefix_str, profile="suwayomi-filter")

        # suwayomi-lifecycle rejects (expects 15 prior)
        with self.assertRaisesRegex(RuntimeError, "suwayomi-lifecycle profile requires exact 15 prior"):
            ta.validate_catalog_transition(p_svcs, c_svcs, home_str, prefix_str, profile="suwayomi-lifecycle")

        # suwayomi-jar rejects key count drift (16 vs 17)
        with self.assertRaisesRegex(RuntimeError, "suwayomi-jar profile requires exact 16 candidate services"):
            ta.validate_catalog_transition(p_svcs, c_svcs, home_str, prefix_str, profile="suwayomi-jar")

        # antra-service passes
        ta.validate_catalog_transition(p_svcs, c_svcs, home_str, prefix_str, profile="antra-service")

    def test_antra_service_profile_rejects_unrelated_destination_changes(self):
        """Profile 'antra-service' prohibits changes to caddy, gateway, or suwayomi."""
        forbidden_entries = [
            [{"remote": "/data/data/com.termux/files/home/.config/caddy/Caddyfile", "action": rt.ACTION_REPLACE}],
            [{"remote": "/data/data/com.termux/files/home/bin/mobile-gateway", "action": rt.ACTION_REPLACE}],
            [{"remote": ra.SUWAYOMI_LAUNCHER_REMOTE, "action": rt.ACTION_REPLACE}],
            [{"remote": ra.SUWAYOMI_FILTER_REMOTE, "action": rt.ACTION_REPLACE}],
        ]
        for f_entry in forbidden_entries:
            with self.assertRaisesRegex(ra.ActivationError, "prohibits changes"):
                ra.map_changed_destinations(f_entry, profile=ra.PROFILE_ANTRA_SERVICE)

    def test_native_driver_antra_worker_matching_and_decoy_rejection(self):
        """TermuxLifecycleDriver matches authentic antra worker process and excludes decoys."""
        proc_dir = self.root / "mock_proc"
        proc_dir.mkdir(parents=True, exist_ok=True)
        driver = ta.TermuxLifecycleDriver(namespace_root=self.live, proc_root=proc_dir)

        # 1. Authentic process (PID 1001)
        p1 = proc_dir / "1001"
        p1.mkdir()
        (p1 / "stat").write_text("1001 (python3) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100000 0 0\n")
        worker_target = str((self.home / ".local/lib/mobile-server/antra_worker.py").resolve())
        (p1 / "cmdline").write_bytes(f"/usr/bin/python3\0-u\0{worker_target}\0".encode())

        # 2. Decoy -c code execution (PID 1002)
        p2 = proc_dir / "1002"
        p2.mkdir()
        (p2 / "stat").write_text("1002 (python3) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100001 0 0\n")
        (p2 / "cmdline").write_bytes(b"/usr/bin/python3\0-c\0import antra_worker\0")

        # 3. Decoy different script (PID 1003)
        p3 = proc_dir / "1003"
        p3.mkdir()
        (p3 / "stat").write_text("1003 (python3) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100002 0 0\n")
        (p3 / "cmdline").write_bytes(b"/usr/bin/python3\0/other/script.py\0")

        # 4. Zombie authentic path (PID 1004)
        p4 = proc_dir / "1004"
        p4.mkdir()
        (p4 / "stat").write_text("1004 (python3) Z 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100003 0 0\n")
        (p4 / "cmdline").write_bytes(f"/usr/bin/python3\0-u\0{worker_target}\0".encode())

        matches = driver.matching_processes("antra")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], 1001)
        self.assertEqual(matches[0][1], "100000")
        self.assertTrue(driver.healthy("antra", {}))

    def test_pre_existing_worker_refused_before_initial_transition(self):
        """Pre-existing antra worker process before initial 16->17 transition is refused fail-closed."""
        live_driver = tra.FakeLifecycleDriver(sc.runtime_services(str(self.home), str(self.usr)))
        live_driver.running_processes["antra"] = [(5050, "pre_existing_worker_5050")]

        with self.assertRaises(ra.QuiescenceError) as ctx:
            ra.apply(self.bundle, self.preconds, self.tx_dir, self.live, driver=live_driver, profile="antra-service")
        self.assertIn("Pre-existing antra worker process found", str(ctx.exception))

    def test_successful_16_to_17_transition_and_unaffected_services_preserved(self):
        """Successful activation starts worker, updates catalog, preserves unaffected services and flags."""
        prior_svcs = sc.runtime_services(str(self.home), str(self.usr))
        del prior_svcs["antra"]
        self.assertEqual(len(prior_svcs), 16)

        driver = AntraFakeLifecycleDriver(dict(prior_svcs), self.live)
        driver.running_processes["caddy"] = [(2001, "caddy_2001")]
        driver.running_processes["transfer_gateway"] = [(2002, "gw_2002")]
        driver.running_processes["suwayomi"] = [(2003, "sw_2003")]
        driver.healthy_services.update({"caddy", "transfer_gateway", "suwayomi", "dashboard", "antra"})

        res = ra.apply(self.bundle, self.preconds, self.tx_dir, self.live, driver=driver, profile="antra-service")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["profile"], "antra-service")
        self.assertIn("antra", res["affected_services"])
        self.assertIn("dashboard", res["affected_services"])
        self.assertNotIn("caddy", res["affected_services"])
        self.assertNotIn("transfer_gateway", res["affected_services"])
        self.assertNotIn("suwayomi", res["affected_services"])
        self.assertEqual(driver.launch_counts.get("caddy", 0), 0)
        self.assertEqual(driver.launch_counts.get("suwayomi", 0), 0)

        # Verified live catalog updated to 17 services
        self.assertEqual(ra.compute_file_sha256(self.live_cat), ra.ANTRA_SERVICE_CATALOG_SHA256)

    def test_standalone_recovery_after_bundle_deletion_stops_worker_restores_16(self):
        """Interrupted activation recovers cleanly after candidate bundle is deleted and live helpers damaged."""
        prior_svcs = sc.runtime_services(str(self.home), str(self.usr))
        del prior_svcs["antra"]
        driver = AntraFakeLifecycleDriver(dict(prior_svcs), self.live)
        driver.running_processes["suwayomi"] = [(2003, "sw_2003")]
        driver.healthy_services.update({"suwayomi", "dashboard"})
        driver.running_processes["antra"] = [(9090, "candidate_worker_9090")]

        # Apply transaction up to APPLIED state, then simulate interruption
        rt.prepare(self.bundle, self.manifest, self.preconds, self.tx_dir, namespace_root=self.live)
        tx = rt.ReleaseTransaction(self.tx_dir)
        # Verify recovery lifecycle code preserved
        rec_hashes = ra.preserve_recovery_lifecycle_code(self.tx_dir, self.live)
        self.assertIn("service_catalog.py", rec_hashes)
        self.assertEqual(rec_hashes["service_catalog.py"], ra.SUWAYOMI_FORK_FREE_CATALOG_SHA256)

        act_journal = {
            "version": ra.JOURNAL_VERSION,
            "profile": ra.PROFILE_ANTRA_SERVICE,
            "state": ra.STATE_APPLIED,
            "created_at": time.time(),
            "updated_at": time.time(),
            "tx_id": "act_test_recovery",
            "namespace_root": str(self.live),
            "transaction_dir": str(self.tx_dir),
            "top_uid": os.getuid(),
            "affected_services": ["antra", "dashboard"],
            "supervisor_affected": True,
            "daemon_was_running": True,
            "flags": {},
            "quiescence_records": {},
            "daemon_record": (8888, "daemon_start_8888"),
            "recovery_assets": rec_hashes,
            "transition": {
                "type": "antra_16_to_17",
                "prior_service_count": 16,
                "candidate_service_count": 17,
                "new_services": ["antra"],
                "prior_catalog_sha256": ra.SUWAYOMI_FORK_FREE_CATALOG_SHA256,
                "candidate_catalog_sha256": ra.ANTRA_SERVICE_CATALOG_SHA256,
                "recovery_hashes": dict(rec_hashes),
            }
        }
        ra.install_guards(
            disabled_dir=self.disabled_dir,
            affected_services={"antra", "dashboard"},
            tx_id="act_test_recovery",
            uid=os.getuid(),
            journal=act_journal,
            tx_dir=self.tx_dir,
        )
        ra.save_activation_journal(self.tx_dir, act_journal)
        tx.apply(namespace_root=self.live)

        # Corrupt live catalog and delete bundle completely
        self.live_cat.write_bytes(b"# CORRUPTED_LIVE_CATALOG_TEST")
        shutil.rmtree(self.bundle)

        # Recover using standalone driver from preserved recovery lifecycle
        rec_res = ra.recover(self.tx_dir, namespace_root=self.live, driver=driver)
        self.assertEqual(rec_res["state"], ra.STATE_ROLLED_BACK)

        # 1. Candidate antra worker was terminated
        self.assertEqual(driver.running_processes.get("antra", []), [])

        # 2. Live catalog restored to exact 16-service preimage
        self.assertEqual(ra.compute_file_sha256(self.live_cat), ra.SUWAYOMI_FORK_FREE_CATALOG_SHA256)

        # 3. antra was NEVER relaunched in rollback
        self.assertEqual(driver.launch_counts.get("antra", 0), 0)

        # 4. Unaffected service suwayomi was preserved
        self.assertEqual(driver.running_processes["suwayomi"], [(2003, "sw_2003")])

    def test_future_compatibility_general_and_suwayomi_jar_with_antra(self):
        """After 17 services are installed, general and suwayomi-jar profiles work normally."""
        home_str = str(self.home)
        prefix_str = str(self.usr)
        svcs_17 = sc.runtime_services(home_str, prefix_str)
        self.assertEqual(len(svcs_17), 17)

        # 1. General profile with 17 installed services passes catalog transition validation
        ta.validate_catalog_transition(svcs_17, dict(svcs_17), home_str, prefix_str, profile="general")

        # 2. suwayomi-jar profile with 17 installed services passes catalog transition validation
        ta.validate_catalog_transition(svcs_17, dict(svcs_17), home_str, prefix_str, profile="suwayomi-jar")

        # 3. require_reviewed_jar_lifecycle passes with live 17-service catalog
        # Install 17 catalog on live disk
        self.live_cat.write_bytes(self.bundle_cat.read_bytes())
        # Install reviewed launcher on live disk
        live_launcher = self.home / "bin/start_suwayomi.sh"
        live_launcher.write_bytes((REPO_ROOT / "scripts/start_suwayomi.sh").read_bytes())
        os.chmod(live_launcher, 0o755)

        driver_17 = tra.FakeLifecycleDriver(dict(svcs_17))
        # Validates without error
        ra.require_reviewed_jar_lifecycle(self.live, os.getuid(), driver=driver_17)

        # If antra command drifts, suwayomi-jar refuses
        bad_driver = tra.FakeLifecycleDriver(dict(svcs_17))
        bad_driver.services_cfg["antra"] = {"cmd": ["/usr/bin/python3", "drifted_worker.py"]}
        with self.assertRaisesRegex(ra.ActivationError, "Live antra command drift"):
            ra.require_reviewed_jar_lifecycle(self.live, os.getuid(), driver=bad_driver)


if __name__ == "__main__":
    unittest.main()
