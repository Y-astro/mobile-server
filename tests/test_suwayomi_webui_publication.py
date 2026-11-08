"""Exercise complete publication and recovery with real files and a fake lifecycle."""
import contextlib
import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import publish_suwayomi_webui as publication


class FakeLifecycleDriver:
    def __init__(self):
        self.procs = {'suwayomi': [[1001, 'backend']], 'caddy': [[1002, 'caddy']], 'antra': [[1003, 'worker']]}
        self.launched = 0
        self.stopped = 0
        self.paused = False
        self.fail_candidate = False
        self.locked = False

    @contextlib.contextmanager
    def lock(self, name, timeout=10):
        assert name == 'services' and not self.locked
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def is_paused(self):
        return self.paused

    def services(self):
        return {name: {} for name in self.procs}

    def matching_processes(self, sid):
        return list(self.procs[sid])

    def terminate(self, records):
        assert self.locked and records == self.procs['suwayomi']
        self.stopped += 1
        self.procs['suwayomi'] = []

    def launch(self, sid, cfg):
        assert self.locked and sid == 'suwayomi' and not self.procs[sid]
        self.launched += 1
        self.procs[sid] = [[2000+self.launched, 'backend']]

    def healthy(self, sid, cfg):
        return bool(self.procs[sid]) and not (self.fail_candidate and self.launched == 1)


