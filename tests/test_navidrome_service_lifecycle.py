#!/usr/bin/env python3
"""Tests for 17-to-18 service transition under 'navidrome-service' profile."""

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import hashlib

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

import app.service_catalog as service_catalog
import app.navidrome as launcher
import scripts.termux_activation as ta
import scripts.release_activation as ra
from tests import test_antra_service_lifecycle as antra_tests
from tests import test_release_activation as activation_tests


class NavidromeServiceLifecycleTests(unittest.TestCase):
    def test_launcher_rejects_changed_binary_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "navidrome"
            binary.write_bytes(b"verified fixture")
            with patch.object(launcher, "EXPECTED_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()):
                launcher.verify_binary(binary)
                link = Path(directory) / "linked"
                link.symlink_to(binary)
                with self.assertRaises(FileNotFoundError):
                    launcher.verify_binary(link)
                binary.write_bytes(b"changed bytes")
                with self.assertRaises(ValueError):
                    launcher.verify_binary(binary)

    def setUp(self):
        self.home = "/data/data/com.termux/files/home"
        self.prefix = "/data/data/com.termux/files/usr"
        self.cand_services = service_catalog.runtime_services(self.home, self.prefix)
        # Prior 17 services
        self.prior_services = {k: v for k, v in self.cand_services.items() if k != "navidrome"}

    def test_catalog_transition_17_to_18_success(self):
        """navidrome-service validates exact 17 prior -> 18 candidate services transition."""
        self.assertEqual(len(self.prior_services), 17)
        self.assertEqual(len(self.cand_services), 18)
        ta.validate_catalog_transition(
            self.prior_services, self.cand_services, self.home, self.prefix, profile="navidrome-service"
        )

    def test_catalog_transition_drift_refusal(self):
        """navidrome-service rejects prior command drift or wrong candidate port/cmd."""
        drifted = copy.deepcopy(self.cand_services)
        drifted["navidrome"]["port"] = 4534
        with self.assertRaises(RuntimeError):
            ta.validate_catalog_transition(
                self.prior_services, drifted, self.home, self.prefix, profile="navidrome-service"
            )

        # Prior service modified
        drifted_prior = copy.deepcopy(self.prior_services)
        drifted_prior["ssh"]["port"] = 22
        with self.assertRaises(RuntimeError):
            ta.validate_catalog_transition(
                drifted_prior, self.cand_services, self.home, self.prefix, profile="navidrome-service"
            )

    def test_general_profile_accepts_18_services(self):
        """General profile permits both 17-service and 18-service catalogs."""
        ta.validate_catalog_transition(
            self.cand_services, self.cand_services, self.home, self.prefix, profile="general"
        )

    def test_plan_validation_permits_only_allowed_assets(self):
        """navidrome-service allows launcher, catalog, Caddyfile, optional supervisor; rejects others."""
        allowed_entries = [
            {"remote": ra.SERVICE_CATALOG_REMOTE, "action": "REPLACE"},
            {"remote": "/data/data/com.termux/files/home/.local/lib/mobile-server/navidrome.py", "action": "CREATE"},
            {"remote": "/data/data/com.termux/files/home/.config/caddy/Caddyfile", "action": "REPLACE"},
        ]
        # Should succeed
        aff, sup, _ = ra.map_changed_destinations(allowed_entries, profile="navidrome-service")
        self.assertIn("navidrome", aff)
        self.assertIn("caddy", aff)
        self.assertTrue(sup)

        # Unauthorized entry (e.g. sshd)
        bad_entries = allowed_entries + [{"remote": "/data/data/com.termux/files/usr/bin/sshd", "action": "REPLACE"}]
        with self.assertRaises(ra.ActivationError):
            ra.map_changed_destinations(bad_entries, profile="navidrome-service")

    def test_native_matching_rejects_foreign_config_and_python_code(self):
        fixture = antra_tests.AntraServiceLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        proc = fixture.root / "navidrome_proc"
        proc.mkdir()
        launcher = str(fixture.home / ".local/lib/mobile-server/navidrome.py")
        binary = str(fixture.home / ".local/opt/navidrome/0.64.2/navidrome")
        config = str(fixture.home / ".config/navidrome/navidrome.toml")
        cases = [
            ["/usr/bin/python3", "-u", launcher],
            [binary, "--configfile", config],
            ["/usr/bin/proot", "-r", "/debian", binary, "--configfile", config],
            ["/usr/bin/python3", "-c", "pass", launcher],
            ["/other/navidrome", "--configfile", config],
            [binary, "--configfile", "/other/config.toml"],
            ["/usr/bin/proot", "/tmp/navidrome-other", "--configfile", config],
        ]
        for index, args in enumerate(cases, 1001):
            entry = proc / str(index)
            entry.mkdir()
            (entry / "cmdline").write_bytes(("\0".join(args) + "\0").encode())
            (entry / "stat").write_text(
                f"{index} (navidrome) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {index} 0 0\n"
            )
        driver = ta.TermuxLifecycleDriver(namespace_root=fixture.live, proc_root=proc)
        with patch.multiple(antra_tests.sup, HOME=fixture.home, PREFIX=fixture.usr, PROC=proc):
            expected = [1001, 1002, 1003]
            self.assertEqual([pid for pid, _ in driver.matching_processes("navidrome")], expected)
            self.assertEqual(sorted(pid for pid, _ in antra_tests.sup.matching_processes("navidrome")), expected)

    def test_activation_and_rollback_preserve_running_antra(self):
        fixture = antra_tests.AntraServiceLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.live_cat.write_text(activation_tests.reviewed_17_catalog_source())
        sha, size = ra.rt.compute_file_sha256(fixture.live_cat)
        entry = next(e for e in fixture.preconds["files"] if e["path"] == ra.SERVICE_CATALOG_REMOTE)
        entry.update(kind="file", sha256=sha, bytes=size, mode="0o600")
        fixture.bundle_cat.write_bytes((ROOT / "app/service_catalog.py").read_bytes())
        shutil.copyfile(ROOT / "app/navidrome.py", fixture.bundle / "app/navidrome.py")
        fixture.preconds["files"].append({
            "path": "/data/data/com.termux/files/home/.local/lib/mobile-server/navidrome.py",
            "kind": "absent",
        })
        prior = service_catalog.runtime_services(str(fixture.home), str(fixture.usr))
        prior.pop("navidrome")
        driver = antra_tests.AntraFakeLifecycleDriver(prior, fixture.live)
        original = [(7001, "antra-start")]
        driver.running_processes["antra"] = list(original)
        driver.healthy_services.update({"dashboard", "caddy", "navidrome", "antra"})
        result = ra.apply(fixture.bundle, fixture.preconds, fixture.tx_dir, fixture.live,
                          driver=driver, profile="navidrome-service")
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(driver.services()), 18)
        self.assertEqual(driver.matching_processes("antra"), original)
        ra.rollback(fixture.tx_dir, namespace_root=fixture.live, driver=driver)
        self.assertEqual(len(driver.services()), 17)
        self.assertEqual(driver.matching_processes("antra"), original)
        self.assertEqual(driver.matching_processes("navidrome"), [])


if __name__ == "__main__":
    unittest.main()
