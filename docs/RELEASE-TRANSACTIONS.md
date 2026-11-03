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
# Idempotent: repeated apply verifies candidate live state and returns {"state": "APPLIED", "applied_count": 0}

# 4. Finalize transaction
tx.commit(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "COMMITTED", "status": "committed"}

# 5. Recovery (for interrupted, failed, or applying transactions)
res_rc = tx.recover(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "ROLLED_BACK", "status": "recovered"}
# If already COMMITTED: verifies live candidate state and returns {"state": "COMMITTED", "status": "no_op"}

# 6. Explicit Rollback (caller-initiated reversion of applied or committed releases)
res_rb = tx.rollback(namespace_root="/data/data/com.termux/files")
# Returns: {"state": "ROLLED_BACK", "status": "recovered"}
```

## 4. Mode Policy & Write Safety

- **Canonical Permissions**:
  - `0755` strictly for `home/.local/bin/agy-runner-loop` (executable by runner processes across UIDs).
  - `0700` for executable binaries and scripts (`dashboard`, `phone-gateway`, `start-services.sh`, `cloudflared`, etc.).
  - `0600` for runtime helper modules (`auth.py`, `notes.py`, `document_conversion.py`), configs, and web assets.
  - Staged files and backups are stored strictly with `0700` directory and file permissions.
- **Write-All Semantics**:
  - All file and journal payload writes utilize `write_all(fd, payload)` handling partial writes, retrying on `EINTR`, and detecting zero-write buffer saturation (`ENOSPC`).
  - Temporary sibling files (`.tmp_*`) are validated for exact byte size, SHA-256 hash, file mode, and expected UID before invoking `os.replace` on live targets (during both apply and rollback).
  - Unused or failed temporary sibling files are cleaned up fail-closed.
- **Directory Durability**:
  - `fsync_dir` ensures directory metadata durability and propagates `EIO`, `ENOSPC`, and `EBADF` fail-closed. On filesystems where directory sync is unsupported, `EINVAL` and `ENOTSUP` are safely tolerated.
- **Locked State & Idempotence**:
  - Mutating operations (`apply`, `commit`, `recover`, `rollback`) acquire `NamespaceLock` before reading or mutating state, and reload the journal under lock to eliminate race windows.
  - Namespace overrides that conflict with the transaction's recorded namespace are rejected fail-closed.
  - Internal journal paths (`staged_file`, `backup_file`) are strictly validated against directory traversal and absolute path escapes.

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

Production service quiescence and service reactivation are the responsibility of a separately reviewed service activation adapter.
