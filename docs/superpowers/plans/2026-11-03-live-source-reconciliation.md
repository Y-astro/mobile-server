# Live Source Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the maintained source represent the current phone, exercise actual served dashboard pages, and repair confirmed API/packaging mismatches without losing live features.

**Architecture:** Reconcile the deployed custom code before structural edits. Use existing Python, Go and Playwright harnesses, then move them to actual role templates. Keep runtime data outside releases and Git. This is the first independently testable subproject of the approved full-server refactor; backend/frontend extraction, shared service/proxy ownership and deployment activation follow in separate bounded plans against the reconciled source.

**Tech Stack:** Python standard-library HTTP server, plain JavaScript/HTML/CSS, Caddy, Go gateway, unittest and Playwright.

**Spec:** docs/superpowers/specs/2026-11-03-redmi-server-stabilization-design.md

## Global Constraints

- Preserve current service behavior, appearance and user data.
- Keep credentials, signing keys, private certificates, databases, user files, session history and generated artifacts outside Git.
- Preserve URLs, status codes, cookie handling, role boundaries, response fields and operational CLI behavior, except for explicitly tested bug fixes.
- Both conversion routes must share the same size and concurrency limits.
- Keep SSH available. Do not use global pause/resume for routine dashboard updates.
- Disabled services remain disabled. No database or JVM changes belong to this subproject.
- Preserve backend :4569, filter :4566, native-client bridge :4567, portal /suwayomi, cached thumbnail routing, Category 1 Reading behavior and midnight IST scheduler behavior.
- Use AGY for bounded execution; Codex independently reviews source, behavior and evidence. Only one designated deployment owner may write phone code or restart services. These first tasks have read-only phone access.
- Read/apply /home/astro/.agents/skills/unslop/SKILL.md for every interface task; preserve appearance. Use modern-web-guidance before client JavaScript/HTML changes.

---

### Task 1: Reconcile deployed custom source into maintained Git

**Files:**
- Modify: app/dashboard.py, app/auth.py, app/telemetry.py, app/limits.py, scripts/server_supervisor.py, scripts/battery_guard.sh, scripts/agy_session.sh, config/Caddyfile, web/filebrowser.js to match verified live counterparts.
- Create: web/admin.html, web/family.html, web/login.html, web/portal.css, scripts/start_suwayomi.sh, scripts/suwayomi_daily_update.py, scripts/suwayomi_graphql_filter.py, scripts/suwayomi_update.sh from verified live custom sources.
- Inspect and reconcile named live counterparts for existing web/agy_index.html, scripts/server-user, scripts/server-status, scripts/server-health, scripts/server-up, scripts/server-down, scripts/server-restart, scripts/agy-resume, scripts/termux_session.sh, scripts/laptop_session.py and scripts/start-services.sh.
- Create: docs/SOURCE-OWNERSHIP.md and config/deployment-files.json (sanitized path ownership and source hashes, no runtime secrets).
- Create: AGENTS.md with maintained ownership and verification rules.
- Preserve: gateway source unless independently located live source proves a later version; report binary/source uncertainty.

**Interfaces:**
- Consumes: /home/astro/server-audit/2026-11-03/source-manifest.json, a list of local repository path, remote absolute path, sha256 and comparison state.
- Produces: actual role pages at web/{admin,family,login}.html, shared web/portal.css and matching current custom backend/helpers/scripts. config/deployment-files.json records local source -> phone destination with verified SHA-256. Runtime filenames remain unchanged.

- [ ] **Step 1: Preserve a local read-only source capture and compare recorded hashes.**

Use SSH port 8022, user u0_a505, host 100.75.159.53. For an existing manifest row, the literal transfer command shape is:

```sh
scp -q -P 8022 -o BatchMode=yes -o ConnectTimeout=10 u0_a505@100.75.159.53:/data/data/com.termux/files/usr/bin/dashboard .superpowers/sdd/live-source/app-dashboard.py
```

