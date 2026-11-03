# Release File Transaction Implementation Plan

**Approved scope:** Coherent software deployment, unexpected live-drift refusal, preserved service intent and rollback are required by the approved stabilization design. This subproject implements only the standalone file transaction. A separately reviewed service activation adapter must quiesce affected services before production use.

**Goal:** Prepare a complete validated candidate, refuse changed live preconditions, atomically replace changed owned files, and restore the prior compatible file set after failure or interrupted application. No service controls or live deployment in this task.

**Base:** 429dace. Existing release-manifest.py is reviewed and accepted. It validates immutable35-row historical metadata, exact source ownership, fresh candidate hashes, safe flat helper discovery, and explicit gateway artifact. Never change its frozen baseline to conceal drift.

## Task 1: Standalone stdlib file transaction and recovery

Create `scripts/release_transaction.py`, `tests/test_release_transaction.py` and focused documentation `docs/RELEASE-TRANSACTIONS.md`. Do not change existing app, web, config, installer, supervisor, manifest validator or unrelated tests. Use stdlib only. This is one Termux file transaction, not a deployment platform or plugin system.

### Ownership and inputs

- Python API prepares/applies/rolls back/commits a transaction with explicit bundle root, candidate manifest, reviewed installed preconditions, transaction directory and live namespace root. Manifest remote paths remain under `/data/data/com.termux/files/`; production namespace root is that directory, fixture roots are temporary stand-ins. Do not infer or auto-bless installed preconditions from whatever happens to be live during prepare.
- Load reviewed release-manifest.py from its sibling script only during prepare; validate actual bundle and require gateway before staging. Do not import dashboard or app helpers. Recovery must not require the original bundle, app imports or running services.
- Preconditions cover EVERY candidate destination, including new-path absence, existing bytes/SHA256/mode/UID, and explicitly recorded symlink metadata. Reject missing, extra or duplicate preconditions, unexpected types, wrong UID, drift and escaping source/target paths before changing live source files. Recheck live preconditions immediately before apply, not only prepare.
- Root read-only metadata evidence is in the parent implementation worktree `.superpowers/sdd/live-release-preimage-metadata.json`; do not contact phone.35 audited content hashes still match. New helper paths are absent. Gateway executable preimage is recorded separately because historical source baseline excludes the built binary.
- One existing owned alias MUST survive: `home/start_services.sh` is a symlink to `/data/data/com.termux/files/home/.termux/boot/start-services.sh`. Its target content matches audited script SHA25690dd7aacc85f3289ed53e35752b4dc804d48c92abd29136f4cc81b63f89d0bcc,316bytes,mode0700,UID10505. Preserve alias lstat/link text; install/restore the regular boot target. Permit only this explicitly recorded alias after containment and identity checks. Reject arbitrary target symlinks and escaping/symlinked parents. Fixtures may use an equivalent relative link within their temporary namespace.
- Owned modes: dashboard executable0700, Python helper modules0600, gateway0700, ordinary scripts0700, agy-runner-loop0755, web/config0600. Keep exact prior modes for rollback. Files with identical candidate hash and desired mode remain untouched, including inode/mtime. No user DBs, notes, credentials, keys, histories, runtime flags or unrelated files are backed up or installed.

### Transaction and recovery

- Stage candidate files and backups privately (0700 directories/0600 metadata), verify staged hashes before apply, and verify backup hashes before any restoration. Finish all prepare validation/backups before live source replacement. Capture only changed owned files, not user data.
- Use one namespace-scoped flock to prevent concurrent file operations. Different prepared transactions may coexist, but a stale transaction must refuse apply after another changes the live preconditions. Do not hold locks in children or create a second service supervisor.
- Persist a versioned atomic journal with states such as PREPARED, APPLYING, APPLIED, COMMITTED, ROLLED_BACK and RECOVERY_REQUIRED. fsync journal/files/parent directories at meaningful durability boundaries. Record each entry's write intent BEFORE atomic target replacement so a process dying between replacement and journal completion can recover.
- Install helpers/assets before executable entrypoints. Use temporary sibling files and os.replace, preserving the known alias. Verify candidate bytes/modes after replacement. On ordinary apply failure, restore the entire touched preimage set; record recovery failure explicitly instead of claiming success.
- Recovery restores files only when current state matches the recorded original or candidate state; refuse unknown concurrent edits instead of overwriting them. Validate all backups and recovery decisions before restoring any file. New candidate files return to absence. Repeated rollback/recovery and completed apply/commit calls must be idempotent and must not silently rewrite files.
- Save a standalone recovery copy of this stdlib script alongside the journal. A CLI for status and file rollback is appropriate; do NOT expose a production activation/apply CLI yet. Document that file-only recovery does not restore service-disabled flags or start services; the later service adapter owns that journal and sequence. Do not claim tested power-loss guarantees beyond implemented fsync and interruption fixtures.

### Required proof and stop condition

Use temporary bundle/live roots only. Build fixture bundles from maintained sources and the unchanged pinned metadata; synthetic gateway bytes are acceptable for hash-gate tests and must never be executed.

Prove: complete preparation/application; hash, mode, UID and new-path drift refusal before live writes; stale prepare refusal; candidate/staged tamper rejection; unchanged files untouched; known boot alias preserved and restored; traversal/symlink/secret rejection; independent operation lock; injected mid-apply failure rolls all touched files back; interrupted APPLYING state recovers in a fresh subprocess using only transaction directory/recovery script after bundle removal; unknown concurrent file edits and corrupted/missing backups refuse recovery; modes and absent new files restore; retries are idempotent. Mock only deterministic failure points, never process controls or real phone operations.

Run focused transaction tests and existing release-manifest tests once, diff --check and syntax compilation. No browser, Go, phone SSH or actual installer/service checks. Commit only scoped files. Report exact API/schema, validation, interruption guarantees and remaining activation adapter in `.superpowers/sdd/release-transaction-report.md`. Stop after tested local commit. Root and independent reviewer must accept before any live use.
