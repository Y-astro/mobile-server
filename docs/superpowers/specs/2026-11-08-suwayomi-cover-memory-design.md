# Suwayomi website library cover memory preload

The user explicitly requests preloading existing library title covers in memory. The website is slow; the companion app works normally. This design implements that requested behavior while the corrected performance baseline is collected. No production activation is allowed before root acceptance.

## Evidence and alternatives

The current library has 280 titles and 266 cached cover files, totalling 74,843,572 encoded bytes. Sampled server-loopback delivery is fast. The website uses ordinary image URLs containing `sourceId` and `fetchedAt`, through an image queue and Service Worker. Component cleanup does not establish a repeated network transfer. Earlier timing fields incorrectly measured resource duration instead of click-to-visible latency.

1. **Website memory preload — selected.** Reuse the existing request client, retain completed encoded image data/object URLs for the current library, and warm covers after foreground demand. This directly implements the user's request.
2. **Smaller website-only derivatives — deferred.** They may reduce first-transfer and memory cost, but require separate measured proof and version/invalidation handling. Preserve originals.
3. **Another server RAM daemon — not selected.** No material warm-server storage bottleneck has been demonstrated. It would not remove client request, transfer or rendering delays.

## Scope and behavior

Use the existing WebUI request/image-cache architecture, not an injected DOM shim, a new framework or a new daemon. Only in-library manga title thumbnails qualify. Never preload chapter pages, browsing/history records or unrelated images.

A session-scoped encoded cover store returns ready library images across category remounts. Foreground misses retain the current request path and error handling. A bounded background worker warms the library at low priority, with at most two transfers. It pauses/cancels background work when visible requests need the connection. Failed or missing covers cannot block other covers, cache an HTML/login response, or disable the existing manual retry.

Retain exact versioned URLs, including source ID and fetched timestamp. Reconcile library additions, removals and changed URLs. Clear/revoke owned object URLs when the server/account changes or the session is cleared. Individual card unmount must not revoke a shared loaded cover. Use the existing authenticated request client and configured server origin; do not send credentials to arbitrary image origins.

Bound encoded memory at 128 MiB and each background image at 16 MiB. Current cached library fits this budget. On timeout, overflow or decode failure, keep normal foreground loading available. Do not create a persistent private-library cache as part of this change. Report encoded and decoded memory separately; retaining compressed bytes does not prove all GPU textures stay resident.

## Source preservation and build

Upstream source candidate: `Suwayomi/Suwayomi-WebUI`, tag `v20260929.01`, commit `41443eaba44bcbdf45d666fe27471b50d41fd994`, associated with official preview release `r3518`. Official ZIP SHA-256: `c86e9851aa184fbd2f94c7d424438c63b9015c216e70daef76c9e204e157fea7`.

Current embedded/custom `WebUI.zip` SHA-256 is `9ff567868091f90653d3e544c8558fb3a9d4dd9e26ce98b2d9d85980c9253a09`, already pinned in maintained package inputs. Its only main-bundle change from official r3518 is the existing recently-updated-chapters filter: optional caller filter, unread in-library default, and forwarding the explicit filter during pagination. Preserve this behavior in source and tests. Never patch compiled phone JavaScript in place.

Maintain source patches, source/compiler/dependency pins and focused tests in this repository. Do not assume the existing class-only JAR packager supports WebUI resources. Build and production asset activation are separate acceptance steps.

## Acceptance

- Focused tests cover preload concurrency/foreground priority, reuse across unmounts, missing/failed covers, URL-version invalidation, library removal, account/server cleanup, memory caps and cancellation.
- Type-check and build the pinned frontend; retain the existing unread-feed behavior.
- Exercise the actual built gallery with real server data and category clicks. Compare cold entry, first category visits and repeated switches using action-to-visible timing, with failed/pending images counted separately.
- No material initial-visible-cover regression; retain a performance change only when comparable measurements show benefit. Distinguish Service Worker interception from a cache hit.
- Preserve original covers, database/read/download state, custom JAR and intentional DNS shim. Current observed runtime has 14 healthy enabled services and four disabled services. AGY enablement and a concurrent dashboard HTML edit must be preserved.

The exact website URL remains pending from the user. Physical Tsumiru behavior is outside this website-only change.
