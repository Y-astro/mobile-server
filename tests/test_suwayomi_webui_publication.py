"""Focused unit and acceptance tests for Suwayomi WebUI publication."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.publish_suwayomi_webui import (
    PublicationError,
    WebUIPublisher,
    compute_file_sha256,
    compute_tree_manifest,
    modify_server_conf_flavor_custom,
    preserve_prior_assets,
    validate_provenance_and_zip,
)


class FakeLifecycleDriver:
    def __init__(self, services_dict=None, should_fail_health=False):
        self.services_dict = services_dict or {"suwayomi": {"name": "suwayomi", "port": 4567}}
        self.paused = False
        self.terminated = []
        self.launched = []
        self.healthy_checks = 0
        self.should_fail_health = should_fail_health
        self.procs = {
            "suwayomi": [(1001, "java -jar suwayomi.jar")],
            "caddy": [(1002, "caddy run")],
            "antra": [(1003, "python3 antra.py")],
        }

    def lock(self, name: str, timeout: float = 10):
        class _Lock:
            def __enter__(inner_self): return inner_self
            def __exit__(inner_self, *args): pass
        return _Lock()

    def is_paused(self) -> bool:
        return self.paused

    def services(self):
        return dict(self.services_dict)

    def matching_processes(self, sid: str):
        return list(self.procs.get(sid, []))

    def terminate(self, records):
        self.terminated.extend(records)
        for pid, cmd in records:
            for s, plist in self.procs.items():
                self.procs[s] = [p for p in plist if p[0] != pid]

    def launch(self, sid: str, cfg):
        self.launched.append((sid, cfg))
        self.procs[sid] = [(2001 + len(self.launched), f"launched {sid}")]

    def healthy(self, sid: str, cfg) -> bool:
        self.healthy_checks += 1
        if self.should_fail_health:
            return False
        return True


class TestWebUIPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.home_dir = self.base_dir / "home"
        self.suwayomi_dir = self.home_dir / "suwayomi" / "data"
        self.live_webui_dir = self.suwayomi_dir / "webUI"
        self.live_webui_dir.mkdir(parents=True)
        self.conf_path = self.suwayomi_dir / "server.conf"

        # Baseline server.conf
        self.initial_conf = (
            'server.port = 4567\n'
            'server.webUIFlavor = "WebUI" # default: WEBUI ; options: WEBUI, VUI, CUSTOM\n'
            'server.webUIChannel = BUNDLED\n'
            'server.webUIUpdateCheckInterval = 0.0\n'
            'server.webUIEnabled = true\n'
        )
        self.conf_path.write_text(self.initial_conf, encoding="utf-8")

        # Baseline live webui files
        (self.live_webui_dir / "index.html").write_text("old-index", encoding="utf-8")
        (self.live_webui_dir / "sw.js").write_text("old-sw", encoding="utf-8")
        assets_dir = self.live_webui_dir / "assets"
        assets_dir.mkdir(parents=True)
        (assets_dir / "old-chunk-12345678.js").write_text("old-chunk-content", encoding="utf-8")

        self.driver = FakeLifecycleDriver()
        self.publisher = WebUIPublisher(home_dir=self.home_dir, driver=self.driver)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_zip_and_prov(self, files: dict, corrupt_sha=False, extra_file=None, symlink_file=None):
        zip_path = self.base_dir / "WebUI.zip"
        prov_path = self.base_dir / "WebUI.provenance.json"

        manifest = {}
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
                manifest[name] = hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()
            if extra_file:
                zf.writestr(extra_file, "extra")
            if symlink_file:
                info = zipfile.ZipInfo(symlink_file)
                info.create_system = 3  # Unix
                info.external_attr = 0o120777 << 16  # Symlink
                zf.writestr(info, "target.txt")

        zip_sha = compute_file_sha256(zip_path)
        if corrupt_sha:
            zip_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        prov_data = {
            "source_commit": "40bc5da57820324ed0ae724cbbc020cc4673b594",
            "base_revision": "v1.1.1",
            "archive_sha256": zip_sha,
            "inputs": {},
            "files": manifest,
            "build_directory": "/tmp/build",
        }
        prov_path.write_text(json.dumps(prov_data, indent=2), encoding="utf-8")
        return zip_path, prov_path, zip_sha

    def test_full_valid_publication_with_asset_preservation(self):
        files = {
            "index.html": "<!doctype html><html>new-index</html>",
            "sw.js": "new-sw-code",
            "assets/new-chunk-abcdef12.js": "new-chunk-content",
        }
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files)

        res = self.publisher.publish(zip_path, prov_path, expected_zip_sha256=zip_sha)
        self.assertEqual(res["status"], "success")

        # Verify live files
        self.assertEqual((self.live_webui_dir / "index.html").read_text(), "<!doctype html><html>new-index</html>")
        self.assertEqual((self.live_webui_dir / "sw.js").read_text(), "new-sw-code")
        self.assertEqual((self.live_webui_dir / "assets" / "new-chunk-abcdef12.js").read_text(), "new-chunk-content")
        # Old lazy chunk must be preserved!
        self.assertTrue((self.live_webui_dir / "assets" / "old-chunk-12345678.js").is_file())
        self.assertEqual((self.live_webui_dir / "assets" / "old-chunk-12345678.js").read_text(), "old-chunk-content")

        # Check server.conf modified ONLY at server.webUIFlavor
        conf = self.conf_path.read_text()
        self.assertIn('server.webUIFlavor = "CUSTOM" # default: WEBUI ; options: WEBUI, VUI, CUSTOM', conf)
        self.assertIn('server.port = 4567', conf)
        self.assertIn('server.webUIChannel = BUNDLED', conf)
        self.assertIn('server.webUIUpdateCheckInterval = 0.0', conf)

        # Check driver interactions
        self.assertEqual(len(self.driver.terminated), 1)
        self.assertEqual(self.driver.terminated[0][0], 1001)
        self.assertEqual(len(self.driver.launched), 1)
        self.assertEqual(self.driver.launched[0][0], "suwayomi")
        self.assertEqual(self.driver.healthy_checks, 1)

    def test_rejection_on_digest_mismatch_before_mutation(self):
        files = {"index.html": "index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files, corrupt_sha=True)

        with self.assertRaises(PublicationError):
            self.publisher.publish(zip_path, prov_path)

        # Ensure no mutation happened
        self.assertEqual((self.live_webui_dir / "index.html").read_text(), "old-index")
        self.assertEqual(len(self.driver.terminated), 0)

    def test_rejection_on_traversal_and_symlink(self):
        files = {"index.html": "index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files, symlink_file="assets/link")

        with self.assertRaises(PublicationError):
            self.publisher.publish(zip_path, prov_path)
        self.assertEqual(len(self.driver.terminated), 0)

    def test_rejection_on_unmanifested_extra_file(self):
        files = {"index.html": "index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files, extra_file="unmanifested.txt")

        with self.assertRaises(PublicationError):
            self.publisher.publish(zip_path, prov_path)
        self.assertEqual(len(self.driver.terminated), 0)

    def test_refuse_when_suwayomi_disabled_or_paused(self):
        files = {"index.html": "index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files)

        # Disabled
        disabled_dir = self.home_dir / ".config" / "services_disabled"
        disabled_dir.mkdir(parents=True)
        (disabled_dir / "suwayomi").touch()

        with self.assertRaises(PublicationError) as cm:
            self.publisher.publish(zip_path, prov_path)
        self.assertIn("disabled", str(cm.exception))

        (disabled_dir / "suwayomi").unlink()

        # Paused
        self.driver.paused = True
        with self.assertRaises(PublicationError) as cm:
            self.publisher.publish(zip_path, prov_path)
        self.assertIn("paused", str(cm.exception))

    def test_health_failure_rolls_back_tree_and_config(self):
        files = {
            "index.html": "new-failing-index",
            "assets/chunk.js": "new-chunk",
        }
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files)

        self.driver.should_fail_health = True
        with self.assertRaises(PublicationError) as cm:
            self.publisher.publish(zip_path, prov_path)

        self.assertIn("unhealthy", str(cm.exception))

        # Check live tree was restored to original
        self.assertEqual((self.live_webui_dir / "index.html").read_text(), "old-index")
        self.assertTrue((self.live_webui_dir / "assets" / "old-chunk-12345678.js").is_file())
        self.assertFalse((self.live_webui_dir / "assets" / "chunk.js").exists())

        # Check config was restored
        conf = self.conf_path.read_text()
        self.assertIn('server.webUIFlavor = "WebUI"', conf)
        self.assertNotIn('server.webUIFlavor = "CUSTOM"', conf)

    def test_recovery_from_interrupted_state(self):
        files = {"index.html": "new-index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files)

        # Simulate interrupted state by performing partial swap and leaving journal
        backup_dir = self.publisher.staging_dir / "backup_webui"
        backup_dir.mkdir(parents=True)
        (backup_dir / "index.html").write_text("old-index", encoding="utf-8")

        conf_backup = self.publisher.staging_dir / "server.conf.backup"
        conf_backup.write_text(self.initial_conf, encoding="utf-8")

        # Corrupt live
        (self.live_webui_dir / "index.html").write_text("corrupted-half-way", encoding="utf-8")
        self.conf_path.write_text("server.webUIFlavor = \"CUSTOM\"\n", encoding="utf-8")

        from tools.publish_suwayomi_webui import PublicationJournal
        journal = PublicationJournal(
            action="publish",
            live_webui_dir=str(self.live_webui_dir),
            backup_webui_dir=str(backup_dir),
            candidate_webui_dir=str(self.publisher.staging_dir / "candidate_webui"),
            conf_path=str(self.conf_path),
            conf_backup_path=str(conf_backup),
            prior_tree_hashes={},
            expected_final_hashes={},
            prior_conf_sha="",
            target_conf_sha="",
            exchange_method="renameat2_exchange",
            status="swapping",
        )
        journal.save(self.publisher.journal_path)

        rec = self.publisher.recover()
        self.assertEqual(rec["status"], "recovered")
        self.assertEqual(rec["previous_status"], "swapping")

        # Verify live restored
        self.assertEqual((self.live_webui_dir / "index.html").read_text(), "old-index")
        self.assertIn('server.webUIFlavor = "WebUI"', self.conf_path.read_text())

    def test_preserve_unrelated_process_identities(self):
        files = {"index.html": "index"}
        zip_path, prov_path, zip_sha = self._make_zip_and_prov(files)

        init_caddy = list(self.driver.procs["caddy"])
        init_antra = list(self.driver.procs["antra"])

        res = self.publisher.publish(zip_path, prov_path)
        self.assertEqual(res["status"], "success")

        # Unrelated processes must not be terminated or changed
        self.assertEqual(self.driver.procs["caddy"], init_caddy)
        self.assertEqual(self.driver.procs["antra"], init_antra)


if __name__ == "__main__":
    unittest.main()
