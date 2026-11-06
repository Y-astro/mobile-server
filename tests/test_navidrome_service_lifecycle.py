#!/usr/bin/env python3
"""Tests for 17-to-18 service transition under 'navidrome-service' profile."""

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

import service_catalog
import termux_activation as ta
import release_activation as ra


class NavidromeServiceLifecycleTests(unittest.TestCase):
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
        aff, sup = ra.validate_activation_plan(allowed_entries, profile="navidrome-service")
        self.assertIn("navidrome", aff)
        self.assertIn("caddy", aff)
        self.assertTrue(sup)

        # Unauthorized entry (e.g. sshd)
        bad_entries = allowed_entries + [{"remote": "/data/data/com.termux/files/usr/bin/sshd", "action": "REPLACE"}]
        with self.assertRaises(ra.ActivationError):
            ra.validate_activation_plan(bad_entries, profile="navidrome-service")


if __name__ == "__main__":
    unittest.main()
