# Release File Transactions

This document defines the architecture, API, CLI, and operational boundaries of the standalone standard-library file transaction engine implemented in `scripts/release_transaction.py`.

## 1. Overview and Architecture

The file transaction engine provides deterministic, reversible filesystem deployments under Termux environments (`/data/data/com.termux/files/`). It ensures:
1. **Precondition Validation**: Every candidate destination is verified against reviewed preconditions before any live write.
2. **Namespace Serialization**: A non-blocking `flock` on `.release_transaction.lock` prevents concurrent modification of the live namespace.
3. **Write-Intent Journaling**: Every live file replacement records write intent before atomic replacement so that an interrupted process can recover.
4. **Ordered Installation**: Runtime helper libraries and web/config assets are installed before executable entrypoints.
5. **Automatic Rollback**: Any error during live application triggers immediate restoration of all touched files to their original state.
6. **Bundle-Free Recovery**: A standalone recovery copy of `release_transaction.py` is saved in the transaction directory, allowing complete rollback even after candidate bundles are unmounted or deleted.

```
       [ Candidate Bundle + Manifest ]
                      |
                      v
             [ prepare(...) ]
       - Validates candidate via release-manifest.py
       - Checks live preconditions (bytes/SHA/modes/UIDs/absence)
       - Stages files (0700) and captures backups (0700)
       - Writes journal (STATE_PREPARED)
                      |
                      v
              [ apply(...) ]
       - Acquires namespace flock
       - Re-verifies live preconditions immediately before writes
       - Verifies staged file and backup checksums
       - Applies helpers/assets before entrypoints
       - Writes intent -> temporary sibling -> os.replace -> fsync
       - State: STATE_APPLIED
                      |
                      +-------------------+
                      |                   |
                      v                   v
              [ commit(...) ]     [ Failure / Interruption ]
       - State: STATE_COMMITTED           |
                                          v
                                   [ recover(...) ]
                            - Restores from backups in reverse order
                            - Returns new files to absent
                            - Refuses unknown concurrent edits
                            - State: STATE_ROLLED_BACK
```

## 2. Preconditions and Identity Model

Preconditions must be provided explicitly by the caller and cover every candidate destination:
- **Existing Regular Files**: Must match expected SHA-256, byte size, permission mode, and UID.
- **Absent Paths**: New runtime helpers (`notes.py`, `document_conversion.py`) must be confirmed absent on live disk before creation.
- **Boot Alias Symlink**: The historical symlink `home/start_services.sh` pointing to `home/.termux/boot/start-services.sh` is strictly preserved. Only its target script is staged, replaced, and backed up; the symlink itself is never overwritten or removed.
- **Symlink Escapes**: Any non-alias symlink destination, or any symlink component in parent directories, is rejected fail-closed.
- **Forbidden Patterns**: Secrets, databases (`filebrowser.db`), runtime flags (`services_disabled`), private keys, and session tokens are strictly rejected.

In production, expected file UID is `10505`. In test harnesses and fixture namespaces, expected file UID defaults to the active runtime UID (`os.getuid()`).

## 3. Python API Reference

```python
import scripts.release_transaction as rt

# 1. Prepare transaction
# Acquires NamespaceLock; validates canonical modes and runtime UID before creating tx_dir
tx = rt.prepare(
    bundle_root="/path/to/bundle",
    candidate_manifest=manifest_entries,
    preconditions=preconditions_data,
    transaction_dir="/path/to/tx_dir",
    namespace_root="/data/data/com.termux/files",
)

# 2. Inspect status
status = tx.status()
# Returns: {"state": "PREPARED", "version": 1, ...}

# 3. Apply changes to live filesystem
res = tx.apply(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "APPLIED", "applied_count": N}
# Idempotent: repeated apply verifies candidate live state and returns {"state": "APPLIED", "status": "idempotent_success"}

# 4. Finalize transaction
res_cm = tx.commit(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "COMMITTED", "status": "committed"}
# Idempotent: repeated commit returns {"state": "COMMITTED", "status": "idempotent_success"}

# 5. Recovery (for interrupted, failed, or applying transactions)
res_rc = tx.recover(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "ROLLED_BACK", "status": "recovered"}
# If already COMMITTED: verifies live candidate state and returns {"state": "COMMITTED", "status": "idempotent_success"}
# If already ROLLED_BACK: verifies live preimage state and returns {"state": "ROLLED_BACK", "status": "idempotent_success"}

# 6. Explicit Rollback (caller-initiated reversion of applied or committed releases)
res_rb = tx.rollback(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "ROLLED_BACK", "status": "rolled_back"}
# Idempotent: repeated rollback returns {"state": "ROLLED_BACK", "status": "idempotent_success"}
```

