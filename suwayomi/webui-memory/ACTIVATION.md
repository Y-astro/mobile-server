# Suwayomi WebUI Activation & Publication Memory

## Architecture and Constraints

1. **Backend Integration**:
   - Upstream Suwayomi Server (`40bc5da57820324ed0ae724cbbc020cc4673b594`) copies `data/webUI` to `PREFIX/tmp/Tachidesk/webUI-serve` on startup.
   - Setting `server.webUIFlavor = "CUSTOM"` in `server.conf` activates the local WebUI directory and skips remote upstream hash checks/overwrites.
   - Preserves proven backend JAR, intentional DNS shims, Caddy routing, and concurrent AGY / `web/admin.html` drift.

2. **Artifact Interface**:
   - Input: `WebUI.zip` and `WebUI.provenance.json` produced by reviewed build.
   - Provenance specifies `archive_sha256` and strict mapping `files: { relative_path: sha256 }`.
   - Tool verifies archive digest, entry counts, uncompressed size limits, and forbids path traversal (`..`), absolute paths, symlinks, duplicate entries, and unmanifested files.

3. **Asset Preservation**:
   - In-flight tabs holding older HTML chunks require existing hashed lazy assets (e.g. `assets/*.js`, `assets/*.css`).
   - Publication preserves prior hashed assets into the candidate directory if not present in the new manifest.
   - Preserved assets are included in the expected final manifest verification.

4. **Lifecycle & Concurrency Control**:
   - Reuses `TermuxLifecycleDriver` lock on `"services"` (`timeout=10`) across stop, swap, start, and health-check steps.
   - Refuses mutation if Suwayomi is paused or disabled.
   - Tracks unrelated process identities (`caddy`, `antra`, `navidrome`, `suwayomi_scheduler`) and mutates none.

5. **Atomic Swap & Durable Journaling**:
   - Feature-tests `renameat2(RENAME_EXCHANGE)` before stopping services; falls back to staged atomic moves if unsupported.
   - Records durable `publication_journal.json` in staging before mutations.
   - Automatic rollback on health or manifest verification failure restores previous tree and configuration, then restarts backend.
   - Explicit `--recover` operation safely recovers from interrupted states without arbitrary overwrites.
