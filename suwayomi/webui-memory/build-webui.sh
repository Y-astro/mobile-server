#!/usr/bin/env bash
set -euo pipefail

# Build script for Suwayomi-WebUI with cover memory preloader and unread feed preservation.
# Pinned to tag v20260929.01 commit 41443eaba44bcbdf45d666fe27471b50d41fd994.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORK_DIR="${REPO_ROOT}/.work/webui-source"
SAFETY_MARKER="/home/astro/server-audit/2026-11-08/cover-performance-redesign/baseline-measurement-running"

if [[ -f "${SAFETY_MARKER}" ]]; then
    echo "BUILD SAFETY: Marker ${SAFETY_MARKER} is present."
    echo "Baseline timing measurement in progress. Heavy build and dependency install are deferred."
    exit 0
fi

echo "==> Preparing source checkout..."
if [[ ! -d "${WORK_DIR}/.git" ]]; then
    mkdir -p "${WORK_DIR}"
    cd "${WORK_DIR}"
    git init
    git remote add origin https://github.com/Suwayomi/Suwayomi-WebUI.git
    git fetch --depth 1 origin 41443eaba44bcbdf45d666fe27471b50d41fd994
    git checkout FETCH_HEAD
else
    cd "${WORK_DIR}"
    git reset --hard 41443eaba44bcbdf45d666fe27471b50d41fd994
    git clean -fd
fi

echo "==> Applying maintained patches..."
git apply "${SCRIPT_DIR}/0001-unread-updates-feed.patch"
git apply "${SCRIPT_DIR}/0002-cover-memory-preload.patch"

echo "==> Installing pinned workspace dependencies..."
pnpm install --frozen-lockfile

echo "==> Building WebUI frontend..."
pnpm build

echo "==> Packaging WebUI.zip..."
cd dist
zip -r "${REPO_ROOT}/suwayomi/webui-memory/WebUI.zip" .

echo "==> Build complete."
