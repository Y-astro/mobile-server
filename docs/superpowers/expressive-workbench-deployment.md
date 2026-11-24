# Expressive Workbench Deployment

The reference-based redesign is live for both portal accounts, login and shared dialogs, Antra, Repertoire, Cloud Storage, and terminal chrome. Matugen remains the only theme source. Appearance controls remain admin-only. The six excluded standalone applications retain their own interfaces: Suwayomi, Wealthfolio, Navidrome, Vaultwarden, AdGuard Home, and Syncthing.

## Initial release

- Source commit: `11def743320a185b1a1433056b277a7af1603ee5`.
- Phone transaction: `act_e9558e013ace`, completed successfully on 2026-11-25 (IST).
- Complete manifest: 62 entries, 13 replacements, 49 untouched.
- Private release directory: `~/.local/share/mobile-server/releases/expressive-11def743320a/`.
- Archive SHA-256: `3ee1c6f29ba621fbd04cdff1c3a51ce141121157cc0f5ff5b623826f31959b04`.
- Enabled affected services restarted: dashboard and laptop terminal. Supervisor refreshed the catalog. No Caddy reload or gateway replacement occurred.
- Post-activation verification independently matched all 62 destination hashes. Seven disabled markers retained identical content and metadata. Observed Caddy, gateway, AdGuard and Suwayomi process PID/start identities were unchanged.

The laptop ttyd command adds only the reviewed `-I` shared custom-index pair. The production driver accepts this exact transition and its rollback; other command changes remain rejected. The AGY release exception permits only its exact static index and keeps session/runner guards.

## Verification

- Root ran 149 Python tests across release activation, transaction recovery, service catalog, role templates and Repertoire; all passed.
- Actual-template expressive browser suite: 12 assertion blocks passed. Cloud appearance suite: seven phases passed using the actual FileBrowser bundle. Terminal wrapper suite passed at desktop, mobile and short-window boundaries.
- Both terminal vendor bundles retained SHA-256 `42bb3a80a3cb5207e88f18b8dfe603a08f535196de1145fb65277dbbcabf0ac2`.
- Live browser checks exercised admin/family portal and Cloud, desktop and 390px mobile layouts, three default columns, 320px aside, fitting Cloud header actions, and no document overflow or uncaught page errors.
- Live admin auth returned 200; family admin/laptop checks returned 403; anonymous admin request redirected to `/login`. Family had no appearance, laptop, AGY or Tor controls.
- Live laptop HTML served the new wrapper. A separate process probe confirmed one ttyd on 7683 with the custom index, plus successful dashboard/Caddy/laptop HTTP health after detached activation.
- Representative live screenshots were inspected. Private notes were masked; no notes, files, account passwords, or service toggles were changed during live browser checks.

Root evidence is stored privately outside Git at `/home/astro/server-audit/2026-11-24/expressive-redesign/`: `final-python.log`, browser suite logs, `candidate-release.json`, `activation-observation.txt`, `live-post-activation.json`, and `live-browser-result.json`. The source candidate predates this documentation-only completion commit.

## Limits

AGY and Termux remained disabled, so live WebSocket sessions were not started. Local terminal tests mock transport; live laptop verification covers HTTP and process arguments without opening a new shell session. Existing terminal sessions were preserved. Antra's disabled backend was not started for a real download. Excluded native applications were not redesigned or enabled for testing. The Graphify machine graph retains its Nov 20 baseline date; the human-readable system map carries current verified UI ownership and service-state changes.

## Compact dashboard follow-up (2026-11-25)

User feedback identified the top header, dynamic service columns, service descriptions, and verbose laptop/AGY panels as mismatches with the supplied reference. The correction is live from `804c875d7e724d2f5360c1c7aa4577308895f81e`, transaction `act_44783c524b6d` (`COMPLETED`).

