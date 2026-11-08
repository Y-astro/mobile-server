#!/usr/bin/env bash
set -euo pipefail

# Rebuild from pinned upstream without resetting or cleaning a developer checkout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UPSTREAM_COMMIT="41443eaba44bcbdf45d666fe27471b50d41fd994"
SOURCE_DIR="${REPO_ROOT}/.work/webui-source"
mkdir -p "${REPO_ROOT}/.work"
if [[ ! -d "${SOURCE_DIR}/.git" ]]; then
    git clone --no-checkout https://github.com/Suwayomi/Suwayomi-WebUI.git "${SOURCE_DIR}"
fi
git -C "${SOURCE_DIR}" cat-file -e "${UPSTREAM_COMMIT}^{commit}"
BUILD_DIR="$(mktemp -d "${REPO_ROOT}/.work/webui-build.XXXXXX")"
git -C "${SOURCE_DIR}" archive "${UPSTREAM_COMMIT}" | tar -x -C "${BUILD_DIR}"
cd "${BUILD_DIR}"
git init -q
git apply "${SCRIPT_DIR}/0001-unread-updates-feed.patch"
git apply "${SCRIPT_DIR}/0002-cover-memory-preload.patch"
git apply "${SCRIPT_DIR}/0003-custom-webui-updates.patch"
cmp "${SCRIPT_DIR}/CoverMemoryStore.ts" src/features/library/services/CoverMemoryStore.ts
cmp "${SCRIPT_DIR}/useLibraryCoverPreload.ts" src/features/library/hooks/useLibraryCoverPreload.ts
pnpm install --frozen-lockfile
pnpm run tsc
pnpm run build
# Vite's output directory is build, not dist. Never derive the revision from a shallow clone.
export BUILD_DIR SCRIPT_DIR REPO_ROOT
python3 - <<'PY'
from pathlib import Path
import hashlib, json, os, zipfile
root = Path(os.environ['REPO_ROOT'])
source = Path(os.environ['SCRIPT_DIR'])
build = Path(os.environ['BUILD_DIR'])/'build'
out = root/'.work/artifacts'
out.mkdir(exist_ok=True)
(build/'revision').write_text('r3518\n')
assert (build/'index.html').is_file() and any((build/'assets').glob('*.js'))
archive = out/'WebUI.zip'
with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
    for p in sorted(build.rglob('*')):
        if p.is_file():
            info = zipfile.ZipInfo(p.relative_to(build).as_posix(), (2026, 9, 29, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, p.read_bytes())
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
record = {'source_commit': '41443eaba44bcbdf45d666fe27471b50d41fd994', 'base_revision': 'r3518',
          'build_directory': str(build), 'archive_sha256': sha(archive),
          'inputs': {p.name: sha(p) for p in sorted(source.iterdir()) if p.is_file()},
          'files': {p.relative_to(build).as_posix(): sha(p) for p in sorted(build.rglob('*')) if p.is_file()}}
(out/'WebUI.provenance.json').write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps({'archive': str(archive), 'sha256': record['archive_sha256'], 'file_count': len(record['files'])}))
PY
