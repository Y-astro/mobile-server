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

CANONICAL_MAPPINGS = rm.CANONICAL_MAPPINGS
REQUIRED_SOURCES = rm.REQUIRED_SOURCES
FORBIDDEN_SECRET_PATTERNS = rm.FORBIDDEN_SECRET_PATTERNS
DEFAULT_GATEWAY_LOCAL = rm.DEFAULT_GATEWAY_LOCAL
DEFAULT_GATEWAY_REMOTE = rm.DEFAULT_GATEWAY_REMOTE
ManifestError = rm.ManifestError
generate_manifest = rm.generate_manifest
validate_manifest = rm.validate_manifest
validate_path_safety = rm.validate_path_safety
compute_sha256_and_size = rm.compute_sha256_and_size
is_forbidden_secret_or_runtime_path = rm.is_forbidden_secret_or_runtime_path


class TestReleaseManifest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_minimal_bundle(self) -> Path:
        """Create a complete valid fixture bundle containing all required release assets."""
        bundle_root = self.test_root / "bundle"
        bundle_root.mkdir(parents=True, exist_ok=True)

        for rel_path in REQUIRED_SOURCES:
            src_file = REPO_ROOT / rel_path
            dst_file = bundle_root / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        return bundle_root

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

        # JSON serialized form must be byte-for-byte identical
        json1 = json.dumps(entries1, indent=2) + "\n"
        json2 = json.dumps(entries2, indent=2) + "\n"
        self.assertEqual(json1, json2)

    def test_validate_complete_candidate_passes(self):
        """A complete candidate release bundle passes validation cleanly."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)
        # Should not raise
        validate_manifest(manifest_entries, bundle_root)

    def test_validate_repo_root_passes(self):
        """Current maintained repository passes candidate manifest generation and validation."""
        entries = generate_manifest(REPO_ROOT)
        validate_manifest(entries, REPO_ROOT)

    def test_required_page_or_helper_missing_fails(self):
        """Removing required templates, CSS, limits.py, or launchers fails validation."""
        required_targets = [
            "web/admin.html",
            "web/family.html",
            "web/login.html",
            "web/portal.css",
            "app/limits.py",
            "scripts/server_supervisor.py",
            "scripts/agy-runner-loop",
        ]

        for target in required_targets:
            with self.subTest(target=target):
                bundle_root = self._create_minimal_bundle()
                manifest_entries = generate_manifest(bundle_root)

                # Remove from manifest
                filtered_entries = [e for e in manifest_entries if e["local"] != target]
                with self.assertRaises(ManifestError) as ctx:
                    validate_manifest(filtered_entries, bundle_root)
                self.assertIn("Release manifest omits required assets", str(ctx.exception))
                self.assertIn(target, str(ctx.exception))

                # Remove from disk
                target_path = bundle_root / target
                target_path.unlink()
                with self.assertRaises(ManifestError) as ctx:
                    validate_manifest(manifest_entries, bundle_root)
                self.assertIn("Missing release source file", str(ctx.exception))

    def test_file_tampered_or_bytes_changed_fails(self):
        """Byte size or checksum modifications must fail validation immediately."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        # 1. Byte size changed
        target_file = bundle_root / "web/login.html"
        original_content = target_file.read_bytes()
        target_file.write_bytes(original_content + b"\n<!-- extra byte -->")

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_entries, bundle_root)
        self.assertIn("Byte size mismatch", str(ctx.exception))

        # 2. Byte size same, content changed (hash mismatch)
        tampered_content = original_content[:-1] + (b"X" if original_content[-1:] != b"X" else b"Y")
        target_file.write_bytes(tampered_content)

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(manifest_entries, bundle_root)
        self.assertIn("SHA-256 hash mismatch", str(ctx.exception))

    def test_unmapped_new_python_module_fails(self):
        """A new Python runtime module in app/ cannot silently disappear from release."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        # Introduce an unmapped Python module in app/
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
        ]

        for path in forbidden_examples:
            with self.subTest(path=path):
                self.assertTrue(
                    is_forbidden_secret_or_runtime_path(path),
                    f"Path {path} should be detected as forbidden",
                )

                # Direct safety validation must fail
                with self.assertRaises(ManifestError):
                    validate_path_safety(
                        path,
                        "/data/data/com.termux/files/home/test",
                        self.test_root,
                    )

    def test_rejection_of_path_traversal(self):
        """Source traversal or relative escapements must be rejected."""
        traversal_sources = [
            "../outside.py",
            "app/../../etc/passwd",
            "/absolute/path/file.py",
            "app/../..",
        ]

        for bad_source in traversal_sources:
            with self.subTest(bad_source=bad_source):
                with self.assertRaises(ManifestError):
                    validate_path_safety(
                        bad_source,
                        "/data/data/com.termux/files/home/test",
                        self.test_root,
                    )

        # Remote traversal
        with self.assertRaises(ManifestError):
            validate_path_safety(
                "app/auth.py",
                "/data/data/com.termux/files/home/../../etc/shadow",
                self.test_root,
            )

    def test_rejection_of_symlink_escape(self):
        """Symlinks pointing outside release root must be refused."""
        bundle_root = self._create_minimal_bundle()
        outside_file = self.test_root / "outside_secret.txt"
        outside_file.write_text("secret", encoding="utf-8")

        symlink_path = bundle_root / "scripts/escaped_symlink.sh"
        try:
            symlink_path.symlink_to(outside_file)
        except OSError:
            self.skipTest("Symlinks not supported on filesystem")

        with self.assertRaises(ManifestError) as ctx:
            validate_path_safety(
                "scripts/escaped_symlink.sh",
                "/data/data/com.termux/files/home/bin/escaped.sh",
                bundle_root,
            )
        self.assertIn("Refused symlink escape", str(ctx.exception))

    def test_rejection_of_duplicate_destinations(self):
        """Multiple local sources targeting the same remote destination must fail."""
        bundle_root = self._create_minimal_bundle()
        manifest_entries = generate_manifest(bundle_root)

        # Duplicate destination
        dup_entry = copy.deepcopy(manifest_entries[0])
        dup_entry["local"] = "web/duplicate_copy.html"
        # Same remote as manifest_entries[0]["remote"]
        tampered_manifest = manifest_entries + [dup_entry]

        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(tampered_manifest, bundle_root)
        self.assertIn("Duplicate remote destination", str(ctx.exception))

    def test_rejection_of_unsafe_remote_destinations(self):
        """Remote destinations outside permitted Termux prefix must fail."""
        unsafe_remotes = [
            "/etc/cron.d/malicious",
            "/usr/bin/sudo",
            "/system/bin/sh",
            "relative/path",
        ]

        for unsafe in unsafe_remotes:
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ManifestError):
                    validate_path_safety("app/auth.py", unsafe, self.test_root)

    def test_baseline_manifest_integrity_and_separation(self):
        """
        Historical deployment-files.json must preserve its existing 33 baseline hashes,
        with row 34 being the verified scripts/agy-runner-loop mapping.
        """
        baseline_path = REPO_ROOT / "config/deployment-files.json"
        self.assertTrue(baseline_path.exists())

        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline_entries = json.load(f)

        self.assertEqual(len(baseline_entries), 34)

        # Verify historical auth.py hash in baseline remains byte-for-byte unchanged
        auth_entry = next(e for e in baseline_entries if e["local"] == "app/auth.py")
        self.assertEqual(auth_entry["sha256"], "1684c592f5d468713b45070755ed8f7687b94fde8853439c8b35beaf7f3773be")
        self.assertEqual(auth_entry["bytes"], 5455)

        # Verify row 34 is agy-runner-loop matching launcher-dependency-check.json
        runner_entry = next(e for e in baseline_entries if e["local"] == "scripts/agy-runner-loop")
        self.assertEqual(runner_entry["sha256"], "fe4148a9a6e92680c94748d7637b5cffeddedf95d5bb6e66e59489f4cdbe9d2c")
        self.assertEqual(runner_entry["bytes"], 1193)
        self.assertEqual(
            runner_entry["remote"],
            "/data/data/com.termux/files/usr/var/lib/proot-distro/containers/debian/rootfs/usr/local/bin/agy-runner-loop",
        )

        # Maintain source auth.py deliberately differs from baseline
        actual_sha, actual_sz = compute_sha256_and_size(REPO_ROOT / "app/auth.py")
        self.assertNotEqual(actual_sha, auth_entry["sha256"])

    def test_built_gateway_handling(self):
        """Declared Go built artifacts are verified when present and fail when required but missing."""
        bundle_root = self._create_minimal_bundle()

        # Missing gateway when required
        with self.assertRaises(ManifestError) as ctx:
            generate_manifest(bundle_root, require_gateway=True)
        self.assertIn("Required Go built artifact not found", str(ctx.exception))

        # Add gateway binary fixture
        gw_file = bundle_root / DEFAULT_GATEWAY_LOCAL
        gw_file.parent.mkdir(parents=True, exist_ok=True)
        gw_file.write_bytes(b"\x7fELFfakegatewaybinary")

        manifest_with_gw = generate_manifest(bundle_root, include_gateway=True)
        gw_entry = next((e for e in manifest_with_gw if e["local"] == DEFAULT_GATEWAY_LOCAL), None)
        self.assertIsNotNone(gw_entry)
        self.assertEqual(gw_entry["remote"], DEFAULT_GATEWAY_REMOTE)

        # Should validate successfully
        validate_manifest(manifest_with_gw, bundle_root, require_gateway=True)

    def test_cli_generate_and_validate(self):
        """CLI invocation of generate and validate commands via subprocess."""
        manifest_out = self.test_root / "candidate.json"
        script_path = str(REPO_ROOT / "scripts/release-manifest.py")

        # Generate CLI
        res_gen = subprocess.run(
            [sys.executable, script_path, "generate", "--root", str(REPO_ROOT), "--output", str(manifest_out)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_gen.returncode, 0, f"Generate failed: {res_gen.stderr}")
        self.assertTrue(manifest_out.exists())

        # Validate CLI
        res_val = subprocess.run(
            [sys.executable, script_path, "validate", "--manifest", str(manifest_out), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_val.returncode, 0, f"Validate failed: {res_val.stderr}")
        self.assertIn("Release manifest validation PASSED", res_val.stdout)

        # Validate CLI with tampered manifest fails
        with open(manifest_out, "r", encoding="utf-8") as f:
            items = json.load(f)
        items[0]["bytes"] += 1
        with open(manifest_out, "w", encoding="utf-8") as f:
            json.dump(items, f)

        res_tampered = subprocess.run(
            [sys.executable, script_path, "validate", "--manifest", str(manifest_out), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res_tampered.returncode, 0)
        self.assertIn("Byte size mismatch", res_tampered.stderr)


if __name__ == "__main__":
    unittest.main()
