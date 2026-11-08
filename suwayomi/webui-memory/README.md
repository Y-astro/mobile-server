# Suwayomi WebUI Library Cover Memory Preload

This component implements session-scoped in-memory cover preloading for the Suwayomi-WebUI library view, as specified in `docs/superpowers/specs/2026-11-08-suwayomi-cover-memory-design.md`.

## Features and Guarantees

1. **Session-Scoped Memory Store**:
   - Completed encoded manga covers are retained in memory as object URLs.
   - Keys are full versioned cover URLs including `sourceId` and `fetchedAt`. Each displayed image owns a separate object URL backed by the retained Blob; eviction cannot revoke a displayed image.
   - Card unmount cleanup is safe: unmounting individual gallery cards does NOT revoke the shared object URL.
   - Reuse across category switches without re-querying or re-fetching.

2. **Foreground Priority & Bounded Concurrency**:
   - Background warmup is throttled to at most 2 concurrent low-priority transfers.
   - When foreground image requests are active, background transfers are paused and in-flight background transfers are aborted/re-queued to yield socket capacity immediately.
   - Background warmup automatically resumes when foreground is idle.
   - Failed, missing, or timed-out images (404, 15s timeout, network error) never abort or hang the batch.

3. **Security and Origin Isolation**:
   - Preloading and image requests are strictly restricted to the configured server origin and `/api/v1/manga/{id}/thumbnail` path, including configured subpaths (e.g. `/suwayomi/api/v1/manga/...`).
   - No credential leakage to external origins.
   - On account logout, token refresh failure, or server change, all owned object URLs are revoked, pending requests aborted, and late-resolving promises discarded via generation guards.
   - Reconciles versioned URLs on library additions, updates, and title removals.

4. **Strict Memory Budgets**:
   - Retained encoded memory is capped at 128 MiB with automatic LRU eviction.
   - Per-image cap is 16 MiB: oversize responses fall back safely to normal image loading.
   - HTML responses (e.g. login/auth redirects), empty bodies, and non-image payloads are never cached. Streaming enforces the size cap before Blob allocation. A cached image must decode successfully before display; invalid images are evicted and use the original loading and retry path.

5. **Preserved Upstream & Custom Semantics**:
   - Preserves existing custom `useGetRecentlyUpdatedChapters` unread-update-feed behavior (`0001-unread-updates-feed.patch`).
   - Clean, reviewable patches against upstream tag `v20260929.01` (commit `41443eaba44bcbdf45d666fe27471b50d41fd994`).

## Validation

Run the focused test suite synchronously against canonical TypeScript:

```sh
node --experimental-strip-types --test tests/test_cover_memory_store.mjs
```

Covers:
- Origin and path security validation with subpaths
- Reuse across unmounts without revoking shared cache
- Reconciliation on version change and library removal with in-flight abort
- Clear on logout / server change revokes all URLs and prevents late puts
- Memory limits: per-image cap, HTML rejection, and 128 MiB store LRU eviction
- Concurrency limit (max 2), deduplication, and failure resilience with timeout
- Foreground priority preempts in-flight background and resumes on idle


## Build and activation

Run `bash suwayomi/webui-memory/build-webui.sh`. The script exports pinned upstream source into a fresh ignored build directory, applies both complete patches, compares canonical modules, checks TypeScript, and runs the real production build. It writes a deterministic archive and full file/input SHA-256 provenance to `.work/artifacts/`. It never resets or cleans another checkout. Dependencies remain workspace-local and use upstream's pinned package manager and frozen lockfile.

The archive records upstream base revision `r3518`; the SHA-256 provenance distinguishes this custom build. It does not claim to be the unmodified official release. The phone publication mechanism and actual deployed browser acceptance are separate gates. The existing backend JAR contains an older customized UI resource; a custom frontend must use Suwayomi's native CUSTOM flavor so startup validation does not replace it.

Encoded memory is bounded; decoded image/GPU residency is browser-managed. A newly opened tab must warm its own cache. First-page JavaScript and GraphQL startup costs remain separate from cover warming. No chapter pages, resized derivatives, or new server daemons are added.