Build a local capture script in ignored .superpowers/sdd that iterates the supplied manifest, creates safe parent directories and invokes subprocess.run with an argument list, never shell interpolation. Fetch all source paths before importing any. Compare each hash with the recorded value; on unexpected drift, record changed path/hash without printing source or secrets and reconcile deliberately. Do not capture runtime JSON/env/key/DB files or obsolete source copies. Read applicable repository instructions before edits.

- [ ] **Step 2: Inspect potential secret-bearing source without printing values.**

Use AST/static inspection of captured source, and report only suspicious path/line locations. Do not commit a source file containing live passwords, tokens or signing keys. Existing users.json and session key remain on the phone; they are never capture inputs. Import exact custom code/assets when no sensitive literals are present. Retain all existing comments. For additional named files use remote home /data/data/com.termux/files/home and prefix /data/data/com.termux/files/usr. Static files reside under ~/.config/dashboard/static, ttyd HTML under ~/.config/ttyd, custom helpers under ~/.local/lib/mobile-server and launchers under ~/bin or prefix/bin.

- [ ] **Step 3: Record source ownership and the contributor contract.**

Document that app/dashboard.py owns HTTP composition, app/auth.py credential verification, app/limits.py admission, app/telemetry.py measurements, scripts/server_supervisor.py lifecycle, config/Caddyfile edge routing, gateway/ streaming transport and web/ actual frontend assets. Describe external templates overriding embedded fallbacks. Document existing native reader and disabled-service invariants. AGENTS.md requires edits in maintained source, actual-page browser checks and deployment through the reviewed release mechanism rather than phone-only patching. It must require reading /home/astro/.agents/skills/unslop/SKILL.md for visual tasks.

- [ ] **Step 4: Run existing baseline proof without changing runtime behavior to satisfy old fixtures.**

```sh
python3 -m unittest discover -s tests -q
(cd gateway && go test -race ./...)
NODE_PATH=/home/astro/repos/mobile-server/node_modules CHROMIUM_PATH=/home/astro/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm test
git diff --check
```

If an existing test fails because it assumes an older live interface, report the exact mismatch and preserve live behavior. Do not hide the failure, disable the test or revert a live feature. Small fixture updates that accurately represent preserved behavior are permitted with explanation. No new contract fix belongs to this task.

- [ ] **Step 5: Commit only reconciled sanitized source and ownership docs.**

```sh
git add app scripts config web docs/SOURCE-OWNERSHIP.md AGENTS.md
git commit -m "chore: reconcile maintained source with live Redmi deployment"
```

Before committing inspect staged names and secret-sensitive patterns without dumping values. Return commit IDs, test commands/results, source hash comparison and unresolved mapping gaps in the named report file. No phone writes or restarts.

### Task 2: Repair actual dashboard contracts and test served pages

**Files:**
- Modify: scripts/browser-fixture.py, tests/browser.cjs, tests/laptop-browser.cjs, tests/test_dashboard.py, tests/test_auth.py, app/auth.py, app/dashboard.py, app/limits.py, web/admin.html.
- Create: tests/dashboard-browser.cjs and update package.json test script when needed for actual admin/family coverage.
- Preserve: web role-page appearance and existing utility/terminal behavior.

**Interfaces:**
- Consumes: reconciled web/admin.html, web/family.html, web/login.html and app/dashboard.py from Task 1.
- Produces: actual-page browser fixtures and one conversion route normalization function used before both BODY_LIMITS and ADMISSION acquisition. /api/convert and /api/convert/pdf resolve to the same operation.

- [ ] **Step 1: Add focused failing tests against the reconciled handler and pages.**

Run the real HTTP handler with existing disposable HOME/test users. Patch convert_documents_batch with a deterministic fixture converter; do not invoke LibreOffice. Submit the same tiny multipart DOCX fixture to both route names as admin and family users. Assert equivalent success/output, unauthenticated denial, identical oversized-body rejection, one shared admission slot and slot release after exceptions. A request to /api/convert/pdf must no longer encounter default-zero BODY_LIMITS.