## 4. Mode Policy & Write Safety

- **Canonical Permissions**:
  - Exact audited destination overrides take precedence; see [the mode policy](RELEASE-MODE-POLICY.md) and `AUDITED_TARGET_MODE_OVERRIDES`. They preserve existing Suwayomi/AGY/terminal permissions without mode-only restarts.
  - Defaults are `0700` for ordinary executables/scripts and `0600` for helper modules, configs and web assets. Historical runner fallback paths use `0755`; the audited Debian container runner override is `0700`.
  - Manifests declaring non-canonical target modes fail-closed in `prepare()` before any staging.
  - Staged files follow canonical `target_mode`, backups preserve `orig_mode`, and transaction directories are created with `0700` mode.
- **Strict UID Validation & Prepare Zero-Disk-Write Guarantee**:
  - UID values must be non-negative integers (`type(uid) is int and not isinstance(uid, bool)`).
  - In `prepare()`, runtime UID (`os.getuid()`) is verified against declared `top_uid` and entry UIDs before `transaction_dir` is created on disk. If UID drift is detected, zero transaction files are created.
  - Mutating operations (`apply`, `commit`, `recover`, `rollback`) verify runtime UID under lock.
- **Boot Alias Identity Verification**:
  - Known raw link target text (`os.readlink`), device (`st_dev`), inode (`st_ino`), UID (`st_uid`), and mode (`st_mode`) are verified across `prepare`, `apply`, `commit`, `rollback`, `recover`, and idempotent checks.
  - Retargeting (e.g. `./.termux` vs `.termux`) or inode replacement triggers fail-closed refusal; the symlink is never overwritten, unlinked, or automatically repaired.
- **Write-All & Atomic File Safety**:
  - All file and journal payload writes utilize `write_all(fd, payload)` with bounded `BlockingIOError` retries, `EINTR` retries, and zero-write detection.
  - `write_atomic_file` encapsulates write, mode change, `os.fsync`, size/hash/mode/UID verification, and `os.replace` within a single utility. An outer `finally` block guarantees unreplaced temporary sibling files (`.tmp_*`) are unlinked on any failure or exception.
- **Directory Durability & Non-Empty Guard**:
  - `prepare()` acquires `NamespaceLock` to serialize candidate preparation.
  - If `transaction_dir` already exists and is non-empty, `prepare()` refuses fail-closed to protect historical recovery evidence.
  - `fsync_dir` ensures directory metadata durability and propagates `EIO`, `ENOSPC`, and `EBADF` fail-closed. On filesystems where directory sync is unsupported, `EINVAL` and `ENOTSUP` are safely tolerated.
- **Strict Journal Schema Validation**:
  - `load_journal()` enforces schema version == 1, valid state/action/status enums, non-empty entry list, canonical target modes, 1:1 correspondences across manifest, entries, and preconditions, duplicate remote detection, and directory traversal containment.
- **Locked Namespace Re-Check**:
  - Mutating operations reload the journal under `NamespaceLock` and verify that the reloaded `namespace_root` strictly matches the locked root and caller root.

## 5. CLI Commands

The CLI tool supports only inspection and safe file recovery. It deliberately **does not** expose an `apply` subcommand; live activation requires the reviewed service adapter.

### Inspect Status
```bash
python3 scripts/release_transaction.py status --transaction-dir /path/to/tx_dir
```

