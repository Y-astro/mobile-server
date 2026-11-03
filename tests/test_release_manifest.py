"""
test_release_manifest.py: Unit and fixture tests for release manifest generation and validation.
"""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_SCRIPT = REPO_ROOT / "scripts/release-manifest.py"
spec = importlib.util.spec_from_file_location("release_manifest", MANIFEST_SCRIPT)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

DEFAULT_GATEWAY_LOCAL = rm.DEFAULT_GATEWAY_LOCAL
DEFAULT_GATEWAY_REMOTE = rm.DEFAULT_GATEWAY_REMOTE
FORBIDDEN_SECRET_PATTERNS = rm.FORBIDDEN_SECRET_PATTERNS
ManifestError = rm.ManifestError
generate_manifest = rm.generate_manifest
validate_manifest = rm.validate_manifest
validate_path_safety = rm.validate_path_safety
compute_sha256_and_size = rm.compute_sha256_and_size
is_forbidden_secret_or_runtime_path = rm.is_forbidden_secret_or_runtime_path
load_baseline_mapping = rm.load_baseline_mapping
get_canonical_ownership = rm.get_canonical_ownership


class TestReleaseManifest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_minimal_bundle(self) -> Path:
        """Create a complete valid fixture bundle containing all baseline release assets."""
        bundle_root = self.test_root / "bundle"
        bundle_root.mkdir(parents=True, exist_ok=True)

        # Copy deployment-files.json into fixture config/
        cfg_dir = bundle_root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "config/deployment-files.json", cfg_dir / "deployment-files.json")

        baseline_mapping = load_baseline_mapping(bundle_root)
        for rel_path in baseline_mapping.keys():
            src_file = REPO_ROOT / rel_path
            dst_file = bundle_root / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        return bundle_root

    def test_reproduce_and_reject_source_prefix_lookalike(self):
        """Reject paths like application-data/fixture.txt that trick startswith('app/')."""
        with self.assertRaises(ManifestError) as ctx:
            validate_path_safety(
                "application-data/fixture.txt",
                "/data/data/com.termux/files/home/test",
                self.test_root,
            )
        self.assertIn("not inside allowed source roots", str(ctx.exception))

    def test_reproduce_and_reject_parent_symlink_escape(self):
        """Reject parent directory symlinks like app/linked/fixture.py pointing outside root."""
        bundle_root = self._create_minimal_bundle()
        outside_dir = self.test_root / "outside_dir"
        outside_dir.mkdir()
        secret_file = outside_dir / "fixture.py"
        secret_file.write_text("print('secret')", encoding="utf-8")

        linked_dir = bundle_root / "app/linked"
        try:
            linked_dir.symlink_to(outside_dir)
        except OSError:
            self.skipTest("Symlinks not supported")

        with self.assertRaises(ManifestError) as ctx:
            validate_path_safety(
                "app/linked/fixture.py",
                "/data/data/com.termux/files/home/.local/lib/mobile-server/fixture.py",
                bundle_root,
            )
        self.assertIn("Refused symlink escape", str(ctx.exception))

    def test_reproduce_and_reject_destination_substitution(self):
        """Reject candidate destination substitution (e.g. app/auth.py -> .ssh/authorized_keys)."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        for entry in manifest_entries:
            if entry["local"] == "app/auth.py":
                entry["remote"] = "/data/data/com.termux/files/home/.ssh/authorized_keys"

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_entries, bundle_root)
        self.assertTrue(
            "Destination substitution rejected" in str(ctx.exception)
            or "Refused secret or runtime class" in str(ctx.exception)
        )

    def test_reproduce_and_reject_missing_torrc(self):
        """Candidate validation must fail when config/torrc is missing."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        # Omit config/torrc from candidate manifest
        filtered_entries = [e for e in manifest_entries if e["local"] != "config/torrc"]
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(filtered_entries, bundle_root)
        self.assertIn("config/torrc", str(ctx.exception))

        # Tamper config/torrc content
        torrc_file = bundle_root / "config/torrc"
        torrc_file.write_text("SocksPort 9051\n", encoding="utf-8")
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_entries, bundle_root)
        self.assertTrue(
            "SHA-256 hash mismatch" in str(ctx.exception)
            or "Byte size mismatch" in str(ctx.exception)
        )

    def test_independent_installed_assets_coverage(self):
        """Independently assert installed assets coverage without relying on shared constants."""
        required_independent = [
            "web/admin.html",
            "web/family.html",
            "web/login.html",
            "web/portal.css",
            "app/limits.py",
            "app/auth.py",
            "config/torrc",
            "config/Caddyfile",
            "scripts/server_supervisor.py",
            "scripts/agy-runner-loop",
        ]
        ownership = get_canonical_ownership(REPO_ROOT)
        for asset in required_independent:
            self.assertIn(asset, ownership)

    def test_generate_candidate_manifest_deterministic(self):
        """Candidate manifest generation must be deterministic and sorted by local path."""
        entries1 = generate_manifest(REPO_ROOT)
        entries2 = generate_manifest(REPO_ROOT)

        self.assertEqual(entries1, entries2)
        locals_list = [e["local"] for e in entries1]
        self.assertEqual(locals_list, sorted(locals_list))

        for entry in entries1:
            self.assertIn("local", entry)
            self.assertIn("remote", entry)
            self.assertIn("sha256", entry)
            self.assertIn("bytes", entry)
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertGreater(entry["bytes"], 0)

        json1 = json.dumps(entries1, indent=2) + "\n"
        json2 = json.dumps(entries2, indent=2) + "\n"
        self.assertEqual(json1, json2)

    def test_validate_complete_candidate_passes(self):
        """A complete candidate release bundle passes validation cleanly."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)
        validate_manifest(manifest_entries, bundle_root)

    def test_validate_repo_root_passes(self):
        """Current maintained repository passes candidate manifest generation and validation."""
        entries = generate_manifest(REPO_ROOT)
        validate_manifest(entries, REPO_ROOT)

    def test_unmapped_new_python_module_fails(self):
        """A new Python runtime module in app/ cannot silently disappear from release."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        extra_module = bundle_root / "app/unmapped_feature.py"
        extra_module.write_text("def run(): pass\n", encoding="utf-8")

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_entries, bundle_root)
        self.assertIn("Newly added Python runtime module", str(ctx.exception))
        self.assertIn("app/unmapped_feature.py", str(ctx.exception))

    def test_rejection_of_secret_and_runtime_classes(self):
        """Runtime secrets, databases, user credentials, and history files are refused."""
        forbidden_examples = [
            "users.json",
            "app/users.json",
            "config/.env",
            "scripts/production.env",
            "config/server.key",
            "app/session.key",
            "web/private.pem",
            "app/id_rsa",
            "scripts/filebrowser.db",
            "app/app.sqlite3",
            "scripts/history.log",
            "app/bash_history",
            "config/services_disabled",
            "config/.phone-server-paused",
            "home/.ssh/authorized_keys",
        ]

        for path in forbidden_examples:
            with self.subTest(path=path):
                self.assertTrue(
                    is_forbidden_secret_or_runtime_path(path),
                    f"Path {path} should be detected as forbidden",
                )

    def test_baseline_manifest_integrity_and_separation(self):
        """
        Historical deployment-files.json must preserve its existing 33 baseline hashes,
        row 34 as agy-runner-loop, and row 35 as verified config/torrc.
        """
        baseline_path = REPO_ROOT / "config/deployment-files.json"
        self.assertTrue(baseline_path.exists())

        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline_entries = json.load(f)

        self.assertEqual(len(baseline_entries), 35)

        # Baseline auth.py retains historical hash
        auth_entry = next(e for e in baseline_entries if e["local"] == "app/auth.py")
        self.assertEqual(auth_entry["sha256"], "1684c592f5d468713b45070755ed8f7687b94fde8853439c8b35beaf7f3773be")
        self.assertEqual(auth_entry["bytes"], 5455)

        # Row 34 is agy-runner-loop
        runner_entry = next(e for e in baseline_entries if e["local"] == "scripts/agy-runner-loop")
        self.assertEqual(runner_entry["sha256"], "fe4148a9a6e92680c94748d7637b5cffeddedf95d5bb6e66e59489f4cdbe9d2c")
        self.assertEqual(runner_entry["bytes"], 1193)

        # Row 35 is config/torrc
        torrc_entry = next(e for e in baseline_entries if e["local"] == "config/torrc")
        self.assertEqual(torrc_entry["sha256"], "ccbcef3b051c6db3fcb9b8f796d6d3d0d4184ad8c0347a41f013aa9f45463e36")
        self.assertEqual(torrc_entry["bytes"], 235)
        self.assertEqual(torrc_entry["remote"], "/data/data/com.termux/files/home/.config/tor/torrc")

    def test_built_gateway_handling(self):
        """Declared Go built artifact is verified when present and fails when required but missing."""
        bundle_root = self._create_minimal_bundle()

        with self.assertRaises(ManifestError) as ctx:
            generate_manifest(bundle_root, require_gateway=True)
        self.assertIn("Required Go built artifact not found", str(ctx.exception))

        gw_file = bundle_root / DEFAULT_GATEWAY_LOCAL
        gw_file.parent.mkdir(parents=True, exist_ok=True)
        gw_file.write_bytes(b"\x7fELFfakegatewaybinary")

        manifest_with_gw = generate_manifest(bundle_root, include_gateway=True)
        gw_entry = next((e for e in manifest_with_gw if e["local"] == DEFAULT_GATEWAY_LOCAL), None)
        self.assertIsNotNone(gw_entry)
        self.assertEqual(gw_entry["remote"], DEFAULT_GATEWAY_REMOTE)

        validate_manifest(manifest_with_gw, bundle_root, require_gateway=True)

    def test_cli_generate_and_validate(self):
        """CLI invocation of generate and validate commands via subprocess."""
        manifest_out = self.test_root / "candidate.json"
        script_path = str(REPO_ROOT / "scripts/release-manifest.py")

        res_gen = subprocess.run(
            [sys.executable, script_path, "generate", "--root", str(REPO_ROOT), "--output", str(manifest_out)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_gen.returncode, 0, f"Generate failed: {res_gen.stderr}")
        self.assertTrue(manifest_out.exists())

        res_val = subprocess.run(
            [sys.executable, script_path, "validate", "--manifest", str(manifest_out), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_val.returncode, 0, f"Validate failed: {res_val.stderr}")
        self.assertIn("Release manifest validation PASSED", res_val.stdout)


if __name__ == "__main__":
    unittest.main()