The alias set is exact:

```python
CONVERSION_PATHS = frozenset(("/api/convert", "/api/convert/pdf"))

def canonical_post_path(path):
    return "/api/convert" if path in CONVERSION_PATHS else path
```

- [ ] **Step 2: Run the focused tests and confirm the current source fails on the alias mismatch.**

```sh
python3 -m unittest discover -s tests -p test_dashboard.py -v
```

Record the specific expected mismatch rather than a generic import failure.

- [ ] **Step 3: Normalize before enforcement and dispatch.**

Apply canonical_post_path consistently to body limit selection, ADMISSION.acquire and conversion route dispatch. Avoid rewriting self.path globally because unrelated route/query semantics must remain unchanged. Both aliases share one semaphore keyed by /api/convert. Keep existing upload size, input count, multipart fields, error handling and response format.

- [ ] **Step 4: Exercise actual admin/family pages through browser actions.**

Serve maintained role templates in the fixture instead of extracting old embedded strings. Retain existing AGY/laptop tests. Add input-driven document selection/convert tests that observe request URL, multipart fields and resulting download. Add notes save/load and role-specific element checks using synthetic responses. Mock external services and user data; do not connect tests to real phone mutation APIs.

- [ ] **Step 5: Run focused and existing suites, then commit.**

```sh
python3 -m unittest discover -s tests -q
NODE_PATH=/home/astro/repos/mobile-server/node_modules CHROMIUM_PATH=/home/astro/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm test
git diff --check
git add app tests scripts/browser-fixture.py package.json package-lock.json
git commit -m "fix: align dashboard conversion routes and test served pages"
```


#### Task 2 additions from verified live-source defects


The approved design allows tested bug fixes before structural refactoring. Task 1 imported live source verbatim and revealed confirmed inherited defects. Task 2 must repair the defects and make the real maintained pages testable; passing the old fixture by weakening its assertions is not acceptance.

## Required changes and separate proof

1. Credential freshness: preserve the existing failing test_private_store_reload_and_disable assertion. CredentialStore must stat the credential file on each authorization lookup and cache JSON only while its file identity/version is unchanged. Keep constructor and invalidate() source compatibility. Test immediate account disable, admin-to-family downgrade, store deletion and atomic replacement, and verify unchanged files do not cause repeated JSON reads. Neither TTL waiting nor manual invalidate() calls from tests is an acceptable replacement for cross-process freshness. Use no real credentials.

2. Conversion contract: normalize /api/convert/pdf and /api/convert before BODY_LIMITS selection, Admission.acquire and conversion dispatch. Do not globally rewrite request paths or query handling. The aliases must share one conversion semaphore, identical existing size/input limits, role rules, multipart fields and actual response/download format. Test both URLs using tiny temporary fixtures and a mocked converter, including failure/slot release and equivalent admission behavior. Verify actual frontend multipart and response handling against backend, not only URL spellings.

3. Actual-page fixtures: scripts/browser-fixture.py must copy maintained web/admin.html rather than AST-extract embedded fallback HTML. Add family/login fixture coverage where needed. Existing browser assertions should check the current visible Wi-Fi host:port link text and preserved URL, not an old version's format. Add input-driven notes and document-conversion checks using synthetic responses. Keep AGY/laptop user actions covered. No browser connection to real mutating phone APIs.

4. Truthful memory: remove ESTIMATED_SERVICE_MEM/get_dynamic_service_rss fabrication. Real measured RSS stays numeric; unknown active RSS becomes null with explicit unavailable state. Active totals become unavailable/explicitly partial when any active value is unknown. Avoid treating null as 0 in web/admin.html. Preserve disabled/sleeping state and converter zero-idle behavior. Test measured/unknown/partial cases and browser rendering without changing appearance/layout. This is a semantic bug fix required by the approved truthful-telemetry objective.