### File Recovery / Rollback
```bash
python3 scripts/release_transaction.py recover --transaction-dir /path/to/tx_dir [--namespace-root /data/data/com.termux/files]
```
Even if `scripts/` is missing, the standalone copy in the transaction directory can be invoked directly:
```bash
python3 /path/to/tx_dir/release_transaction.py recover --transaction-dir /path/to/tx_dir
```

## 6. Durability & Interruption Guarantees

- **Journal Durability**: Written to sibling temporary file via `write_all`, verified, fsynced, atomically renamed via `os.replace`, and directory fsynced.
- **Write-Intent Durability**: Every destination transition (`INTENT_WRITE` -> `WRITTEN`) is recorded before the corresponding live file replacement.
- **Unknown Concurrent Edit Refusal**: Recovery inspects all live destinations before modifying disk. If any file matches neither the original preimage nor the candidate target, recovery refuses to proceed, transitioning to `STATE_RECOVERY_REQUIRED` to prevent corruption of external modifications.
- **Power-Loss Boundaries**: Relies on POSIX filesystem `os.fsync` and single-file `os.replace` atomicity. Does not guarantee recovery across physical flash controller failure or kernel-level filesystem corruption during sudden battery removal.

## 7. Service Isolation Boundaries

This transaction engine strictly manages files on disk. It:
- **Does NOT** inspect or manipulate processes or supervisord.
- **Does NOT** touch service flags (`services_disabled`, `.phone-server-paused`).
- **Does NOT** connect to remote phones or SSH daemons.
- **Does NOT** manage database migrations or user credentials.

Production service quiescence and service reactivation are orchestrated by `scripts/release_activation.py` using `scripts/termux_activation.py`.

## 8. Service-Scoped Release Activation

The service activation adapter coordinates the file transaction engine with Termux service supervisor lifecycle authority (`scripts/server_supervisor.py`).

### Lock Hierarchy
To prevent deadlock across independent lock files:
1. `ActivationLock` (`<namespace_root>/.release_activation.lock`): Acquired exclusively for the entire activation command duration.
2. `services.lock` (`<STATE>/services.lock`): Held during flag inspection/mutation, process quiescence, and service start/probe. Released during supervisor daemon termination to avoid deadlock with a daemon blocked on `cycle()`.
3. `NamespaceLock` (`<namespace_root>/.release_transaction.lock`): Held internally and transiently by `ReleaseTransaction` methods (`prepare`, `apply`, `commit`, `rollback`, `recover`).

### Destination Mapping & Invariants
Only destinations with actual content or mode changes (`action != ACTION_UNTOUCHED`) affect running services:
- Dashboard helpers, templates, CSS, JS: Affects `dashboard`.
- Gateway executable (`home/bin/mobile-gateway`): Affects `transfer_gateway`.
- Caddy config (`home/.config/caddy/Caddyfile`): Affects `caddy`.
- Supervisor script (`home/bin/server_supervisor.py`) / shared catalog (`service_catalog.py`): Affects `supervisor` daemon (and `dashboard` if catalog changed).
- TTYD assets / scripts: Maps to their owning terminal service. This release refuses changes to AGY launcher/runner/ttyd runtime assets rather than attempting unsupported descendant migrations.
- Tor config (`home/.config/tor/torrc`): Affects `tor`.
- CLI commands (`server-user`, `server-status`, `server-health`, `server-up`, `server-down`, `server-restart`) and boot aliases: No running daemon affected.
- **Safety Invariants**:
  - `ssh` is strictly prohibited from being selected or restarted.
  - Suwayomi scripts (`start_suwayomi.sh`, `suwayomi_daily_update.py`, `suwayomi_graphql_filter.py`, `suwayomi_update.sh`) must be unchanged in software releases; any candidate modification is rejected fail-closed.
  - Pre-disabled services (flags existing in `services_disabled/`) are preserved exactly (bytes, mode, inode) and never started.
  - Unrelated running services continue running without interruption.

