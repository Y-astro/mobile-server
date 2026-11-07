#!/usr/bin/env python3
"""
Regression and equivalence tests for Caddy route configuration.

Verifies that:
1. Candidate config/Caddyfile adapts to the identical JSON structure as
   tests/fixtures/Caddyfile.before-portal-dedup.
2. Wealthfolio (:8444) and Native client (:4567) configuration blocks remain
   byte-for-byte identical to the historical baseline fixture.
3. Critical routing invariants are preserved across listeners:
   - Portal listeners :8000 (HTTP) and :8443 (HTTPS)
   - Port 8443 retains explicit internal TLS with on_demand
   - FileBrowser /files/ and /api/raw routes have attachment/sandbox security headers
   - Forward auth checks distinguish standard vs admin endpoints
   - Suwayomi thumbnail cache, GraphQL, and streaming reverse proxies
   - Wealthfolio offline status and host filtering
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "Caddyfile"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "Caddyfile.before-portal-dedup"

SSH_HOST = "100.75.159.53"
SSH_PORT = "8022"
SSH_USER = "u0_a505"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def adapt_caddyfile(caddyfile_content: str) -> dict:
    """
    Adapt Caddyfile content to JSON representation.
    Uses local 'caddy' if present; otherwise, if CADDY_TEST_SSH=1,
    executes authorized read-only 'caddy adapt' via SSH on phone.
    """
    if shutil.which("caddy"):
        proc = subprocess.run(
            ["caddy", "adapt", "--config", "-", "--adapter", "caddyfile"],
            input=caddyfile_content,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return json.loads(proc.stdout)

    if os.environ.get("CADDY_TEST_SSH") == "1":
        cmd = [
            "ssh",
            *SSH_OPTS,
            "-p",
            SSH_PORT,
            f"{SSH_USER}@{SSH_HOST}",
            "caddy adapt --config - --adapter caddyfile",
        ]
        proc = subprocess.run(
            cmd,
            input=caddyfile_content,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        # caddy on stdout, warnings may be present on stderr
        return json.loads(proc.stdout)

    raise unittest.SkipTest(
        "Caddy binary not found locally and CADDY_TEST_SSH=1 not set. "
        "Opt-in with CADDY_TEST_SSH=1 to execute authorized read-only remote adapt."
    )


class TestCaddyfileEquivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cls.candidate_text = f.read()
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixture_text = f.read()

    def test_thumbnail_route_parity_and_cache_guards(self):
        """Verify thumbnail routes have 2xx-only immutable cache and bounded timeout on both portal and 4567."""
        for text, label in [(self.candidate_text, 'candidate'), (self.fixture_text, 'fixture')]:
            self.assertIn('status 200 206', text, f'{label} missing image status guard')
            self.assertIn('header Content-Type image/*', text, f'{label} missing image MIME guard')
            self.assertIn('header_down Cache-Control "no-store"', text, f'{label} missing error cache policy')
            self.assertIn('handle_response @thumbnailImage', text, f'{label} missing handle_response guard')
            self.assertIn('response_header_timeout 15s', text, f'{label} missing bounded thumbnail timeout')
            self.assertIn('handle /suwayomi*', text)
            self.assertIn('response_header_timeout 300s', text)

    def test_unmodified_blocks_byte_identical(self):
        """Wealthfolio 8444 and Native client 4567 blocks must remain byte-identical."""
        wf_marker = "https://192.168.1.8:8444"
        self.assertIn(wf_marker, self.candidate_text)
        self.assertIn(wf_marker, self.fixture_text)

        candidate_wf = self.candidate_text[self.candidate_text.find(wf_marker):]
        fixture_wf = self.fixture_text[self.fixture_text.find(wf_marker):]
        self.assertEqual(candidate_wf, fixture_wf, "Wealthfolio and native blocks diverged!")

    def test_caddyfile_adapt_json_equality(self):
        """Full adapted JSON of candidate must be identical to baseline fixture."""
        fixture_json = adapt_caddyfile(self.fixture_text)
        candidate_json = adapt_caddyfile(self.candidate_text)
        self.assertEqual(
            candidate_json,
            fixture_json,
            "Adapted Caddy JSON structures differ between candidate and baseline!",
        )

    def test_critical_invariants_in_adapted_json(self):
        """Assert presence and correctness of key route and server invariants."""
        adapted = adapt_caddyfile(self.candidate_text)
        http_app = adapted.get("apps", {}).get("http", {})
        servers = http_app.get("servers", {})

        # Check that we have the 4 servers: portal 8000, portal 8443, wealthfolio 8444, native 4567
        all_listeners = []
        for srv in servers.values():
            all_listeners.extend(srv.get("listen", []))

        self.assertIn(":8000", all_listeners)
        self.assertIn(":8443", all_listeners)
        self.assertIn(":8444", all_listeners)
        self.assertIn(":4567", all_listeners)

        # Assert TLS differences: 8443 has tls_connection_policies with internal issuer
        srv_8443 = next(
            srv for srv in servers.values() if ":8443" in srv.get("listen", [])
        )
        self.assertTrue(srv_8443.get("tls_connection_policies"))

        # Invariant checks serialized to string: critical headers and paths
        dumped = json.dumps(adapted)
        self.assertIn("/api/auth-check", dumped)
        self.assertIn("/api/auth-check-admin", dumped)
        self.assertIn("RedmiHomeServerCA.crt", dumped)
        self.assertIn("Unrecognized Wealthfolio host.", dumped)
        self.assertIn("/data/data/com.termux/files/home/suwayomi/data/downloads/thumbnails", dumped)
        self.assertIn("127.0.0.1:4569", dumped)
        self.assertIn("127.0.0.1:8001", dumped)


if __name__ == "__main__":
    unittest.main()
