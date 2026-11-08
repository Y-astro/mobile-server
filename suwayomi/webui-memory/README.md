# Suwayomi WebUI Library Cover Memory Preload

This component implements session-scoped in-memory cover preloading for the Suwayomi-WebUI library view, as specified in `docs/superpowers/specs/2026-11-08-suwayomi-cover-memory-design.md`.

## Features and Guarantees

1. **Session-Scoped Memory Store**:
   - Completed encoded manga covers are retained in memory as object URLs.
   - Keys are full versioned cover URLs including `sourceId` and `fetchedAt`.
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
   - HTML responses (e.g. login/auth redirects) and non-image payloads are never cached.

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
