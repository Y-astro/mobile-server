# Expressive Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the supplied Expressive Workbench design to both account roles and all included service interfaces while preserving current server behavior.

**Architecture:** Retain the current HTML/CSS/JavaScript portal and Python service/API layer. Reconcile newer live source first, then migrate shared visual components and service-specific surfaces in independently reviewed stages. Native service adapters are a separate subproject with a concrete asset plan before implementation.

**Tech Stack:** Existing vanilla HTML/CSS/JavaScript, Matugen appearance tokens, Python standard library, Playwright, and the reviewed release manifest/activation tools.

**Spec:** `docs/superpowers/specs/2026-11-24-expressive-workbench-design.md`

## Global Constraints

- Exclude standalone Suwayomi, Wealthfolio, and Navidrome UIs; include their dashboard cards.
- Match the rendered reference without obsolete navigation rails, prototype data, palette presets, or embedded prototype wallpaper.
- Keep appearance customization admin-only and preserve all existing role/data boundaries.
- Preserve current service enablement, current live Repertoire features, session semantics, notes, queues, and workbooks.
- Make source changes in the isolated worktree, commit before publication, and deploy against complete reviewed hashes and fresh live preconditions.

## Review Focus

- Long service names and 390px widths must wrap without clipping actions or creating document overflow.
- Polling during a pending toggle or reorder must not overwrite user intent or re-enable a service.
- Family must not acquire administrator remote tools, appearance customization, or private data.
- New live Repertoire routes/assets must remain deployable and available to currently authorized users.
- Browser cache, omitted service assets, or an unmatched live file must not create a mixed or destructive release.

## Task 1: Reconcile the live Repertoire feature

**Files:** `app/dashboard.py`, new `app/repertoire.py`, `web/admin.html`, `web/family.html`, new `web/repertoire.html`, `web/repertoire.css`, `web/repertoire.js`, `scripts/release-manifest.py`, focused `tests/test_release_manifest.py` and `tests/test_repertoire.py` coverage.

**Interfaces:** Preserve `dispatch_repertoire(handler, user)` and `dispatch_repertoire_post(handler, user)` from the live module. Preserve `/repertoire/` and its actual asset/API routes. Python helper ownership is already discovered dynamically from flat `app/*.py`; add explicit ownership only for the three new static assets.

- [ ] Verify captured baseline hashes against `source-hash-manifest.json` and inspect the live-to-source diff. Do not copy runtime data.
- [ ] Add focused checks that the release owns all four Repertoire files, anonymous requests remain blocked, and role-specific upload behavior matches the existing implementation.
- [ ] Copy the verified live source without redesigning it. The dashboard integration should be limited to its existing import, GET/POST dispatch hooks, and role-template links.
- [ ] Run relevant Repertoire/release/template/dashboard tests, compare source bytes or hunks with the capture, and commit this baseline separately.

## Task 2: Shared visual system and account workbenches

**Files:** `web/portal.css`, `web/theme-tokens.css`, `web/admin.html`, `web/family.html`, `web/portal-polling.js` only if the existing binding contract needs a small change; browser acceptance in `tests/material3-browser.cjs`, `tests/sidebar-service-controls-browser.cjs`, and a focused Expressive-layout suite.

**Interfaces:** Preserve the existing telemetry IDs, `services-grid`, `data-service-id`, `data-action`, session containers, notes controls, and current public action functions. Reuse existing `/api/stats`, `/api/services`, toggle, notes, AGY and laptop endpoints. No new backend API is required for this visual stage.

