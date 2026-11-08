#!/usr/bin/env python3
"""Replace reviewed GraphQL class owners; preserve the current custom JAR."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import warnings
import zipfile

_spec = importlib.util.spec_from_file_location(
    'downloader_package', Path(__file__).resolve().parents[1] / 'parallel-downloads/package_jar.py')
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
OWNERS = (
    'suwayomi/tachidesk/graphql/types/MangaType',
    'suwayomi/tachidesk/graphql/dataLoaders/ChapterFlagCountForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/MangaChapterStats',
    'suwayomi/tachidesk/graphql/dataLoaders/LastReadChapterForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/LatestReadChapterForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/LatestFetchedChapterForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/LatestUploadedChapterForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/FirstUnreadChapterForMangaDataLoader',
    'suwayomi/tachidesk/graphql/dataLoaders/HighestNumberedChapterForMangaDataLoader',
)
METADATA = 'META-INF/mobile-server-library-query.json'


def package(base, classes, output, pins, patch):
    base, classes, output = Path(base), Path(classes), Path(output)
    if _helpers.sha256(base) != pins['base_jar_sha256']:
        raise ValueError('Base JAR does not match the current installed build')
    if set(pins['class_owners']) != set(OWNERS) or len(pins['class_owners']) != len(OWNERS):
        raise ValueError('Unexpected class owner boundary')
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists')
    selected = {}
    for path in classes.rglob('*.class'):
        name = path.relative_to(classes).as_posix()
        if not _helpers.owned(name, OWNERS):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(classes.resolve()):
            raise ValueError('Compiled class escapes its directory')
        body = path.read_bytes()
        if body[:4] != b'\xca\xfe\xba\xbe' or len(body) < 8 or int.from_bytes(body[6:8], 'big') != 65:
            raise ValueError('Expected Java 21 class: ' + name)
        selected[name] = body
    if any(owner + '.class' not in selected for owner in OWNERS):
        raise ValueError('Missing reviewed class owner')
    with zipfile.ZipFile(base) as old:
        _helpers.index(old)
        if METADATA in old.namelist():
            raise ValueError('Library query patch already present')
        names = old.namelist()
        if any(names.count(name) != 1 for name in names if _helpers.owned(name, OWNERS)):
            raise ValueError('Ambiguous original GraphQL class')
        stamp = old.getinfo(OWNERS[0] + '.class').date_time
        provenance = {
            'format': 1, 'base_jar_sha256': pins['base_jar_sha256'],
            'upstream_commit': pins['upstream_commit'], 'source_patch_sha256': _helpers.sha256(patch),
            'class_sha256': {name: hashlib.sha256(body).hexdigest() for name, body in sorted(selected.items())},
        }
        created = False
        try:
            with output.open('xb') as target:
                created = True
                with warnings.catch_warnings(), zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as out:
                    warnings.filterwarnings('ignore', message='Duplicate name: .*', category=UserWarning)
                    for info in old.infolist():
                        if _helpers.owned(info.filename, OWNERS):
                            continue
                        with _helpers.open_pinned_member(old, info) as source:
                            out.writestr(copy.copy(info), source.read())
                    for name, body in sorted(selected.items()):
                        info = zipfile.ZipInfo(name, stamp)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        out.writestr(info, body)
                    info = zipfile.ZipInfo(METADATA, stamp)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    out.writestr(info, json.dumps(provenance, sort_keys=True).encode())
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
    return provenance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--classes', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    result = package(args.base, args.classes, args.output,
                     json.loads((root / 'source-pin.json').read_text()), root / 'library-count.patch')
    print(json.dumps({'output_sha256': _helpers.sha256(args.output), 'replaced_classes': len(result['class_sha256'])}))