class TestWebUIPublication(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root/'home'
        data = self.home/'suwayomi/data'
        self.live = data/'webUI'
        (self.live/'assets').mkdir(parents=True)
        (self.live/'index.html').write_text('old index')
        (self.live/'revision').write_text('r3518')
        (self.live/'assets/old-chunk.js').write_text('old asset')
        self.conf = data/'server.conf'
        self.original_conf = b'# untouched\r\nserver.webUIFlavor = "WebUI" # comment\r\nserver.password = "private"\r\n'
        self.conf.write_bytes(self.original_conf)
        self.conf.chmod(0o600)
        (self.home/'suwayomi/bin').mkdir()
        (self.home/'suwayomi/bin/Suwayomi-Server.jar').write_bytes(b'proven backend')
        (self.home/'suwayomi/libdns_fix.so').write_bytes(b'intentional shim')
        flags = self.home/'.config/services_disabled'
        flags.mkdir(parents=True)
        (flags/'tor').touch()
        self.driver = FakeLifecycleDriver()
        self.publisher = publication.WebUIPublisher(self.home, self.driver)
        self.old_tree = publication.compute_tree_manifest(self.live)
        self.original_protected = self.publisher._protected()
        self.archive = self.root/'WebUI.zip'
        self.provenance = self.root/'WebUI.provenance.json'
        self._artifact({'index.html': b'new index', 'revision': b'r3518', 'assets/new-chunk.js': b'new asset'})

    def tearDown(self):
        self.temporary.cleanup()

    def _artifact(self, files, modes=None):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, content in files.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (modes or {}).get(name, 0o100644) << 16
                archive.writestr(info, content)
        self.provenance.write_text(json.dumps({'archive_sha256': publication.compute_file_sha256(self.archive),
            'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}))

    def publish(self, **overrides):
        kwargs = {'expected_zip_sha256': publication.compute_file_sha256(self.archive),
            'expected_provenance_sha256': publication.compute_file_sha256(self.provenance),
            'expected_prior_tree_sha': self.old_tree,
            'expected_prior_conf_sha': hashlib.sha256(self.original_conf).hexdigest()}
        kwargs.update(overrides)
        return self.publisher.publish(self.archive, self.provenance, **kwargs)

    def assert_original(self):
        self.assertEqual(publication.compute_tree_manifest(self.live), self.old_tree)
        self.assertEqual(self.conf.read_bytes(), self.original_conf)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        self.assertEqual(self.publisher._protected(), self.original_protected)

    def test_full_publication_preserves_assets_config_permissions_and_runtime(self):
        before = dict(self.driver.procs)
        result = self.publish()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.conf.read_bytes(), self.original_conf.replace(b'"WebUI"', b'"CUSTOM"'))
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        self.assertEqual((self.live/'assets/old-chunk.js').read_text(), 'old asset')
        self.assertEqual((self.live/'index.html').read_text(), 'new index')
        self.assertEqual(self.driver.procs['caddy'], before['caddy'])
        self.assertEqual(self.driver.procs['antra'], before['antra'])
        self.assertEqual(self.publisher._protected(), self.original_protected)
        self.assertEqual(stat.S_IMODE(self.publisher.conf_backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.publisher.staging_dir.stat().st_mode), 0o700)
        self.assertEqual(self.driver.launched, 1)

    def test_archive_and_provenance_digests_are_both_required(self):
        for field in ('expected_zip_sha256', 'expected_provenance_sha256'):
            with self.assertRaises(publication.PublicationError):
                self.publish(**{field: '0'*64})
        self.assert_original()
        self.assertEqual(self.driver.stopped, 0)

    def test_traversal_nonregular_and_extra_entries_rejected_before_stop(self):
        for name, mode in [('../escape.js', 0o100644), ('assets/link', 0o120777), ('assets/fifo', 0o010644), ('assets/../escape', 0o100644)]:
            self._artifact({'index.html': b'x', 'revision': b'r3518', name: b'x'}, {name: mode})
            with self.assertRaises(publication.PublicationError):
                self.publish()
        self._artifact({'index.html': b'x', 'revision': b'r3518'})
        with zipfile.ZipFile(self.archive, 'a') as archive:
            archive.writestr('extra', b'extra')
        with self.assertRaises(publication.PublicationError):
            self.publish()
        self.assert_original()
        self.assertEqual(self.driver.stopped, 0)

    def test_archive_content_mismatch_rejected_before_stop(self):
        prov = json.loads(self.provenance.read_bytes())
        prov['files']['index.html'] = '0'*64
        self.provenance.write_text(json.dumps(prov))
        with self.assertRaises(publication.PublicationError):
            self.publish()
        self.assert_original()
        self.assertEqual(self.driver.stopped, 0)

    def test_live_config_and_nested_symlink_drift_refuse_before_stop(self):
        with self.assertRaises(publication.PublicationError):
            self.publish(expected_prior_conf_sha='0'*64)
        (self.live/'assets/link').symlink_to(self.conf)
        with self.assertRaises(publication.PublicationError):
            self.publish()
        (self.live/'assets/link').unlink()
        self.assert_original()
        self.assertEqual(self.driver.stopped, 0)

    def test_paused_disabled_and_unavailable_exchange_refuse_before_stop(self):
        self.driver.paused = True
        with self.assertRaises(publication.PublicationError):
            self.publish()
        self.driver.paused = False
        flag = self.home/'.config/services_disabled/suwayomi'
        flag.symlink_to('nonexistent')
        with self.assertRaises(publication.PublicationError):
            self.publish()
        flag.unlink()
        with patch.object(publication, 'test_rename_exchange_support', return_value=False):
            with self.assertRaises(publication.PublicationError):
                self.publish()
        self.assert_original()
        self.assertEqual(self.driver.stopped, 0)

    def test_health_failure_rolls_back_and_verifies_old_runtime(self):
        self.driver.fail_candidate = True
        with patch.object(publication, 'HEALTH_TIMEOUT', 0):
            with self.assertRaisesRegex(publication.PublicationError, 'unhealthy'):
                self.publish()
        self.assert_original()
        self.assertEqual(self.driver.launched, 2)
        self.assertEqual(json.loads(self.publisher.journal_path.read_bytes())['status'], 'rolled_back')

    def test_interruption_immediately_after_exchange_recovers_idempotently(self):
        real_exchange = publication._rename_exchange
        calls = 0
        def interrupted(src, dst):
            nonlocal calls
            real_exchange(src, dst)
            calls += 1
            if calls == 2:  # first exchange is the native capability check
                raise KeyboardInterrupt()
        with patch.object(publication, '_rename_exchange', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.publish()
        self.assertEqual((self.live/'index.html').read_text(), 'new index')
        self.assertEqual(self.publisher.recover()['status'], 'recovered')
        self.assert_original()
        launches = self.driver.launched
        self.assertEqual(self.publisher.recover()['status'], 'already_consistent')
        self.assertEqual(self.driver.launched, launches)

    def test_recovery_refuses_config_or_tree_drift_without_service_signals(self):
        self.publish()
        stops = self.driver.stopped
        self.conf.write_bytes(self.conf.read_bytes()+b'# concurrent edit\n')
        with self.assertRaisesRegex(publication.PublicationError, 'Configuration drift'):
            self.publisher.recover()
        self.assertEqual(self.driver.stopped, stops)
        self.conf.write_bytes(self.original_conf.replace(b'"WebUI"', b'"CUSTOM"'))
        (self.live/'index.html').write_text('concurrent edit')
        with self.assertRaisesRegex(publication.PublicationError, 'WebUI drift'):
            self.publisher.recover()
        self.assertEqual(self.driver.stopped, stops)

    def test_explicit_rollback_after_success_preserves_original_bytes(self):
        self.publish()
        self.assertEqual(self.publisher.recover()['status'], 'recovered')
        self.assert_original()


if __name__ == '__main__':
    unittest.main()
