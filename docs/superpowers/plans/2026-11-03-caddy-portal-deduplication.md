# Caddy Portal Deduplication Implementation Plan

**Approved scope:** The server stabilization design authorizes consolidating equivalent portal configuration while preserving every access, transfer, reader, and Wealthfolio invariant.

**Goal:** Extract the repeated HTTP8000 and HTTPS8443 portal body into one same-file Caddyfile snippet. Keep their listener and TLS differences explicit. Do not change routing behavior, security policy, appearance, ports, upstreams, headers, ordering, body limits or timeouts.

**Base:** 965bc71. Integrated backend/memory/auth/conversion proof:69 Python tests and all browser suites pass. Current Caddyfile has503 lines. A read-only adaptation through installed phone Caddy already succeeds, producing30239 JSON bytes and four HTTP servers. Baseline artifact is in the parent implementation worktree `.superpowers/sdd/caddy-baseline-adapted.json`.

## Task 1: Consolidate equivalent portal routes and prove adapter equality

Scope `config/Caddyfile`, a focused `tests/test_caddy_routes.py`, a tracked historical fixture `tests/fixtures/Caddyfile.before-portal-dedup`, and brief operations documentation if needed. No app, web, supervisor, installer or historical deployment-baseline edits.

- Capture the original Caddyfile byte-for-byte as a tracked, sanitized historical test fixture before editing. The fixture must come from base965bc71, not from the refactored output. It is a test oracle, not an alternate maintained runtime configuration.
- Confirm equivalence of the two portal bodies first. Share only identical directives in one same-file named snippet and import it from the8000 and8443 sites. Preserve8443 `tls internal { on_demand }` separately. Do not flatten route order or deduplicate unrelated native/Wealthfolio routes.
- Preserve global admin socket, full duplex and timeouts; portal CSP/cache/frame/referrer headers; FileBrowser branding/static injection/raw active-content sandbox/attachment protections and forward identity; gateway flush/buffers/timeouts; admin-only terminal/AGY/laptop/vault/screenshot/reader routes; thumbnail cache and GraphQL filter routing.
- Keep Wealthfolio8444 and native client4567 blocks byte-for-byte unchanged, including inline offline HTML, comments and access policy. Preserve all existing comments/docstrings throughout moved sections. No visual changes.
- Use actual Caddy adapter output as decisive proof: adapt original fixture and current candidate with the SAME installed binary, parse JSON, compare entire structures with array order preserved. Do not compare only selected port strings or a hand-written approximate parser.
- No Caddy binary is installed on development host. Explicitly authorized READ-ONLY adapter command: `ssh -o BatchMode=yes -o ConnectTimeout=10 -p 8022 u0_a505@100.75.159.53 'caddy adapt --config - --adapter caddyfile'`, source supplied through stdin. This only parses configuration. Do not use `--validate`, `caddy validate`, run/start/reload/stop, write files on phone, read private keys, install dependencies or contact services through candidate configuration.
- Add reusable focused tests that use local `caddy` if present, or opt into the fixed read-only SSH adapter with an explicit environment flag such as `CADDY_TEST_SSH=1`. Default Python tests must not silently contact the phone. If no adapter is available, report explicit skip; acceptance requires the opt-in real adapter checks to have passed. Bound subprocesses, check return codes and JSON parsing, and fail on route differences.
- Cover both portal listeners, auth-check versus auth-check-admin, FileBrowser raw response protections and streaming settings, native4567 auth policy, and Wealthfolio8444 unchanged. Full adapted JSON equality is the primary preservation test; targeted assertions explain the critical invariants. Do not write tests that only inspect fixture against itself.
- Run focused tests with the phone adapter opted in, diff --check and configuration adaptation. No browser or Go checks; no unrelated full Python repeat unless test integration warrants it. Save large adapter JSON/diffs/logs in ignored `.superpowers/sdd`.
- Commit only scoped config/tests/fixture/docs. Report exact before/after line count, same-adapter equivalence, critical route coverage, adapter version and limitations in `.superpowers/sdd/caddy-portal-report.md`. Stop after tested local commit. Independent review and root verification precede integration/deployment.

Official references checked by root: https://caddyserver.com/docs/caddyfile/directives/import and https://caddyserver.com/docs/command-line#caddy-adapt . Import expands snippets before parsing; adapt accepts stdin and emits native JSON. Provisioning validation is a later deployment step, not part of these read-only checks.