### Flag Preservation & Ownership Tokens
- Existing disable flags are presence markers. Pre-existing flags are never overwritten, truncated, or removed.
- Temporary guards are created only for originally absent flags under `services.lock` prior to process termination.
- The complete original flag snapshot is journaled before creating the first guard. Recovery binds that snapshot to the file engine's affected destinations and current state.
- Each temporary guard receives a unique ownership token `release_guard:{tx_id}:{sid}` written with mode `0600` and fsync.
- Guard removal verifies ownership token, file type (regular file), runtime UID, mode, and recorded inode. Concurrent modifications fail-closed without deleting external markers.

### CLI Commands

#### Dry Planning
```bash
python3 scripts/release_activation.py plan \
  --bundle /path/to/bundle \
  --preconditions /path/to/preconditions.json \
  [--namespace-root /data/data/com.termux/files]
```

#### Service-Scoped Activation
```bash
python3 scripts/release_activation.py apply \
  --bundle /path/to/bundle \
  --preconditions /path/to/preconditions.json \
  --transaction-dir /path/to/tx_dir \
  [--namespace-root /data/data/com.termux/files]
```

#### Standalone Interruption Recovery
Use the preserved command when the candidate bundle is deleted or published candidate modules cannot be imported. All five preserved code assets must match recorded hashes before execution. Unknown edits to live files or recovery code are refused:
```bash
python3 /path/to/tx_dir/recovery_lifecycle/release_activation.py recover \
  --transaction-dir /path/to/tx_dir \
  --namespace-root /data/data/com.termux/files
```

#### Rollback
```bash
python3 scripts/release_activation.py rollback \
  --transaction-dir /path/to/tx_dir \
  [--namespace-root /data/data/com.termux/files]
```

#### Inspect Activation Status
```bash
python3 scripts/release_activation.py status --transaction-dir /path/to/tx_dir
```

### Durability & Rollback Boundaries
- **No Multi-File Atomic Filesystem Swaps**: Files are replaced individually via `os.replace` while affected services are quiesced. Ordinary exceptions attempt coherent backout; a terminated process requires the preserved recovery command. Recovery refuses unknown edits and surviving processes rather than overwriting them.
- **Power Loss**: Relies on POSIX `fsync` and journal state boundaries. Does not guarantee flash controller durability against sudden power loss during raw hardware write cycles.
- **Live Processes During File Recovery**: File recovery must never be invoked on a namespace with running candidate processes. The activation adapter guarantees candidate processes are quiesced before file rollback executes.
- **Fail-Closed on Drift**: External modifications to owned destinations or guard flags fail-closed into `STATE_RECOVERY_REQUIRED`.


### Suwayomi lifecycle migration profile

The explicit `suwayomi-lifecycle` profile permits changes only to the existing
backend launcher, supervisor, and service catalog. It requires the exact ordered
15-service catalog followed by one `suwayomi_scheduler` entry. Every prior
service configuration must remain equal. The default general and filter profiles
retain their existing restrictions.

Activation refuses an existing scheduler during this initial migration. Before
creating guards, it captures one owned native Java backend, including its JVM
arguments, root directory, working directory, and allowlisted environment. The
journal binds that recipe to the prior launcher and catalog preimages. Unsupported
or ambiguous prior runtimes are refused before service changes.

Recovery verifies the staged, backup, and preserved catalogs before loading them,
recomputes the exact transition, and requires the recipe for an active backend.
It stops the candidate scheduler before restoring the prior catalog. Backout
launches the prior Java command directly: invoking the old launcher would spawn
a scheduler that was absent before migration. A damaged owned live catalog can
be restored through verified staged bytes before the file engine restores its
prior preimage. Recovery keeps the transaction and service intent guards when
quiescence, file integrity, or prior service health cannot be proved.

The native recovery test runs copied recovery code after deleting the bundle and
breaking the live candidate catalog. Its temporary backend is a benign process
model, not a real Android JVM test. Phone activation and Android runtime checks
remain separate acceptance steps.
