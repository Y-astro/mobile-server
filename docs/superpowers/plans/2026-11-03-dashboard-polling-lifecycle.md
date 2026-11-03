# Dashboard Polling Lifecycle Implementation Plan

**Approved scope:** Isolated, bounded single-flight dashboard polling, hidden-page pause/visible refresh and preserved user state under the existing stabilization design. Appearance and mutation contracts remain unchanged.

**Goal:** Give each dashboard refresh loop explicit ownership and cancellation instead of unmanaged intervals. Keep independent features responsive when another endpoint stalls or fails. This slice does not extract notes/converter implementations; that remains a separate feature-ownership task.

**Base:** 85f2aa0. Catalog/backend/release/Caddy changes accepted. Combined99 Python tests pass, with2 explicit offline Caddy skips; actual adapter equivalence already verified.38 maintained source entries validate. Template-authority work runs separately and removes obsolete embedded HTML; do not edit those obsolete Python strings.

## Task 1: Owned polling and complete asset delivery

Create `web/portal-polling.js` as a small dependency-free lifecycle helper. Modify `web/admin.html` polling integration and `web/laptop.js` refresh reads. Add the fixed authenticated asset route in `app/dashboard.py`, explicit reviewed asset ownership/completeness in `scripts/release-manifest.py`, focused tests and existing browser fixture asset serving as necessary. Preserve frozen historical baseline, Caddy, supervisor, gateway, CSS and family/login markup.

### Guidance and behavior

- Read/apply `/home/astro/.agents/skills/unslop/SKILL.md` with the already approved preservation direction. No styling, layout, icons, copy makeover, framework or new dependency.
- Root ran modern-web-guidance search and retrieved guides. Artifacts in parent implementation `.superpowers/sdd/polling-modern-web-search.json` and `polling-modern-web-guidance.md`. Historical initial-visibility analytics and offscreen content-visibility CSS guides do not fit this task; do not add those unrelated features. Current visibilitychange + document.hidden and AbortController are appropriate. Primary references: https://developer.mozilla.org/en-US/docs/Web/API/AbortController , https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API , https://playwright.dev/docs/clock . Clock installation must precede timer use in tests.
- Preserve refresh intervals: stats3000ms, VPN10000ms, services4000ms, AGY sessions5000ms, laptop8000ms. Schedule the next read after completion, with at most one in flight per feature. Use a conservative10000ms deadline for background reads with AbortController; injected short intervals/timeouts or Playwright clock may be used by focused helper tests without production test flags.
- Hidden documents start no polling. Pause timers and abort owned background GETs on hidden/pagehide. Refresh promptly on visible/pageshow, including back/forward-cache return, with no duplicate timers or overlap. Coalesce explicit refresh calls with any existing read. Stop/dispose paths remove timers/listeners/controllers. Guard against late aborted results mutating the panel.
- A slow, failed or aborted stats/VPN/services/AGY/laptop read must affect only its own refresh loop. Retain existing authorization redirects and unavailable/stale presentation. Do not create new decorations or global alerts. Visibility cancellation must not flash spurious network errors.
- Only background GETs belong to polling cancellation. Do not abort/retry notes saves, conversions, service toggle POSTs, session create/delete, popup handling or terminal connections. Preserve existing inline global handler names and mutation behavior. Manual post-operation refresh calls must use the same single-flight read owner. Preserve laptop changing/refreshing guards and operation error display until its next scheduled status check.
- Existing notes debounce750ms, edited text, file selections and in-progress conversion must survive hidden/visible lifecycle. Do not reinitialize utilities or reset user state on resume. Keep family/login behavior unchanged.

### Source and release ownership

- Include the helper once, in an execution order guaranteed to exist before use. Missing module must not throw an uncaught error that prevents unrelated controls from initializing. Keep actual maintained templates as the test source; do not update stale embedded HTML that the separate template task removes.
- Add only fixed `/static/portal-polling.js` serving beside existing laptop asset handling, protected with the same admin gate and raw-byte/no-cache/Content-Length behavior. Admin receives exact module bytes, family403, unauthenticated302, missing asset503. No arbitrary static path resolver or traversal surface.
- Declare `web/portal-polling.js` -> `/data/data/com.termux/files/home/.config/dashboard/static/portal-polling.js` in one small additional ownership map in the reviewed manifest tool. Keep the35-row historical metadata and its pin untouched. New asset is mandatory in generation AND validation; a candidate omitting it must fail even if the file is present in bundle. Existing app-helper and gateway semantics remain intact.
- The legacy installer has a separate hardcoded asset list. Add a small fail-fast guard before any source writes stating transactional activation is required; do not implement a competing deployment path. The separately reviewed service activation adapter will replace that guard with delegation. No installer execution in this task.

### Meaningful proof

- Focused lifecycle/helper and actual-page browser tests prove no overlap under delayed responses, deadline cancellation/recovery, failed feature isolation, hidden-page zero new reads, prompt visible refresh, pagehide/pageshow restart without duplicate timers, manual refresh coalescing and no global exception when module unavailable.
- Use real maintained admin page and input-driven notes/file selection to prove edits and converter selection persist across lifecycle. Exercise converter opening through the actual admin card and family button in existing browser checks, replacing internal modal-helper calls where feasible. Mock endpoints and abort external requests; no real upstreams or credentials. Clock/control of visibility events is acceptable for lifecycle mechanics, but user-state assertions must follow actual input actions.
- Existing browser fixtures must serve the new module with JavaScript MIME rather than allowing unknown request stubs to return JSON. Keep all previous assertions. Add new focused browser suite to npm test. Existing memory scheduled-poll and laptop controls must still pass.
- Add HTTP asset gate/byte/missing tests using temporary fixture paths without colliding with the separate template fixture update. Add manifest required-asset tests. Default Python tests must not contact phone. No dependency installation.
- Run focused new checks, full Python once and existing+new npm browser suite once after fixes. Regenerate/validate source candidate and bash-n installer, diff --check. No Go or actual phone/service checks.

Commit only relevant web/module/HTTP asset/manifest/installer guard/tests/package integration and source-ownership documentation. Save large logs/screenshots in ignored `.superpowers/sdd`. Report exact checks, request-count/overlap observations from synthetic workload, lifecycle choices, mandatory asset mapping, and the incomplete production activation boundary in `.superpowers/sdd/dashboard-polling-report.md`. No claim of live performance improvement. Stop after tested local commit; root and independent review precede integration.
