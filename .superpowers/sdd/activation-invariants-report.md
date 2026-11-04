# Activation Invariants Correction Report

## Summary
Resolved root failure probes 1 and 2 in `scripts/release_activation.py`:
1. Atomic Flag Snapshot & Binding: Full snapshot of all affected services is persisted before creating the first guard. `validate_activation_journal` strictly enforces that journal flag mappings match recomputed affected services from verified file transaction entries, validating exact tokens, integer metadata types, and intents. Partial flags maps fail closed before any process signal, guard mutation, or file restoration.
2. Robust Guard Recreation & No-Clobber: Replaced `os.replace` clobbering with `O_EXCL` guard creation. Legitimate recreation on rollback clears stale dev/ino, journals `recreate_guard` intent, writes exact token payload, fsyncs dir, and records fresh dev/inode. Concurrent foreign markers and broken symlinks are refused via `ConcurrencyError` without clobbering.
3. Pre-rollback Quiescence Gap Verification: Post-gap re-check under `services.lock` verifies flag integrity, fresh `matching_processes`, and daemon quiescence immediately before file rollback.
4. Coherent Lifecycle & Recovery: Idempotent recovery for completed activations (`tx.recover`), backout for incomplete transactions even if files were committed, and strict preservation of guards on prior/candidate health failures.

## Verification
- `tests/test_release_activation.py`: 33 tests passed (including regressions for both root probes, flag mapping corruption, post-success explicit rollback, commit/guard removal interruption recovery, no-clobber concurrent markers, and unlocked daemon-stop gap candidate appearance).
- Full repository test gate: 234 tests passed, 2 skipped.
