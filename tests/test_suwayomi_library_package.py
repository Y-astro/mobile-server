"""GraphQL overlay packaging must preserve existing Android/downloader customizations."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('library_package', ROOT / 'suwayomi/library-query/package_jar.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CLASS = b'\xca\xfe\xba\xbe\0\0\0Afixture'


class LibraryPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base, self.output = self.root / 'base.jar', self.root / 'output.jar'
        self.classes = self.root / 'classes'
        self.patch = self.root / 'source.patch'
        self.patch.write_text('reviewed source patch')
        with warnings.catch_warnings(), zipfile.ZipFile(self.base, 'w') as jar:
            warnings.simplefilter('ignore', UserWarning)
            jar.writestr('META-INF/MANIFEST.MF', 'Main-Class: OriginalMain\r\n')
            jar.writestr('META-INF/mobile-server-build.json', 'existing provenance')
            jar.writestr('duplicate.resource', 'first occurrence')
            jar.writestr('duplicate.resource', 'second occurrence')
            jar.writestr('android/shim.class', 'original Android shim')
            jar.writestr('custom/Downloader.class', 'original parallel downloader')
            for owner in module.OWNERS:
                jar.writestr(owner + '.class', CLASS + b'old')
                path = self.classes / (owner + '.class')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(CLASS + b'new')
        self.pins = {'base_jar_sha256': hashlib.sha256(self.base.read_bytes()).hexdigest(),
                     'upstream_commit': 'fixture', 'class_owners': list(module.OWNERS)}

    def package(self):
        return module.package(self.base, self.classes, self.output, self.pins, self.patch)

    def test_preserves_untouched_occurrences_and_previous_metadata(self):
        result = self.package()
        with zipfile.ZipFile(self.base) as old, zipfile.ZipFile(self.output) as new:
            def untouched(jar):
                return [(i.filename, jar.read(i)) for i in jar.infolist()
                        if not module._helpers.owned(i.filename, module.OWNERS) and i.filename != module.METADATA]
            self.assertEqual(untouched(old), untouched(new))
            for owner in module.OWNERS:
                self.assertEqual(new.read(owner + '.class'), CLASS + b'new')
            self.assertEqual(len(result['class_sha256']), len(module.OWNERS))

    def test_rejects_drift_missing_owner_and_wrong_bytecode(self):
        self.pins['base_jar_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Base JAR'): self.package()
        self.assertFalse(self.output.exists())
        self.pins['base_jar_sha256'] = hashlib.sha256(self.base.read_bytes()).hexdigest()
        path = self.classes / (module.OWNERS[0] + '.class')
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'): self.package()
        path.write_bytes(b'not Java')
        with self.assertRaisesRegex(ValueError, 'Java 21'): self.package()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'already exists'): self.package()
        self.assertEqual(self.output.read_bytes(), b'existing')

    def test_copy_failure_removes_only_new_output(self):
        original = self.base.read_bytes()
        with patch.object(module._helpers, 'open_pinned_member', side_effect=OSError('injected read failure')):
            with self.assertRaisesRegex(OSError, 'injected'): self.package()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.base.read_bytes(), original)

    def test_class_symlink_cannot_escape(self):
        path = self.classes / (module.OWNERS[0] + '.class')
        path.unlink()
        outside = self.root / 'outside.class'
        outside.write_bytes(CLASS)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes'): self.package()


if __name__ == '__main__': unittest.main()