- Four assets replaced: `web/admin.html`, `web/family.html`, `web/laptop.js`, `web/portal.css`. The remaining 58 manifest entries stayed untouched. Only dashboard restarted; supervisor and terminal processes kept their identities.
- Header/profile/actions and narrow-screen instrument labels match the reference structure. Connection details retain live Tailscale/LAN values outside the header. CPU temperature remains live, and status/percentage values move to metric footers.
- Column selection now sets an intrinsic maximum instead of imposing a two-column cap below 1280px. Preferences 1–4 persist, default is three, and invalid stored values recover to three. A selected four renders four at 1280px, two at 390px; one remains one on mobile.
- Service descriptions remain available through launch hints and accessible text. All original navigation, converter, filter, reorder and toggle actions remain.
- Session panels use compact headers, icon actions with accessible names, full terminal-ID titles, readable long-name truncation, and native Session details disclosures. Targets remain at least 48px. No live terminal session was created or closed by verification.
- Root reran 32 role-template/manifest tests, the 12-block actual-template browser suite, and all seven sidebar service-control browser checks. Checks cover 1440, 1280, 768, 390 and 320px widths, long session names, intermediate column preferences and invalid stored values.
- Live admin/family portal, Cloud, role gates, responsive column changes and laptop HTML checks passed. All 62 destination hashes, including the unchanged boot-wrapper symlink target, match the candidate. Seven disabled markers retained identical metadata. Every observed process except dashboard kept its PID/start identity.
- Current live geometry at 1440px: header 48px, first service tile about 136px, laptop panel about 337px with two sessions, disabled AGY panel 182px. Baseline measurements were header 77px, first tile about 200px, laptop 549px and AGY about 344px; session populations were not controlled between observations.
- HTTP responses for `/portal.css` and `/static/laptop.js` matched committed bytes. The JS response carries `no-cache`; existing browser caches may require a hard refresh for CSS.

Private evidence is in the audit's `compact-release/` directory: candidate manifest, local/remote plans, completed transaction status, `live-browser-result.json`, `final-proof.json`, HTTP asset hashes and masked live screenshots. Local UI screenshots are in `compact-preview/`. AGY's second attempt reached its quota; root completed, reviewed and verified its partial edits. Disabled AGY/Termux and excluded native interfaces remain unexercised as running applications.

## Neutral remote panels and running-state tint (2026-11-25)

The user's two screenshots requested neutral laptop/AGY surfaces, tighter session presentation and accent fills for active service cards. Source `f69d7e8ffac0a944af6c775c394d8e9cfac11c4c` is live through completed transaction `act_c991a2f2df2e`.

- Three assets replaced: admin template, laptop session renderer, shared portal CSS. All 59 other manifest entries stayed untouched. Only dashboard restarted.
- Removed the terminal-specific colour fill. Both remote panels now resolve to the same neutral surface. New and native Session details share one toolbar; clicking the session label reconnects, while Close and protected-primary semantics remain intact. Action targets remain 48px.
- The old shared glass `!important` rule masked active service backgrounds. A narrowly scoped running-state selector now applies the Matugen accent container with the existing translucency value. Ready converter cards and cards without a verified live running state stay neutral. Family's static navigation cards do not invent backend availability.
- Root reproduced the missing tint before the fix. The updated actual-template browser suite passes active/disabled/Ready colours, on/off tint transitions, matching remote surfaces, denser rows, keyboard disclosure, long names, responsive layouts and account boundaries. All seven sidebar checks, the dedicated laptop create/reconnect/close and failure-recovery browser suite, and 28 role/laptop/AGY Python tests passed.
- Live checks passed for admin/family portal, Cloud, role gates, colours, compact layout and keyboard disclosure. At 1440px, the laptop panel measured about 207px with one session; disabled AGY measured 168px. All 62 destination hashes matched, seven disabled-marker records were identical, and every observed process except dashboard retained its PID/start identity.
- Runtime service flags, native interfaces, notes/files, shell-session transport and lifecycle rules were preserved. No live terminal session was created or closed during verification. AGY remained quota-limited; root completed this bounded correction directly.

Private evidence: `neutral-before.log`, `neutral-browser.log`, `neutral-sidebar.log`, `neutral-laptop.log`, `neutral-python.log`, screenshots in `neutral-preview/`, and complete release/live receipts in `neutral-release/` under the same audit directory. A hard refresh may be needed for existing CSS caches.
