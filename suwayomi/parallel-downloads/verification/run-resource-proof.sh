#!/usr/bin/env bash
# Host-only paired resource proof runner for Suwayomi downloader candidate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOF_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export MOBILE_SERVER_PROOF_ROOT="${PROOF_ROOT}"
export ProductBuildType="Stable"
export GRADLE_USER_HOME="/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/.gradle-user-home"

UPSTREAM_DIR="/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/suwayomi-upstream"
CANDIDATE_JAR="${PROOF_ROOT}/.work/packaged-candidate.jar"
EXPECTED_SHA="d4ecb614f2766ee9d7f940bdfb78c7459516dbcfbc4f2ff9d8754803b1455a45"

if [ ! -f "${CANDIDATE_JAR}" ]; then
  echo "ERROR: Candidate JAR not found at ${CANDIDATE_JAR}" >&2
  exit 1
fi

ACTUAL_SHA="$(sha256sum "${CANDIDATE_JAR}" | awk '{print $1}')"
if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
  echo "ERROR: Candidate JAR SHA mismatch. Expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}" >&2
  exit 1
fi

echo "=== Running Suwayomi Paired Resource Proof ==="
echo "Proof root: ${PROOF_ROOT}"
echo "Candidate JAR: ${CANDIDATE_JAR} (verified SHA)"
echo "Upstream checkout: ${UPSTREAM_DIR}"

mkdir -p "${PROOF_ROOT}/.superpowers/sdd"

"${UPSTREAM_DIR}/gradlew" \
  -p "${UPSTREAM_DIR}" \
  --no-daemon --max-workers=2 -Dorg.gradle.jvmargs=-Xmx2g \
  -I "${PROOF_ROOT}/suwayomi/parallel-downloads/verification/production-test-heap.init.gradle.kts" \
  -I "${PROOF_ROOT}/suwayomi/parallel-downloads/verification/packaged-runtime.init.gradle.kts" \
  :server:test \
  --tests PackagedClassOriginTest \
  --tests suwayomi.tachidesk.manga.impl.download.PairedResourceProofTest \
  > "${PROOF_ROOT}/.superpowers/sdd/resource-proof-build.log" 2>&1

if [ ! -f "${PROOF_ROOT}/.superpowers/sdd/resource-proof.json" ]; then
  echo "ERROR: Resource proof output not generated. Inspect ${PROOF_ROOT}/.superpowers/sdd/resource-proof-build.log" >&2
  exit 1
fi

echo "=== Resource Proof PASSED ==="
echo "Report: ${PROOF_ROOT}/.superpowers/sdd/resource-proof.md"
echo "Metrics: ${PROOF_ROOT}/.superpowers/sdd/resource-proof.json"
