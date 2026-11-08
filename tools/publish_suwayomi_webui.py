#!/usr/bin/env python3
"""Publish one verified custom WebUI, with atomic exchange and drift-safe rollback.

Only the WebUI tree and the server.webUIFlavor value may change. Native atomic
exchange is required; backend JAR, DNS shim, service intent and other processes
are protected. Configuration backups remain private. Recovery derives current
state from hashes, including interruption immediately after an exchange.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

RENAME_EXCHANGE = 2
AT_FDCWD = -100
HEALTH_TIMEOUT = 45
MAX_ZIP_ENTRIES = 2000
MAX_UNCOMPRESSED_TOTAL_BYTES = 50 * 1024 * 1024
MAX_ENTRY_SIZE = 20 * 1024 * 1024

class PublicationError(RuntimeError):
    pass

def _get_libc():
    try:
        return ctypes.CDLL(None)
    except Exception:
        return None


def test_rename_exchange_support(test_dir: Path) -> bool:
    """Feature-test renameat2 RENAME_EXCHANGE support before mutation."""
    d1 = Path(tempfile.mkdtemp(prefix="exchange-a-", dir=test_dir))
    d2 = Path(tempfile.mkdtemp(prefix="exchange-b-", dir=test_dir))
    try:
        d1.mkdir(exist_ok=True)
        d2.mkdir(exist_ok=True)
        (d1 / "a.txt").write_text("a")
        (d2 / "b.txt").write_text("b")
        _rename_exchange(d1, d2)
        if (d1 / "b.txt").exists() and (d2 / "a.txt").exists():
            return True
        return False
    except OSError:
        return False
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


def _rename_exchange(src: Path, dst: Path) -> None:
    """Atomically exchange two paths using renameat2(RENAME_EXCHANGE)."""
    libc = _get_libc()
    if libc is None:
        raise OSError(errno.ENOSYS, "C library not available for renameat2")

    src_bytes = os.fsencode(str(src))
    dst_bytes = os.fsencode(str(dst))
    dir_fd = AT_FDCWD

    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        ret = renameat2(dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_EXCHANGE)
        if ret == 0:
            return
        err = ctypes.get_errno()
        raise OSError(err, f"renameat2 failed: {os.strerror(err)}")

    syscall = getattr(libc, "syscall", None)
    if syscall is None:
        raise OSError(errno.ENOSYS, "renameat2 and syscall not found in libc")

    machine = platform.machine().lower()
    sys_renameat2 = None
    if "x86_64" in machine or "amd64" in machine:
        sys_renameat2 = 316
    elif "aarch64" in machine or "arm64" in machine:
        sys_renameat2 = 276
    elif "arm" in machine:
        sys_renameat2 = 382
    elif "i386" in machine or "i686" in machine:
        sys_renameat2 = 353
    elif "riscv64" in machine:
        sys_renameat2 = 276

    if sys_renameat2 is None:
        raise OSError(errno.ENOSYS, f"SYS_renameat2 syscall unknown for architecture {machine}")

    syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    syscall.restype = ctypes.c_long
    ret = syscall(sys_renameat2, dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_EXCHANGE)
    if ret == 0:
        return
    err = ctypes.get_errno()
    raise OSError(err, f"renameat2 syscall failed: {os.strerror(err)}")


def compute_file_sha256(path: Path) -> str:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise PublicationError('Expected a regular file')
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def safe_path(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise PublicationError('Expected an absolute non-traversing path')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PublicationError('Symlink in publication path')
    if path.exists() and path.stat().st_uid != os.getuid():
        raise PublicationError('Unexpected file owner')


def compute_tree_manifest(directory: Path) -> dict[str, str]:
    safe_path(directory)
    if not directory.is_dir():
        raise PublicationError('Missing WebUI directory')
    result = {}
    for root, dirs, files in os.walk(directory):
        for name in dirs + files:
            p = Path(root)/name
            safe_path(p)
            if name in files:
                if not stat.S_ISREG(p.lstat().st_mode):
                    raise PublicationError('Non-regular WebUI entry')
                result[p.relative_to(directory).as_posix()] = compute_file_sha256(p)
    return result


def validate_manifest(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise PublicationError('Missing full file manifest')
    for name, digest in value.items():
        if (not isinstance(name, str) or not name or '\\' in name or
            PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix() != name or
            any(part in ('', '.', '..') for part in name.split('/')) or
            not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise PublicationError('Invalid manifest path or digest')
    return value


def validate_provenance_and_zip(zip_path: Path, prov_path: Path,
                                expected_zip_sha256: str, expected_provenance_sha256: str) -> dict:
    for path, expected in ((zip_path, expected_zip_sha256), (prov_path, expected_provenance_sha256)):
        safe_path(path)
        if not path.is_file() or not expected or compute_file_sha256(path) != expected:
            raise PublicationError('Archive or provenance digest mismatch')
    prov = json.loads(prov_path.read_bytes())
    if prov.get('archive_sha256') != expected_zip_sha256:
        raise PublicationError('Provenance does not bind archive')
    manifest = validate_manifest(prov.get('files'))
    if 'index.html' not in manifest or 'revision' not in manifest:
        raise PublicationError('Missing WebUI entry points')
    with zipfile.ZipFile(zip_path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(entries) > MAX_ZIP_ENTRIES or len(set(names)) != len(names) or
            set(names) != set(manifest) or
            sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_TOTAL_BYTES):
            raise PublicationError('Archive entry set or total size invalid')
        for entry in entries:
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.is_dir() or mode not in (0, stat.S_IFREG) or
                entry.file_size > MAX_ENTRY_SIZE or
                hashlib.sha256(archive.read(entry)).hexdigest() != manifest[entry.filename]):
                raise PublicationError('Invalid archive entry or file digest')
    return prov


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix='.webui-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def modify_server_conf_flavor_custom(content: str) -> tuple[str, bool]:
    pattern = re.compile(r'(^[ \t]*server\.webUIFlavor[ \t]*=[ \t]*)("[^"]*"|[^\s#]+)', re.MULTILINE)
    matches = list(pattern.finditer(content))
    if len(matches) != 1 or matches[0][2].strip('"') not in ('WebUI', 'WEBUI', 'CUSTOM'):
        raise PublicationError('Expected exactly one supported WebUI flavor setting')
    match = matches[0]
    changed = content[:match.start(2)]+'"CUSTOM"'+content[match.end(2):]
    return changed, changed != content


def preserve_prior_assets(live: Path, candidate: Path, manifest: dict[str, str]) -> dict[str, str]:
    result = dict(manifest)
    for name, digest in compute_tree_manifest(live).items():
        if name not in result and (name.startswith(('assets/', 'static/')) or re.fullmatch(r'workbox-[\w-]+\.js', name)):
            target = candidate/name
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, (live/name).read_bytes(), 0o644)
            result[name] = digest
    return result


class WebUIPublisher:
    def __init__(self, home_dir: Path, driver=None):
        self.home_dir = Path(os.path.abspath(home_dir))
        safe_path(self.home_dir)
        data = self.home_dir/'suwayomi/data'
        self.live_webui_dir = data/'webUI'
        self.conf_path = data/'server.conf'
        self.staging_dir = data/'.webui_staging'
        self.candidate = self.staging_dir/'candidate_webui'
        self.conf_backup = self.staging_dir/'server.conf.backup'
        self.journal_path = self.staging_dir/'publication_journal.json'
        self.driver = driver

    def _ensure_driver(self):
        if self.driver is None:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from scripts.termux_activation import TermuxLifecycleDriver
            self.driver = TermuxLifecycleDriver()
        return self.driver

    def _intent(self):
        driver = self._ensure_driver()
        disabled = self.home_dir/'.config/services_disabled/suwayomi'
        if driver.is_paused() or disabled.exists() or disabled.is_symlink():
            raise PublicationError('Suwayomi disabled or server paused')
        services = driver.services()
        if 'suwayomi' not in services:
            raise PublicationError('Missing Suwayomi service')
        return services

    def _protected(self):
        files = ['suwayomi/bin/Suwayomi-Server.jar', 'suwayomi/libdns_fix.so']
        result = {'files': {}, 'flags': {}}
        for name in files:
            path = self.home_dir/name
            safe_path(path)
            result['files'][name] = compute_file_sha256(path)
        flags = self.home_dir/'.config/services_disabled'
        safe_path(flags)
        for path in flags.iterdir():
            info = path.lstat()
            result['flags'][path.name] = [info.st_ino, info.st_mtime_ns, info.st_mode,
                os.readlink(path) if path.is_symlink() else compute_file_sha256(path)]
        return result

    def _save(self, journal):
        atomic_write(self.journal_path, (json.dumps(journal, sort_keys=True)+'\n').encode(), 0o600)

    def _stop(self):
        driver = self._ensure_driver()
        records = driver.matching_processes('suwayomi')
        if records:
            driver.terminate(records)
        if driver.matching_processes('suwayomi'):
            raise PublicationError('Suwayomi survived verified shutdown')

    def _start(self, services):
        driver = self._ensure_driver()
        driver.launch('suwayomi', services['suwayomi'])
        deadline = time.monotonic()+HEALTH_TIMEOUT
        while not driver.healthy('suwayomi', services['suwayomi']):
            if time.monotonic() >= deadline:
                raise PublicationError('Suwayomi unhealthy after restart')
            time.sleep(0.25)

    def _check_journal(self, journal):
        if journal.get('version') != 1 or journal.get('status') not in (
            'prepared', 'applying', 'committed', 'rolling_back', 'rolled_back', 'recovery_required'):
            raise PublicationError('Invalid publication journal')
        safe_path(self.staging_dir)
        if stat.S_IMODE(self.staging_dir.stat().st_mode) != 0o700 or stat.S_IMODE(self.conf_backup.stat().st_mode) != 0o600:
            raise PublicationError('Private staging permissions changed')
        if stat.S_IMODE(self.conf_path.stat().st_mode) != journal.get('conf_mode'):
            raise PublicationError('Configuration permissions drift')
        for name in ('prior_tree', 'target_tree'):
            validate_manifest(journal.get(name))
        if journal['prior_tree'] == journal['target_tree']:
            raise PublicationError('Ambiguous tree states')
        for path in (self.conf_path, self.conf_backup, self.candidate, self.journal_path):
            safe_path(path)
        if compute_file_sha256(self.conf_backup) != journal['prior_conf_sha']:
            raise PublicationError('Configuration backup drift')
        if self._protected() != journal['protected']:
            raise PublicationError('Protected runtime or disabled flags drifted')
        current_conf = compute_file_sha256(self.conf_path)
        if current_conf not in (journal['prior_conf_sha'], journal['target_conf_sha']):
            raise PublicationError('Configuration drift; recovery refused')
        current = compute_tree_manifest(self.live_webui_dir)
        other = compute_tree_manifest(self.candidate)
        if not ((current == journal['prior_tree'] and other == journal['target_tree']) or
                (current == journal['target_tree'] and other == journal['prior_tree'])):
            raise PublicationError('WebUI drift; recovery refused')
        return current

    def _restore(self, journal, services):
        current = self._check_journal(journal)
        journal['status'] = 'rolling_back'
        self._save(journal)
        driver = self._ensure_driver()
        changed = current != journal['prior_tree'] or compute_file_sha256(self.conf_path) != journal['prior_conf_sha']
        if changed:
            self._stop()
            if current != journal['prior_tree']:
                _rename_exchange(self.live_webui_dir, self.candidate)
                sync_directory(self.live_webui_dir.parent)
                sync_directory(self.staging_dir)
            # Restore old UI before its old validation mode, including after interruption.
            atomic_write(self.conf_path, self.conf_backup.read_bytes(), journal['conf_mode'])
        if changed or not driver.healthy('suwayomi', services['suwayomi']):
            self._start(services)
        self._check_journal(journal)
        if compute_tree_manifest(self.live_webui_dir) != journal['prior_tree'] or compute_file_sha256(self.conf_path) != journal['prior_conf_sha']:
            raise PublicationError('Rollback verification failed')
        journal['status'] = 'rolled_back'
        self._save(journal)

    def publish(self, zip_path: Path, prov_path: Path, *, expected_zip_sha256: str,
                expected_provenance_sha256: str, expected_prior_tree_sha: dict[str, str],
                expected_prior_conf_sha: str) -> dict:
        prov = validate_provenance_and_zip(zip_path, prov_path, expected_zip_sha256, expected_provenance_sha256)
        validate_manifest(expected_prior_tree_sha)
        driver = self._ensure_driver()
        with driver.lock('services', timeout=10):
            services = self._intent()
            for path in (self.live_webui_dir, self.conf_path, self.staging_dir):
                safe_path(path)
            if self.staging_dir.exists():
                raise PublicationError('Publication staging already exists; inspect or recover it')
            prior = compute_tree_manifest(self.live_webui_dir)
            if prior != expected_prior_tree_sha or compute_file_sha256(self.conf_path) != expected_prior_conf_sha:
                raise PublicationError('Prior WebUI or configuration drift')
            before = {sid: driver.matching_processes(sid) for sid in services if sid != 'suwayomi'}
            protected = self._protected()
            content = self.conf_path.read_bytes()
            conf_mode = stat.S_IMODE(self.conf_path.stat().st_mode)
            target = modify_server_conf_flavor_custom(content.decode('utf-8'))[0].encode('utf-8')
            self.staging_dir.mkdir(mode=0o700)
            self.candidate.mkdir()
            if self.staging_dir.stat().st_dev != self.live_webui_dir.stat().st_dev or not test_rename_exchange_support(self.staging_dir):
                raise PublicationError('Atomic directory exchange unavailable')
            with zipfile.ZipFile(zip_path) as archive:
                for name in prov['files']:
                    path = self.candidate/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, archive.read(name), 0o644)
            if compute_tree_manifest(self.candidate) != prov['files']:
                raise PublicationError('Extracted manifest mismatch')
            final = preserve_prior_assets(self.live_webui_dir, self.candidate, prov['files'])
            if compute_tree_manifest(self.candidate) != final or prior == final:
                raise PublicationError('Candidate manifest invalid or unchanged')
            for directory, _, _ in os.walk(self.candidate, topdown=False):
                sync_directory(Path(directory))
            atomic_write(self.conf_backup, content, 0o600)
            journal = {'version': 1, 'status': 'prepared', 'prior_tree': prior, 'target_tree': final,
                'prior_conf_sha': expected_prior_conf_sha, 'target_conf_sha': hashlib.sha256(target).hexdigest(),
                'conf_mode': conf_mode, 'protected': protected, 'archive_sha256': expected_zip_sha256,
                'provenance_sha256': expected_provenance_sha256}
            self._save(journal)
            self._check_journal(journal)
            try:
                journal['status'] = 'applying'
                self._save(journal)
                self._stop()
                self._check_journal(journal)
                # CUSTOM first: an interrupted publication can still serve the complete old UI.
                atomic_write(self.conf_path, target, conf_mode)
                _rename_exchange(self.candidate, self.live_webui_dir)
                sync_directory(self.live_webui_dir.parent)
                sync_directory(self.staging_dir)
                self._start(services)
                if self._check_journal(journal) != final or compute_file_sha256(self.conf_path) != journal['target_conf_sha']:
                    raise PublicationError('Published output verification failed')
                if any(driver.matching_processes(sid) != records for sid, records in before.items()):
                    raise PublicationError('Unrelated service identity changed')
                journal['status'] = 'committed'
                self._save(journal)
            except Exception:
                try:
                    self._restore(journal, services)
                except Exception as error:
                    journal['status'] = 'recovery_required'
                    self._save(journal)
                    raise PublicationError('Publication failed; explicit recovery required') from error
                raise
            return {'status': 'success', 'manifest_count': len(final), 'conf_sha256': journal['target_conf_sha'],
                'exchange_method': 'renameat2_exchange', 'journal': str(self.journal_path)}

    def recover(self) -> dict:
        if not self.journal_path.exists():
            return {'status': 'no_journal_found'}
        driver = self._ensure_driver()
        with driver.lock('services', timeout=10):
            services = self._intent()
            safe_path(self.journal_path)
            journal = json.loads(self.journal_path.read_bytes())
            current = self._check_journal(journal)
            if journal['status'] == 'rolled_back':
                if current != journal['prior_tree'] or compute_file_sha256(self.conf_path) != journal['prior_conf_sha']:
                    raise PublicationError('Rolled-back state drift')
                return {'status': 'already_consistent', 'journal_status': 'rolled_back'}
            self._restore(journal, services)
            return {'status': 'recovered'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home-dir', type=Path, default=Path('/data/data/com.termux/files/home'))
    parser.add_argument('--recover', action='store_true')
    for option in ('zip', 'provenance', 'preconditions'):
        parser.add_argument('--'+option, type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--expected-provenance-sha256')
    args = parser.parse_args()
    publisher = WebUIPublisher(args.home_dir)
    if args.recover:
        result = publisher.recover()
    else:
        if not all((args.zip, args.provenance, args.preconditions, args.expected_sha256, args.expected_provenance_sha256)):
            parser.error('Publication requires archive, provenance, both digests and reviewed preconditions')
        pre = json.loads(args.preconditions.read_bytes())
        result = publisher.publish(args.zip, args.provenance, expected_zip_sha256=args.expected_sha256,
            expected_provenance_sha256=args.expected_provenance_sha256,
            expected_prior_tree_sha=pre['tree'], expected_prior_conf_sha=pre['conf_sha256'])
    print(json.dumps(result))


if __name__ == '__main__':
    main()
