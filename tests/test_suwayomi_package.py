"""Packaging checks preserve custom phone entries and bound replacement classes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('suwayomi_package', ROOT / 'suwayomi/parallel-downloads/package_jar.py')
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.compiled = self.root / 'base.jar', self.root / 'compiled.jar'
        self.owners = [
            'suwayomi/tachidesk/manga/impl/download/DownloadManager',
            'suwayomi/tachidesk/manga/impl/download/Downloader',
            'suwayomi/tachidesk/manga/impl/download/DownloaderNetworkBudget',
            'suwayomi/tachidesk/manga/impl/download/fileProvider/ChaptersFilesProvider',
        ]
        self.old = {
            packager.MANIFEST: b'Manifest-Version: 1.0\r\nMain-Class: suwayomi.tachidesk.MainKt\r\n\r\n',
            'WebUI.zip': b'custom reading filter', 'jni/aarch64/libzstd-kmp.so': b'ANDROID BIONIC',
            'suwayomi/tachidesk/server/util/WebInterfaceManager.class': b'custom offline validation',
            'suwayomi/tachidesk/i18n/MR.class': b'old generated metadata',
            self.owners[1] + '.class': self.bytecode(65, b'old downloader'),
            self.owners[1] + '$Obsolete.class': self.bytecode(65, b'old coroutine'),
        }
        self.new = {owner + '.class': self.bytecode(65, owner.encode()) for owner in self.owners}
        # Deliberately wrong non-downloader inputs must never enter the output.
        self.new.update({'WebUI.zip': b'UNPATCHED', 'jni/aarch64/libzstd-kmp.so': b'GLIBC'})
        self.write(self.base, self.old)
        self.write(self.compiled, self.new)
        self.pins = {'base_jar_sha256': packager.sha256(self.base),
                     'upstream_commit': '40bc5da57820324ed0ae724cbbc020cc4673b594',
                     'class_owners': self.owners}

    @staticmethod
    def bytecode(major, content):
        # Only the packaging header gate is exercised; JVM execution is separate.
        return b'\xca\xfe\xba\xbe\x00\x00' + major.to_bytes(2, 'big') + content

    @staticmethod
    def write(path, entries):
        with zipfile.ZipFile(path, 'w') as jar:
            for name, data in entries.items():
                jar.writestr(name, data)

    def run_package(self, name='output.jar', **kwargs):
        return packager.package(self.base, self.compiled, self.root / name,
                                kwargs.get('pins', self.pins), 'a' * 64,
                                kwargs.get('compiled_sha', packager.sha256(self.compiled)), 1793860000)

    def test_changes_only_reviewed_classes_and_provenance(self):
        result = self.run_package()
        with zipfile.ZipFile(result['output']) as jar:
            for name, data in self.old.items():
                if name != packager.MANIFEST and not packager.owned(name, self.owners):
                    self.assertEqual(jar.read(name), data, name)
            for owner in self.owners:
                self.assertEqual(jar.read(owner + '.class'), self.new[owner + '.class'])
            self.assertNotIn(self.owners[1] + '$Obsolete.class', jar.namelist())
            metadata = json.loads(jar.read(packager.METADATA))
            self.assertTrue(metadata['custom_build'])
            self.assertEqual(metadata['source_patch_sha256'], 'a' * 64)
            self.assertIn(b'X-Mobile-Server-Custom: true\r\n', jar.read(packager.MANIFEST))
        self.assertEqual(result['downloader_classes'], 4)
        self.assertEqual(result['preserved_entries'], 4)

    def test_deterministic_given_pinned_inputs_and_epoch(self):
        self.assertEqual(self.run_package('one.jar')['sha256'], self.run_package('two.jar')['sha256'])

    def test_refuses_wrong_base_or_compiled_hash(self):
        for kwargs in ({'pins': dict(self.pins, base_jar_sha256='0' * 64)}, {'compiled_sha': '0' * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_package(**kwargs)
        self.assertFalse((self.root / 'output.jar').exists())

    def test_refuses_wrong_target_or_missing_owner(self):
        for new in (dict(self.new, **{self.owners[0] + '.class': self.bytecode(66, b'new')}),
                    {k: v for k, v in self.new.items() if k != self.owners[0] + '.class'}):
            self.write(self.compiled, new)
            with self.subTest(entries=list(new)), self.assertRaises(ValueError):
                self.run_package()
            self.assertFalse((self.root / 'output.jar').exists())

    def test_refuses_signature_and_unrelated_owner(self):
        self.write(self.base, dict(self.old, **{'META-INF/OLD.SF': b'signature'}))
        pins = dict(self.pins, base_jar_sha256=packager.sha256(self.base))
        with self.assertRaises(ValueError):
            self.run_package(pins=pins)
        self.write(self.base, self.old)
        with self.assertRaises(ValueError):
            self.run_package(pins=dict(self.pins, class_owners=['java/lang/System']))

    def test_retains_duplicate_dependency_class_occurrences_in_order(self):
        name = 'org/example/Dependency.class'
        with warnings.catch_warnings(), zipfile.ZipFile(self.base, 'a') as jar:
            warnings.filterwarnings('ignore', message='Duplicate name: .*', category=UserWarning)
            jar.writestr(name, b'first dependency version')
            jar.writestr(name, b'second dependency version')
        self.pins['base_jar_sha256'] = packager.sha256(self.base)
        result = self.run_package()
        with zipfile.ZipFile(result['output']) as jar:
            versions = [jar.read(info) for info in jar.infolist() if info.filename == name]
            self.assertEqual(versions, [b'first dependency version', b'second dependency version'])

    def test_rejects_ambiguous_compiled_downloader_class(self):
        with warnings.catch_warnings(), zipfile.ZipFile(self.compiled, 'a') as jar:
            warnings.filterwarnings('ignore', message='Duplicate name: .*', category=UserWarning)
            jar.writestr(self.owners[0] + '.class', self.bytecode(65, b'different implementation'))
        with self.assertRaises(ValueError):
            self.run_package()
        self.assertFalse((self.root / 'output.jar').exists())

    def test_does_not_overwrite_output(self):
        target = self.root / 'output.jar'
        target.write_bytes(b'keep me')
        with self.assertRaises(ValueError):
            self.run_package()
        self.assertEqual(target.read_bytes(), b'keep me')


if __name__ == '__main__':
    unittest.main()
