# Expressive Workbench Deployment

The reference-based redesign is live for both portal accounts, login and shared dialogs, Antra, Repertoire, Cloud Storage, and terminal chrome. Matugen remains the only theme source. Appearance controls remain admin-only. The six excluded standalone applications retain their own interfaces: Suwayomi, Wealthfolio, Navidrome, Vaultwarden, AdGuard Home, and Syncthing.

## Release

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