## Scope boundaries

No module extraction, shared catalog/proxy refactor, service restarts, deployments, phone file changes, library operations or new runtime dependencies. Do not edit the historical source baseline manifest after bug fixes; generated candidate release hashes will be separate. Do not rewrite binary gateway without provenance proof. Embedded fallback removal/generation belongs to the rendering extraction stage; moving tests to actual pages does not claim the broken fallback is repaired.

Read /home/astro/.agents/skills/surgical-patch/SKILL.md and /home/astro/.agents/skills/systematic-debugging/SKILL.md. Read/apply /home/astro/.agents/skills/unslop/SKILL.md for interface work and run modern-web-guidance before client JS changes. Preserve all comments and current UI appearance. Record focused failing proof, targeted fixes and passing covering checks per defect, then run existing full suites once. Keep artifacts in ignored .superpowers/sdd and return concise evidence.

The existing account-disable test must pass without manual invalidation or TTL waits. Preserve JSON read caching by checking the file identity/version before reuse:

```python
version = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
```

A missing, replaced or downgraded user must not keep cached authorization. Prove unchanged content is not repeatedly parsed. Retain public constructor/invalidate compatibility.

For service telemetry, tests must establish these outcomes through get_services_report and actual admin rendering: measured RSS 12.5 remains numeric 12.5; unreadable RSS for a running service is null/unavailable; a missing active reading makes aggregate completeness explicit; disabled services do not acquire fake active memory. Remove jitter generation at its source.

Commit credential freshness and telemetry corrections separately from conversion/fixture changes when useful for review. Each commit must contain its covering test and pass its targeted proof. Run complete Python/browser checks once after all Task 2 fixes. Keep the baseline deployment manifest unchanged.

### Task 3: Make the source-to-deployment contract complete and verifiable

**Files:**
- Modify: scripts/install.sh and docs/OPERATIONS.md.
- Create: scripts/release-manifest.py, tests/test_release_manifest.py.
- Modify: config/deployment-files.json only where a reviewed mapping correction is required.

**Interfaces:**
- Consumes: Task 1 mapping and Task 2 actual templates.
- Produces: a deterministic SHA-256 manifest and an offline validation gate that checks all code/assets/helpers required by the release are present before any live write. Later deployment work consumes this manifest; this task does not activate a release.

- [ ] **Step 1: Add release completeness tests with temporary fixture files.**

A complete mapping passes. Removing admin.html, family.html, login.html, portal.css, limits.py or a mapped launcher fails with an explicit missing-source error. Hash mismatch fails validation. Reject paths escaping release root and reject secret/runtime file classes. Use deterministic sorted relative names.

- [ ] **Step 2: Implement manifest validation and asset installation mapping.**

Use hashlib.file_digest or streaming SHA-256 with pathlib, and JSON output. Keep the CLI local: generate/validate do not contact or change the phone. Make the installer validate a complete candidate before code writes. Include all three role pages and portal.css in the reviewed install set. Preserve runtime credentials, state and disabled flags. Do not make this legacy installer the final activation mechanism; coherent activation/rollback remains a separate reviewed subproject.

- [ ] **Step 3: Verify offline behavior and document restrictions.**

```sh
python3 -m unittest discover -s tests -p test_release_manifest.py -v
python3 -m unittest discover -s tests -q
bash -n scripts/install.sh
git diff --check
```

Document that direct deployment is withheld until atomic activation/rollback and live-drift refusal are verified. Source-to-manifest mapping must include actual served pages, helper modules and custom integration scripts.

- [ ] **Step 4: Commit and request subproject review.**

```sh
git add scripts config/deployment-files.json tests/test_release_manifest.py docs/OPERATIONS.md
git commit -m "build: validate complete Redmi server release artifacts"
```

Codex reviews the complete subproject against the approved spec before moving to structural extraction. No live activation is included in these three tasks.
