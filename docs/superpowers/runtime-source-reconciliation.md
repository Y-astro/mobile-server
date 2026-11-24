# Runtime Source Reconciliation Report

## Overview

This document records the verification and reconciliation of live phone runtime source into the maintained `ui/expressive-workbench` branch. The reconciliation ensures that releasing the Expressive Workbench UI cannot accidentally overwrite or revert live server runtime enhancements on the target Redmi phone.

This is preservation work for excluded Suwayomi runtime behavior, not a feature deployment or architectural modification.

## Live Device Connection and Invariants

- **Target Device:** Redmi (`u0_a505@100.75.159.53:8022`)
- **Protocol:** Read-only SSH (`BatchMode=yes`, `ConnectTimeout=5`)
- **Live Mutation:** Zero mutations. No live files written, no services restarted, no configurations or flags changed, no databases/keys/user data accessed.
- **Operating Invariants:** System packages (e.g. `com.xiaomi.finddevice`) and phone guardian processes untouched.

## Discovered and Reconciled Source Files

### 1. `scripts/server_supervisor.py`
- **Phone Path:** `/data/data/com.termux/files/home/bin/server_supervisor.py`
- **Maintained Path:** `scripts/server_supervisor.py`
- **Size:** 26,745 bytes
- **SHA-256:** `2fbd45c62d8a4c9d63a2bb883ebf24a4f918ab4bd3e2579acc541e1029fdcf59`
- **Runtime Changes:** Adds `BOUNDED_SUWAYOMI_SERVICES` companion launch logic for `suwayomi`, `suwayomi_filter`, and `suwayomi_scheduler`, dynamic path resolution for `suwayomi_logs.py`, and `capture_children` process reaping.

### 2. `scripts/suwayomi_daily_update.py`
- **Phone Path:** `/data/data/com.termux/files/home/bin/suwayomi_daily_update.py`
- **Maintained Path:** `scripts/suwayomi_daily_update.py`
- **Size:** 21,884 bytes
- **SHA-256:** `6539dfd0e6688069cc22c7cfd58b9bda007d48411fae0d1706e87ec9f404c58b`
- **Runtime Changes:** Adds `SUWAYOMI_BOUNDED_LOGGING` awareness, `BrokenPipeError` resilience when logging to a companion pipe, and existing server-side read/archive cleanup routines (`cleanup_graphql`, `READ_CLEANUP_PAGE_SIZE = 50`, `PROTECTED_READ_CATEGORY = "currently reading"`).

### 3. `scripts/suwayomi_graphql_filter.py`
- **Phone Path:** `/data/data/com.termux/files/home/bin/suwayomi_graphql_filter.py`
- **Maintained Path:** `scripts/suwayomi_graphql_filter.py`
- **Size:** 18,437 bytes
- **SHA-256:** `eabe35d70cee5303035a0ef1379d27e48b0d153aa44b22171110f333a87adf92`
- **Runtime Changes:** Adds `SUWAYOMI_BOUNDED_LOGGING` stdout routing and structured per-request operation and latency logging (`logger.info('request method=%s route=%s operation=%s status=%s elapsedMs=%d', ...)`).

### 4. `app/suwayomi_logs.py` (Discovered Missing Runtime Helper)
- **Phone Path:** `/data/data/com.termux/files/home/.local/lib/mobile-server/suwayomi_logs.py`
- **Maintained Path:** `app/suwayomi_logs.py`
- **Size:** 16,107 bytes
- **SHA-256:** `d8349fee430e15bb9e2fbfadf6da5917f2e343914c302feda437c20bb5bf8698`
- **Runtime Function:** Shared ring-store bounded logging engine enforcing a 20,000,000-byte aggregate limit (active + 9 archives @ 2,000,000 bytes) across Suwayomi services, sanitizing credentials and auth headers, coordinating cross-process file locks, and supporting detached capture. Contains no credentials or secrets.

## Helper Inventory & Parity Check

Direct comparison between live `~/.local/lib/mobile-server/*.py` and maintained `app/*.py` verified that `suwayomi_logs.py` was the only missing runtime source file. All other 19 runtime helper scripts (`agy_sessions.py`, `antra_client.py`, `antra_http.py`, `antra_store.py`, `antra_worker.py`, `auth.py`, `dashboard.py`, `document_conversion.py`, `laptop.py`, `limits.py`, `navidrome.py`, `notes.py`, `repertoire.py`, `role_templates.py`, `service_catalog.py`, `service_panel.py`, `telemetry.py`, `tor_proxy.py`, `wallpapers.py`) are present in `app/`.

## Manifest Ownership & Release Status

Deterministic manifest generator (`scripts/release-manifest.py generate`) automatically discovers `app/suwayomi_logs.py` via dynamic `app/*.py` ownership and assigns remote target `/data/data/com.termux/files/home/.local/lib/mobile-server/suwayomi_logs.py`.

Because the maintained files match the live phone files byte-for-byte:
- `scripts/server_supervisor.py` -> `ACTION_UNTOUCHED`
- `scripts/suwayomi_daily_update.py` -> `ACTION_UNTOUCHED`
- `scripts/suwayomi_graphql_filter.py` -> `ACTION_UNTOUCHED`
- `app/suwayomi_logs.py` -> `ACTION_UNTOUCHED`

## Validation Results

1. **Byte & Hash Equality:**
   Verified SHA-256 checksums match across live device, private audit capture, and maintained files:
   - `server_supervisor.py`: `2fbd45c62d8a4c9d63a2bb883ebf24a4f918ab4bd3e2579acc541e1029fdcf59`
   - `suwayomi_daily_update.py`: `6539dfd0e6688069cc22c7cfd58b9bda007d48411fae0d1706e87ec9f404c58b`
   - `suwayomi_graphql_filter.py`: `eabe35d70cee5303035a0ef1379d27e48b0d153aa44b22171110f333a87adf92`
   - `suwayomi_logs.py`: `d8349fee430e15bb9e2fbfadf6da5917f2e343914c302feda437c20bb5bf8698`

2. **Syntax Validation:**
   Compiled all reconciled sources with Python 3 `compile(..., 'exec')` and `ast.parse`:
   - All 4 files passed with zero syntax errors.

3. **Focused Test Execution:**
   - `python3 -m unittest discover -s tests -p "test_release_manifest.py"`: 25 tests passed.
   - `python3 -m unittest discover -s tests -p "test_supervisor.py"`: 12 tests passed.
   - `python3 -m unittest discover -s tests -p "test_suwayomi_filter.py"`: 38 tests passed.
   - `python3 -m unittest discover -s tests -p "test_configure_suwayomi_update_interval.py"`: 5 tests passed.
   - Total: 80 tests passed, 0 failures.

## AGY Static Web Chrome Release Exception

An explicit release-policy exception is authorized for AGY UI redesign:
- **Authorized Destination:** `/data/data/com.termux/files/home/.config/ttyd/agy_index.html` (exact owned path only).
- **Classification:** Component `agy` (`is_suwayomi_violation=False`, `is_agy_violation=False`), allowing normal general release updates to AGY web chrome without triggering runtime-asset blocks.
- **Invariants Preserved:**
  - All AGY runtime assets (`agy_session.sh`, `usr/bin/agy-resume`, and runner-loop scripts) remain strictly prohibited with existing error text.
  - Lookalike index paths remain prohibited or unowned.
  - No changes to terminal service mapping, daemon lifecycle, or service flags. Pre-disabled AGY service state remains untouched.
