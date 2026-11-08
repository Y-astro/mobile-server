#!/usr/bin/env bash
set -euo pipefail

# Non-destructive build script for Suwayomi-WebUI with cover memory preloader and unread feed preservation.
# Upstream commit: 41443eaba44bcbdf45d666fe27471b50d41fd994 (tag v20260929.01)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/.work/webui-build"
UPSTREAM_COMMIT="41443eaba44bcbdf45d666fe27471b50d41fd994"

echo "==> Preparing clean isolated build directory at ${BUILD_DIR}..."
if [[ ! -d "${BUILD_DIR}/.git" ]]; then
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"
    git init
    git remote add origin https://github.com/Suwayomi/Suwayomi-WebUI.git
    git fetch --depth 1 origin "${UPSTREAM_COMMIT}"
    git checkout FETCH_HEAD
fi

cd "${BUILD_DIR}"
CURRENT_HEAD="$(git rev-parse HEAD)"
if [[ "${CURRENT_HEAD}" != "${UPSTREAM_COMMIT}" ]]; then
    echo "ERROR: Build dir HEAD ${CURRENT_HEAD} does not match expected ${UPSTREAM_COMMIT}"
    exit 1
fi

echo "==> Applying maintained patches..."
git apply "${SCRIPT_DIR}/0001-unread-updates-feed.patch"
git apply "${SCRIPT_DIR}/0002-cover-memory-preload.patch"

echo "==> Verifying module identity with canonical sources..."
cmp -s "${SCRIPT_DIR}/CoverMemoryStore.ts" "${BUILD_DIR}/src/features/library/services/CoverMemoryStore.ts" || {
    echo "ERROR: CoverMemoryStore.ts differs from canonical source!"
    exit 1
}
cmp -s "${SCRIPT_DIR}/useLibraryCoverPreload.ts" "${BUILD_DIR}/src/features/library/hooks/useLibraryCoverPreload.ts" || {
    echo "ERROR: useLibraryCoverPreload.ts differs from canonical source!"
    exit 1
}

echo "==> Installing pinned workspace dependencies with pnpm 11.1.2..."
pnpm install --frozen-lockfile

echo "==> Type-checking with tsgo (pnpm run tsc)..."
pnpm run tsc

echo "==> Building frontend bundle (vite outDir: build)..."
pnpm run build

echo "==> Packaging WebUI.zip into ignored .work/artifacts/..."
mkdir -p "${REPO_ROOT}/.work/artifacts"
python3 -c "
import os, zipfile
build_dir = '${BUILD_DIR}/build'
zip_path = '${REPO_ROOT}/.work/artifacts/WebUI.zip'
with open(os.path.join(build_dir, 'revision'), 'w') as f:
    f.write('r3518\n')
with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(build_dir):
        for f in files:
            full_path = os.path.join(root, f)
            arcname = os.path.relpath(full_path, build_dir)
            zf.write(full_path, arcname)
"

echo "==> WebUI.zip built successfully at .work/artifacts/WebUI.zip"
sha256sum "${REPO_ROOT}/.work/artifacts/WebUI.zip"
