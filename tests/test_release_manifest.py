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
DEFAULT_SUWAYOMI_JAR_LOCAL = rm.DEFAULT_SUWAYOMI_JAR_LOCAL
DEFAULT_SUWAYOMI_JAR_REMOTE = rm.DEFAULT_SUWAYOMI_JAR_REMOTE
AUDITED_BASELINE_SHA256 = rm.AUDITED_BASELINE_SHA256
FORBIDDEN_SECRET_PATTERNS = rm.FORBIDDEN_SECRET_PATTERNS
ManifestError = rm.ManifestError
generate_manifest = rm.generate_manifest
validate_manifest = rm.validate_manifest
validate_path_safety = rm.validate_path_safety
REVIEWED_ADDITIONAL_ASSETS = rm.REVIEWED_ADDITIONAL_ASSETS
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
        for rel_path in list(baseline_mapping.keys()) + list(REVIEWED_ADDITIONAL_ASSETS.keys()):
            src_file = REPO_ROOT / rel_path
            if src_file.exists():
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

    def test_baseline_checksum_pin_rejects_damaged_bundle_omitting_admin_html(self):
        """
        Damaged bundle omitting admin.html from baseline fails generation and validation
        because baseline checksum pin detects alteration.
        """
        bundle_root = self._create_minimal_bundle()
        cfg_file = bundle_root / "config/deployment-files.json"
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        data = [row for row in data if row.get("local") != "web/admin.html"]
        cfg_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Must fail closed on load/generate/validate with message about audit recapture
        with self.assertRaises(ManifestError) as ctx:
            load_baseline_mapping(bundle_root)
        self.assertIn("Audited baseline mapping checksum mismatch", str(ctx.exception))
        self.assertIn("Reviewed audit recapture is required", str(ctx.exception))

        with self.assertRaises(ManifestError) as ctx:
            generate_manifest(bundle_root)
        self.assertIn("Audited baseline mapping checksum mismatch", str(ctx.exception))

    def test_baseline_checksum_pin_rejects_removed_launcher_or_destination_tamper(self):
        """
        Baseline checksum pin rejects when arbitrary launcher row is removed or
        baseline auth destination is tampered before installer writes.
        """
        bundle_root = self._create_minimal_bundle()
        cfg_file = bundle_root / "config/deployment-files.json"
        data = json.loads(cfg_file.read_text(encoding="utf-8"))

        # Case 1: Tamper destination of auth.py
        data_tampered = copy.deepcopy(data)
        for row in data_tampered:
            if row.get("local") == "app/auth.py":
                row["remote"] = "/data/data/com.termux/files/home/.local/lib/mobile-server/benign_auth.py"
        cfg_file.write_text(json.dumps(data_tampered, indent=2), encoding="utf-8")
        with self.assertRaises(ManifestError) as ctx:
            load_baseline_mapping(bundle_root)
        self.assertIn("Audited baseline mapping checksum mismatch", str(ctx.exception))

        # Case 2: Remove launcher row (scripts/agy-runner-loop)
        data_removed = [row for row in data if row.get("local") != "scripts/agy-runner-loop"]
        cfg_file.write_text(json.dumps(data_removed, indent=2), encoding="utf-8")
        with self.assertRaises(ManifestError) as ctx:
            load_baseline_mapping(bundle_root)
        self.assertIn("Audited baseline mapping checksum mismatch", str(ctx.exception))

    def test_baseline_separation_candidate_differs_from_historical_hash(self):
        """
        Candidate source code can differ from historical baseline hash:
        generate fresh hash for modified app/auth.py, and candidate validation passes.
        Proves baseline metadata pin is decoupled from candidate freshness computation.
        """
        bundle_root = self._create_minimal_bundle()
        auth_file = bundle_root / "app/auth.py"
        auth_file.write_text(auth_file.read_text(encoding="utf-8") + "\n# Deliberate update\n", encoding="utf-8")

        # Candidate manifest generation computes fresh hash for modified auth.py
        candidate_entries = generate_manifest(bundle_root)
        candidate_auth = next(e for e in candidate_entries if e["local"] == "app/auth.py")
        self.assertNotEqual(candidate_auth["sha256"], "1684c592f5d468713b45070755ed8f7687b94fde8853439c8b35beaf7f3773be")

        # Candidate validation passes with fresh hash
        validate_manifest(candidate_entries, bundle_root)

    def test_baseline_symlink_escape_rejected_before_read(self):
        """Symlink escaping release root on config/deployment-files.json is rejected."""
        bundle_root = self._create_minimal_bundle()
        outside_cfg = self.test_root / "outside_cfg.json"
        outside_cfg.write_text(json.dumps([{"local": "app/auth.py", "remote": "/data/data/com.termux/files/home/test"}]), encoding="utf-8")

        cfg_file = bundle_root / "config/deployment-files.json"
        cfg_file.unlink()
        try:
            cfg_file.symlink_to(outside_cfg)
        except OSError:
            self.skipTest("Symlinks not supported")

        with self.assertRaises(ManifestError) as ctx:
            load_baseline_mapping(bundle_root)
        self.assertIn("Refused baseline symlink escape", str(ctx.exception))
        self.assertIn("Reviewed audit recapture is required", str(ctx.exception))

    def test_relative_symlink_in_root_with_keyword_in_root_name(self):
        """
        A release root directory with 'token' in its absolute path (e.g. /tmp/token-check-xyz/)
        must not cause false positive secret rejection on normal inside-root source files.
        """
        token_root = self.test_root / "token-check-test123"
        token_root.mkdir(parents=True, exist_ok=True)
        cfg_dir = token_root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "config/deployment-files.json", cfg_dir / "deployment-files.json")

        baseline_mapping = load_baseline_mapping(token_root)
        for rel_path in baseline_mapping.keys():
            src_file = REPO_ROOT / rel_path
            dst_file = token_root / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        # Normal sources inside token_root must pass validate_path_safety
        validate_path_safety(
            "app/auth.py",
            baseline_mapping["app/auth.py"],
            token_root,
        )

        # Also an internal symlink pointing to normal file inside token_root must pass
        symlink_file = token_root / "app/sym_auth.py"
        try:
            symlink_file.symlink_to(token_root / "app/auth.py")
            validate_path_safety(
                "app/sym_auth.py",
                "/data/data/com.termux/files/home/.local/lib/mobile-server/sym_auth.py",
                token_root,
            )
        except OSError:
            pass

        # But a symlink pointing to an actual forbidden file relative to root must still fail
        secret_file = token_root / "app/credentials.py"
        secret_file.write_text("SECRET=1\n", encoding="utf-8")
        symlink_secret = token_root / "app/sym_secret.py"
        try:
            symlink_secret.symlink_to(secret_file)
            with self.assertRaises(ManifestError) as ctx:
                validate_path_safety(
                    "app/sym_secret.py",
                    "/data/data/com.termux/files/home/.local/lib/mobile-server/sym_secret.py",
                    token_root,
                )
            self.assertIn("Refused symlink pointing to forbidden secret", str(ctx.exception))
        except OSError:
            pass

    def test_sha256_type_and_format_validation(self):
        """Candidate manifest with non-string, malformed, or bool fields raises clean ManifestError."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        # Non-string sha256 (e.g. integer or dict)
        bad_entries1 = copy.deepcopy(manifest_entries)
        bad_entries1[0]["sha256"] = 123456
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad_entries1, bundle_root)
        self.assertIn("must be a string", str(ctx.exception))

        # Malformed sha256 (not 64 hex characters)
        bad_entries2 = copy.deepcopy(manifest_entries)
        bad_entries2[0]["sha256"] = "invalid_hash"
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad_entries2, bundle_root)
        self.assertIn("must be a 64-character hex string", str(ctx.exception))

        # Non-hex characters
        bad_entries3 = copy.deepcopy(manifest_entries)
        bad_entries3[0]["sha256"] = "z" * 64
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad_entries3, bundle_root)
        self.assertIn("must be a 64-character hex string", str(ctx.exception))

        # Bool byte size (bool is subclass of int in python)
        bad_entries4 = copy.deepcopy(manifest_entries)
        bad_entries4[0]["bytes"] = True
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad_entries4, bundle_root)
        self.assertIn("Invalid byte size", str(ctx.exception))


    def test_reviewed_additional_assets_required_in_manifest(self):
        """Candidate manifest omitting mandatory reviewed additional assets must fail validation."""
        bundle_root = self._create_minimal_bundle()
        # Create web/portal-polling.js in bundle root so it exists on disk
        portal_path = bundle_root / 'web/portal-polling.js'
        portal_path.parent.mkdir(parents=True, exist_ok=True)
        portal_path.write_text('/* portal polling */')

        manifest_entries = generate_manifest(bundle_root)
        # Should validate cleanly when included
        validate_manifest(manifest_entries, bundle_root)

        # Remove web/portal-polling.js from manifest entries
        filtered = [e for e in manifest_entries if e['local'] != 'web/portal-polling.js']
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(filtered, bundle_root)
        self.assertIn('Release manifest omits required maintained assets: web/portal-polling.js', str(ctx.exception))

    def test_suwayomi_jar_optional_built_artifact(self):
        """Opt-in 44 includes exact Suwayomi JAR; missing when required fails; symlink rejected."""
        import scripts.release_transaction as rt
        bundle_root = self._create_minimal_bundle()

        # Add gateway
        gw_file = bundle_root / DEFAULT_GATEWAY_LOCAL
        gw_file.parent.mkdir(parents=True, exist_ok=True)
        gw_file.write_bytes(b"\x7fELFfakegatewaybinary")

        # Without opt-in, JAR is omitted
        manifest_no_jar = generate_manifest(bundle_root, include_gateway=True)
        self.assertIsNone(next((e for e in manifest_no_jar if e["local"] == DEFAULT_SUWAYOMI_JAR_LOCAL), None))
        validate_manifest(manifest_no_jar, bundle_root, require_gateway=True)

        # Full repository tree test: 42 maintained files + gateway = 43; + JAR = 44
        full_bundle = self.test_root / "full_bundle"
        shutil.copytree(REPO_ROOT / "config", full_bundle / "config")
        shutil.copytree(REPO_ROOT / "web", full_bundle / "web")
        shutil.copytree(REPO_ROOT / "scripts", full_bundle / "scripts")
        shutil.copytree(REPO_ROOT / "app", full_bundle / "app")
        # Ensure optional built gateway exists
        (full_bundle / ".work").mkdir(parents=True, exist_ok=True)
        (full_bundle / DEFAULT_GATEWAY_LOCAL).write_bytes(b"\x7fELFgateway")

        manifest_43 = generate_manifest(full_bundle, include_gateway=True)
        self.assertEqual(len(manifest_43), 43)
        validate_manifest(manifest_43, full_bundle, require_gateway=True)

        # Opt-in with JAR produces exact 44 files
        (full_bundle / DEFAULT_SUWAYOMI_JAR_LOCAL).write_bytes(b"PK\x03\x04fulljar")
        manifest_44 = generate_manifest(full_bundle, include_gateway=True, include_suwayomi_jar=True)
        self.assertEqual(len(manifest_44), 44)
        validate_manifest(manifest_44, full_bundle, require_gateway=True, require_suwayomi_jar=True)

        # Requiring JAR when absent fails
        with self.assertRaises(ManifestError) as ctx:
            generate_manifest(bundle_root, include_gateway=True, require_suwayomi_jar=True)
        self.assertIn("Required Suwayomi JAR built artifact not found", str(ctx.exception))

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_no_jar, bundle_root, require_gateway=True, require_suwayomi_jar=True)
        self.assertIn("Required Suwayomi JAR built artifact", str(ctx.exception))

        # Add valid benign JAR file to minimal bundle
        jar_file = bundle_root / DEFAULT_SUWAYOMI_JAR_LOCAL
        jar_file.write_bytes(b"PK\x03\x04fakejarcontent")

        manifest_min_44 = generate_manifest(bundle_root, include_gateway=True, include_suwayomi_jar=True)
        jar_entry = next((e for e in manifest_min_44 if e["local"] == DEFAULT_SUWAYOMI_JAR_LOCAL), None)
        self.assertIsNotNone(jar_entry)
        self.assertEqual(jar_entry["remote"], DEFAULT_SUWAYOMI_JAR_REMOTE)
        validate_manifest(manifest_min_44, bundle_root, require_gateway=True, require_suwayomi_jar=True)

        # Mode policy check: canonical target mode for Suwayomi JAR is fixed 0644
        jar_target_mode = rt.get_canonical_target_mode(DEFAULT_SUWAYOMI_JAR_REMOTE)
        self.assertEqual(jar_target_mode, 0o644)

        # Symlink JAR source rejected
        jar_file.unlink()
        target_outside = bundle_root.parent / "escape_jar.jar"
        target_outside.write_bytes(b"PK\x03\x04escaped")
        jar_file.symlink_to(target_outside)
        with self.assertRaises(ManifestError) as ctx:
            generate_manifest(bundle_root, include_gateway=True, include_suwayomi_jar=True)
        self.assertIn("Refused symlink escape", str(ctx.exception))

    def test_suwayomi_jar_cli_generate_and_validate(self):
        """CLI flags --include-suwayomi-jar and --require-suwayomi-jar."""
        bundle_root = self._create_minimal_bundle()
        jar_file = bundle_root / DEFAULT_SUWAYOMI_JAR_LOCAL
        jar_file.parent.mkdir(parents=True, exist_ok=True)
        jar_file.write_bytes(b"PK\x03\x04cli_jar_test")

        manifest_out = self.test_root / "candidate_jar.json"
        script_path = str(REPO_ROOT / "scripts/release-manifest.py")

        res_gen = subprocess.run(
            [
                sys.executable,
                script_path,
                "generate",
                "--root",
                str(bundle_root),
                "--include-suwayomi-jar",
                "--output",
                str(manifest_out),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_gen.returncode, 0, f"Generate failed: {res_gen.stderr}")
        self.assertTrue(manifest_out.exists())

        res_val = subprocess.run(
            [
                sys.executable,
                script_path,
                "validate",
                "--manifest",
                str(manifest_out),
                "--root",
                str(bundle_root),
                "--require-suwayomi-jar",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_val.returncode, 0, f"Validate failed: {res_val.stderr}")
        self.assertIn("Release manifest validation PASSED", res_val.stdout)

if __name__ == "__main__":
    unittest.main()
