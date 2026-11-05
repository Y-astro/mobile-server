#!/usr/bin/env bash
# Host-only paired resource proof runner for Suwayomi downloader candidate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOF_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export MOBILE_SERVER_PROOF_ROOT="${PROOF_ROOT}"
export ProductBuildType="Stable"
export GRADLE_USER_HOME="/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/.gradle-user-home"

UPSTREAM_DIR="/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/suwayomi-upstream"
CANDIDATE_JAR="${1:-${PROOF_ROOT}/.work/packaged-candidate.jar}"
EXPECTED_SHA="${2:-${CANDIDATE_PIN:-e85adb62d6e65190984e1acc3d35d552402c9915067325744c0a9cd6ed461139}}"
PROOF_TMP="${PROOF_ROOT}/.work/resource-proof-tmp"

if [ ! -f "${CANDIDATE_JAR}" ]; then
  echo "ERROR: Candidate JAR not found at ${CANDIDATE_JAR}" >&2
  exit 1
fi

ACTUAL_SHA="$(sha256sum "${CANDIDATE_JAR}" | awk '{print $1}')"
if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
  echo "ERROR: Candidate JAR SHA mismatch. Expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}" >&2
  exit 1
fi

RUN_NONCE="$(date -u +%Y%m%dT%H%M%SZ)-$$"

echo "=== Running Suwayomi Paired Resource Proof ==="
echo "Proof root: ${PROOF_ROOT}"
echo "Candidate JAR: ${CANDIDATE_JAR} (SHA: ${EXPECTED_SHA})"
echo "Upstream checkout: ${UPSTREAM_DIR}"
echo "Run nonce: ${RUN_NONCE}"
echo "Isolated temp dir: ${PROOF_TMP}"

mkdir -p "${PROOF_ROOT}/.superpowers/sdd"
mkdir -p "${PROOF_TMP}"

# Clear previous artifacts to guarantee no stale test acceptance
rm -f "${PROOF_ROOT}/.superpowers/sdd/resource-proof.json"
rm -f "${PROOF_ROOT}/.superpowers/sdd/resource-proof.md"
rm -f "${UPSTREAM_DIR}/server/build/test-results/test/TEST-suwayomi.tachidesk.manga.impl.download.PairedResourceProofTest.xml"
rm -f "${UPSTREAM_DIR}/server/build/test-results/test/TEST-PackagedClassOriginTest.xml"

(cd "${UPSTREAM_DIR}"
"${UPSTREAM_DIR}/gradlew" \
  -p "${UPSTREAM_DIR}" \
  --no-daemon --max-workers=2 -Dorg.gradle.jvmargs=-Xmx2g \
  -Dmobile.proof.nonce="${RUN_NONCE}" \
  -Dmobile.proof.expected.sha="${EXPECTED_SHA}" \
  -Dmobile.proof.jar="${CANDIDATE_JAR}" \
  -I "${PROOF_ROOT}/suwayomi/parallel-downloads/verification/production-test-heap.init.gradle.kts" \
  -I "${PROOF_ROOT}/suwayomi/parallel-downloads/verification/packaged-runtime.init.gradle.kts" \
  :server:test \
  --rerun \
  --tests PackagedClassOriginTest \
  --tests suwayomi.tachidesk.manga.impl.download.PairedResourceProofTest \
  > "${PROOF_ROOT}/.superpowers/sdd/resource-proof-build.log" 2>&1
)

REPORT_JSON="${PROOF_ROOT}/.superpowers/sdd/resource-proof.json"
if [ ! -f "${REPORT_JSON}" ]; then
  echo "ERROR: Resource proof output not generated. Inspect ${PROOF_ROOT}/.superpowers/sdd/resource-proof-build.log" >&2
  exit 1
fi

# Verify run nonce in generated report
if ! grep -q "\"run_nonce\": \"${RUN_NONCE}\"" "${REPORT_JSON}"; then
  echo "ERROR: Report nonce does not match current run (${RUN_NONCE})" >&2
  exit 1
fi

# Verify candidate SHA in generated report
if ! grep -q "\"sha256\": \"${EXPECTED_SHA}\"" "${REPORT_JSON}"; then
  echo "ERROR: Report candidate SHA does not match expected (${EXPECTED_SHA})" >&2
  exit 1
fi

# Verify test XML results show 0 failures and 0 errors
TEST_XML="${UPSTREAM_DIR}/server/build/test-results/test/TEST-suwayomi.tachidesk.manga.impl.download.PairedResourceProofTest.xml"
if [ ! -f "${TEST_XML}" ]; then
  echo "ERROR: Test XML report missing at ${TEST_XML}" >&2
  exit 1
fi
if grep -q 'failures="[1-9]' "${TEST_XML}" || grep -q 'errors="[1-9]' "${TEST_XML}"; then
  echo "ERROR: Test XML report indicates test failure" >&2
  exit 1
fi

echo "=== Resource Proof PASSED ==="
echo "Report: ${PROOF_ROOT}/.superpowers/sdd/resource-proof.md"
echo "Metrics: ${REPORT_JSON}"
