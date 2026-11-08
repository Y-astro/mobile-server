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
   - When foreground image requests are active, background transfers are paused.
   - Background warmup automatically resumes when foreground is idle.
   - Failed or missing images (404, timeout, network error) never abort the batch.

3. **Security and Origin Isolation**:
   - Preloading and image requests are strictly restricted to the configured server origin and `/api/v1/manga/{id}/thumbnail` path.
   - No credential leakage to external origins.
   - On account logout, token refresh failure, or server change, all owned object URLs are revoked and pending requests aborted.
   - Reconciles versioned URLs on library additions, updates, and title removals.

4. **Strict Memory Budgets**:
   - Retained encoded memory is capped at 128 MiB with automatic LRU eviction.
   - Per-image cap is 16 MiB: oversize responses fall back safely to normal image loading.
   - HTML responses (e.g. login/auth redirects) are never cached.

5. **Preserved Upstream & Custom Semantics**:
   - Preserves existing custom `useGetRecentlyUpdatedChapters` unread-update-feed behavior (`0001-unread-updates-feed.patch`).
   - Clean, reviewable patches against upstream tag `v20260929.01` (commit `41443eaba44bcbdf45d666fe27471b50d41fd994`).

## Validation

Run the focused test suite synchronously:

```sh
node --test tests/test_cover_memory_store.mjs
```

Covers:
- Origin and path security validation
- Reuse across unmounts without revoking shared cache
- Reconciliation on version change and library removal
- Clear on logout / server change revokes all URLs and aborts pending
- Memory limits: per-image cap and 128 MiB store LRU eviction
- HTML response rejection prevents caching login/redirect errors
- Concurrency limit (max 2) and failure resilience
- Foreground priority pauses background queue and resumes on idle