- [ ] Establish the existing relevant browser baseline and add failing assertions for the new header, instrument grouping, desktop service/tools split, responsive columns, service filters, and role-specific controls.
- [ ] Implement the reference surfaces, SVG glyph treatment, typography, grid geometry, and Matugen token mappings in shared styles.
- [ ] Adapt both account templates, preserving all current actions and role differences. Keep one AGY panel with multiple sessions, one laptop panel, and the permitted notes interface.
- [ ] Integrate filtering/column preferences and accessible arrangement with stored order and polling; do not replace live behavior with prototype mocks.
- [ ] Run layout, toggle error/403, persistence, keyboard, mobile overflow, and account-boundary checks on the actual templates. Capture representative admin/family screenshots, review, and commit.

## Task 3: Login, dialogs, and portal-owned service pages

**Files:** `web/login.html`, `web/appearance.css`, `web/appearance.js`, `web/antra.html`, `web/antra.css`, `web/antra.js` only where interaction markup changes, `web/repertoire.html`, `web/repertoire.css`, `web/repertoire.js` only where needed, plus existing shared component CSS.

**Interfaces:** Existing appearance APIs/storage keys, converter controls, notes status, downloader queue APIs, and Repertoire APIs remain unchanged.

- [ ] Add or update focused actual-page browser assertions for consistent toolbars, dialogs, fields, empty/error/loading states, and accessible focus behavior.
- [ ] Apply the shared reference component system to login, appearance, converter, notes, Antra, and Repertoire without altering service/data semantics.
- [ ] Run appearance, notes, converter, downloader, Repertoire and role-boundary suites with isolated data, inspect screenshots, and commit.

## Task 4: Cloud Storage and terminal wrapper surfaces

**Files:** `web/filebrowser.css`, `web/filebrowser.js`, `web/filebrowser-entry.js`, `web/agy_index.html`, `web/termux_index.html`; change `config/Caddyfile` only if the verified current static hook requires it. Preserve terminal protocol/bundle code and service authentication.

- [ ] Add failing checks for the reference surface/control treatment across FileBrowser listing, toolbar, settings/dialogs and both role contexts, plus terminal wrapper chrome.
- [ ] Apply the design through existing native CSS/branding and wrapper hooks. Preserve file operations, uploads/downloads, account visibility, native appearance role restrictions, and terminal/session transport.
- [ ] Run Cloud appearance and session/browser regressions, inspect accessible active pages or isolated wrapper fixtures, and commit.

## Task 5: Native AdGuard, Syncthing, and Vaultwarden interfaces

- [ ] Review `service-ui-scope.md` from the runtime inventory and write a separate implementation plan with exact supported asset hooks, owned source files, route mappings, isolated preview tests, version/hash preconditions, and recovery steps.
- [ ] Implement and review that subproject using native branding or narrowly scoped static assets. Do not introduce a new daemon or generic proxy unless evidence proves it necessary.
- [ ] Verify disabled services using isolated installed-asset previews. Preserve disabled intent throughout. Record concrete limits instead of silently skipping a service.

## Task 6: Integrated verification and controlled publication

**Files:** Release ownership/fixtures as needed, committed source assets, deployment evidence outside the repository, and `docs/architecture/` updates reflecting the final owned assets.

- [ ] Check the complete changed-surface inventory against the specification and run focused Python contracts plus applicable actual-page Playwright suites. Repeat only failures or checks affected by subsequent edits.
- [ ] Inspect final differences for stale prototype data, hardcoded theme colours, lost features, role leaks, and unowned assets. Verify the three excluded application UIs remain untouched.
- [ ] Commit the reviewed source, build a complete candidate manifest, capture fresh live preconditions, and verify a recovery path. Refuse unexplained drift.
- [ ] Activate only affected assets/services through the reviewed mechanism; preserve SSH, disabled flags, user data, and excluded application runtimes.
- [ ] Verify published admin/family pages and included service interfaces, HTTP asset cache behavior, action contracts and service health. Refresh the local/server architecture map and report validation limits.

## Execution record

The user-approved reference and explicit execution request authorize this work. Process details above are implementation decisions, not additional user approvals. Individual task plans may be refined from verified runtime evidence before that task starts; preserve this scope and the recorded acceptance conditions.
