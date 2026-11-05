#!/usr/bin/env python3
"""Package reviewed downloader classes while retaining the pinned phone customizations."""
import argparse
import copy
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import warnings
import zipfile


METADATA = 'META-INF/mobile-server-build.json'
MANIFEST = 'META-INF/MANIFEST.MF'


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def owned(name, owners):
    return any(name == owner + '.class' or
               (name.startswith(owner + '$') and name.endswith('.class')) for owner in owners)


def index(jar):
    entries = {info.filename: info for info in jar.infolist()}
    for name in entries:
        if name.startswith('/') or '..' in Path(name).parts or '\\' in name:
            raise ValueError('Unsafe archive entry')
        upper = name.upper()
        if upper.startswith('META-INF/') and upper.endswith(('.SF', '.RSA', '.DSA', '.EC')):
            raise ValueError('Signed JAR requires a separate signing workflow')
    return entries


def open_pinned_member(jar, info):
    # The fingerprinted installed shadow JAR has shared-offset dependency
    # entries. Read each ZipInfo occurrence, preserving its bytes and order.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Overlapped entries: .*', category=UserWarning)
        return jar.open(info)


def package(base, compiled, output, pins, patch_sha, compiled_sha, epoch):
    if sha256(base) != pins['base_jar_sha256']:
        raise ValueError('Base JAR does not match the pinned installed build')
    if sha256(compiled) != compiled_sha:
        raise ValueError('Compiled JAR does not match the reviewed build')
    if len(patch_sha) != 64 or any(c not in '0123456789abcdef' for c in patch_sha):
        raise ValueError('Expected a reviewed source patch SHA-256')
    owners = pins['class_owners']
    if not owners or len(set(owners)) != len(owners):
        raise ValueError('Invalid class owner list')
    for owner in owners:
        if not owner.startswith('suwayomi/tachidesk/manga/impl/download/') or any(
                marker in owner for marker in ('..', '\\', '$', '.class')):
            raise ValueError('Class owner outside the reviewed downloader boundary')
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists')
    # DOS ZIP timestamps cannot represent dates earlier than 1980.
    stamp = datetime.datetime.fromtimestamp(max(epoch, 315532800), datetime.timezone.utc)
    timestamp = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    with zipfile.ZipFile(base) as old, zipfile.ZipFile(compiled) as new:
        old_index, new_index = index(old), index(new)
        selected = {name: info for name, info in new_index.items() if owned(name, owners)}
        old_counts = Counter(info.filename for info in old.infolist())
        new_counts = Counter(info.filename for info in new.infolist())
        if old_counts[MANIFEST] != 1 or any(
                count != 1 for name, count in old_counts.items() if owned(name, owners)):
            raise ValueError('Ambiguous original manifest or downloader class')
        if any(new_counts[name] != 1 for name in selected):
            raise ValueError('Duplicate compiled downloader class')
        for owner in owners:
            if owner + '.class' not in selected:
                raise ValueError('Missing compiled class owner: ' + owner)
        for name in selected:
            with new.open(name) as stream:
                header = stream.read(8)
            if header[:4] != b'\xca\xfe\xba\xbe' or int.from_bytes(header[6:8], 'big') != 65:
                raise ValueError('Expected Java 21 class: ' + name)
        for name, digest in pins.get('preserved_custom_entries', {}).items():
            if name not in old_index or hashlib.sha256(old.read(name)).hexdigest() != digest:
                raise ValueError('Pinned custom entry mismatch: ' + name)
        if MANIFEST not in old_index or METADATA in old_index:
            raise ValueError('Expected the original pinned package manifest')
        provenance = {
            'format': 1, 'custom_build': True,
            'upstream_commit': pins['upstream_commit'],
            'base_jar_sha256': pins['base_jar_sha256'],
            'compiled_jar_sha256': compiled_sha, 'source_patch_sha256': patch_sha,
            'source_date_epoch': epoch, 'target_java': 21,
            'class_owners': owners,
            'class_sha256': {name: hashlib.sha256(new.read(name)).hexdigest()
                             for name in sorted(selected)},
        }
        manifest = old.read(MANIFEST).decode('utf-8').replace('\r\n', '\n').rstrip('\n')
        # A named section requires separate handling; never accidentally attach
        # custom provenance to a class section rather than the main attributes.
        if '\n\n' in manifest:
            raise ValueError('Manifest contains named sections')
        manifest += '\nX-Mobile-Server-Custom: true\nX-Mobile-Server-Patch-SHA256: ' + patch_sha
        lines = []
        for line in manifest.splitlines():
            while len(line.encode('utf-8')) > 70:
                # The pinned manifest and added attributes are ASCII.
                lines.append(line[:70])
                line = ' ' + line[70:]
            lines.append(line)
        manifest_bytes = ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')
        output.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with output.open('xb') as target:
                created = True
                with warnings.catch_warnings(), zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as out:
                    # Shadow JARs contain dependency duplicates. Retain every
                    # unchanged occurrence in order; never deduplicate classes.
                    warnings.filterwarnings('ignore', message='Duplicate name: .*', category=UserWarning)
                    for info in old.infolist():
                        name = info.filename
                        if owned(name, owners) or name == MANIFEST:
                            continue
                        with open_pinned_member(old, info) as src, out.open(copy.copy(info), 'w') as dst:
                            shutil.copyfileobj(src, dst, length=1024 * 1024)
                    for name in sorted(selected):
                        info = copy.copy(selected[name])
                        info.date_time = timestamp
                        with new.open(selected[name]) as src, out.open(info, 'w') as dst:
                            shutil.copyfileobj(src, dst, length=1024 * 1024)
                    for name, data in ((MANIFEST, manifest_bytes),
                                       (METADATA, (json.dumps(provenance, sort_keys=True, indent=2) + '\n').encode())):
                        info = zipfile.ZipInfo(name, timestamp)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        out.writestr(info, data)
            output.chmod(0o600)
            with zipfile.ZipFile(output) as result:
                index(result)
                unchanged = [info for info in old.infolist()
                             if not owned(info.filename, owners) and info.filename != MANIFEST]
                expected = [info.filename for info in unchanged] + sorted(selected) + [MANIFEST, METADATA]
                actual = result.infolist()
                if [info.filename for info in actual] != expected:
                    raise ValueError('Unexpected output entry sequence')
                preserved = 0
                for original, packaged in zip(unchanged, actual):
                    with open_pinned_member(old, original) as a, result.open(packaged) as b:
                        if hashlib.file_digest(a, 'sha256').digest() != hashlib.file_digest(b, 'sha256').digest():
                            raise ValueError('Unrelated entry changed: ' + original.filename)
                    preserved += 1
                for name, digest in provenance['class_sha256'].items():
                    if hashlib.sha256(result.read(name)).hexdigest() != digest:
                        raise ValueError('Compiled class mismatch: ' + name)
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
    return {'output': str(output), 'sha256': sha256(output), 'bytes': output.stat().st_size,
            'preserved_entries': preserved, 'downloader_classes': len(selected),
            'removed_old_classes': sorted(name for name in old_index if owned(name, owners) and name not in selected),
            'source_patch_sha256': patch_sha, 'base_jar_sha256': pins['base_jar_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base-jar', 'compiled-jar', 'source-patch', 'patch-sha256', 'compiled-sha256', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--pins', type=Path, default=Path(__file__).with_name('package-inputs.json'))
    parser.add_argument('--source-date-epoch', type=int, required=True)
    args = parser.parse_args()
    if sha256(args.source_patch) != args.patch_sha256:
        parser.error('Source patch does not match its reviewed hash')
    try:
        result = package(args.base_jar, args.compiled_jar, args.output,
                         json.loads(args.pins.read_text()), args.patch_sha256,
                         args.compiled_sha256, args.source_date_epoch)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
