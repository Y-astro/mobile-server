# Material 3 Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved dark Material 3 visual design across administrator, family, and login pages, dynamic service/session cards, and conversion dialogs while preserving all server runtime contracts, polling behavior, and test suites.

**Architecture:** Replace legacy Tailwind and OS Atlas CSS overrides with an authentic dark Material 3 design token system (`--md-sys-color-*`), semantic `.m3-*` component styling, 4px grid rhythm, system sans typography, tabular figures, and 48px interactive touch targets. Refactor HTML templates (`admin.html`, `family.html`, `login.html`) and dynamic DOM generators (`laptop.js`, `renderServices()`) to use the M3 component vocabulary with compatibility hooks (`.switch`, `.slider`, `.badge-positive`, `.hidden`).

**Tech Stack:** Native HTML5, CSS3 Custom Properties (Material 3 Dark tokens), vanilla ECMAScript (same-origin fetch, AbortController lifecycle), Node.js Playwright test runner.

**Spec:** `docs/superpowers/specs/2026-11-04-material3-portal-design.md`

## Global Constraints

- Full dark Material 3 only: surface `#111414`, surface-container `#1d2223`, on-surface `#e1e3e3`, on-surface-variant `#bfc8c7`, primary `#70d6c5`, on-primary `#003734`, primary-container `#004f4a`, on-primary-container `#8bf2e1`, tertiary `#e5c46c`, on-tertiary `#3c2f00`, tertiary-container `#574400`, on-tertiary-container `#ffe08b`, error `#ffb4ab`/`#690005`, outlines `#899392`/`#3f4948`.
- No new framework, Material npm package, external font/CDN dependency, build tool, or light theme toggle.
- All interactive targets must have at least 48px hit areas (width/height or touch padding).
- Strict 4px spacing rhythm, 14–16px readable body text, 12px minimum status/meta text, sentence-case labels, no Georgia/Times serif telemetry, no decorative gradients, no glows, no backdrop blur.
- Preserve all existing IDs, form names, API endpoints, role gates, polling lifecycles, and test hooks (`.switch`, `.slider`, `.badge-positive`, `.hidden`).
- Allowed production files: `web/portal.css`, `web/admin.html`, `web/family.html`, `web/login.html`, `web/laptop.js`.
- Allowed validation/docs files: `tests/material3-browser.cjs`, `package.json`, `docs/SOURCE-OWNERSHIP.md`, `docs/superpowers/plans/2026-11-04-material3-portal.md`, `.superpowers/sdd/material3-implementation-report.md`.
- Verified release manifest must contain exactly 42 entries.

---

### Task 1: Material 3 Design Token Architecture & CSS Component Library (`web/portal.css`)

**Files:**
- Modify: `web/portal.css`

**Interfaces:**
- Consumes: M3 color tokens and shape specs from `docs/superpowers/specs/2026-11-04-material3-portal-design.md`.
- Produces: Complete `:root` token variables, typography, baseline layout utilities, and `.m3-*` component classes (surfaces, cards, buttons, switches, inputs, dialogs, progress bars) plus compatibility aliases.

- [x] **Step 1: Replace legacy Tailwind and 162 `!important` overrides with M3 design tokens and base reset**
  - Define `:root` with verified color ramp (`--md-sys-color-surface: #111414`, container `#1d2223`, primary `#70d6c5`, on-primary `#003734`, primary-container `#004f4a`, tertiary `#e5c46c`, outlines `#899392`/`#3f4948`, error `#ffb4ab`).
  - Define shape tokens (`--md-shape-corner-medium: 12px`, `--md-shape-corner-large: 16px`, `--md-shape-corner-full: 9999px`).
  - Set base font family to `Roboto, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", sans-serif`.
  - Set headings `h1, h2, h3` to sans-serif with sentence-case hierarchy (no serif headings).
  - Set tabular numerals for telemetry and metrics (`font-variant-numeric: tabular-nums`).
  - Set visible focus indicators: 2px solid primary outline with 2px offset on `:focus-visible`.

- [x] **Step 2: Author clean layout utilities and responsive grid rules**
  - Implement clean, standard flex and grid utilities (`flex`, `flex-col`, `items-center`, `justify-between`, `grid`, `gap-*`, `space-y-*`, `p-*`, `m-*`, `hidden`).
  - Implement responsive column breakpoints (desktop 2-column split, mobile 360–412px single column with no horizontal overflow).
  - Provide sizing constraints (`max-w-*`, `w-full`) preventing layout blowouts.

- [x] **Step 3: Implement M3 component classes and semantic compatibility hooks**
  - `.m3-card` / `.golden-glass-card`: flat container surface (`#1d2223`), outline (`#3f4948`), 12px radius, no glows or blur.
  - `.m3-btn`, `.m3-btn-filled` / `.gold-gradient-btn`: solid teal `#70d6c5` with `#003734` text, minimum 48px hit area, 20px radius.
  - `.m3-btn-tonal`: primary-container `#004f4a` with `#8bf2e1` text, minimum 48px hit area.
  - `.m3-btn-outlined` / `.btn-secondary`: outline border, teal text, minimum 48px hit area.
  - `.m3-switch` / `.switch` & `.slider`: authentic M3 toggle switch with 48px touch target, preserving existing DOM input/span structure.
  - `.m3-text-field` / `.gold-input`: container highest surface (`#323738`), outline border, minimum 48px touch height, accessible focus state.
  - `.m3-badge`, `.badge-positive`, `.badge-neutral`: tonal status chips with 12px minimum font size.
  - `.progress-track` & `.progress-fill`: solid teal fill bar preserving inline `style="width: X%"` data bindings.
  - `.m3-dialog` / `#converter-modal`: modal backdrop and centered dialog container with 16px radius and clean dismiss actions.

---

### Task 2: Refactor HTML Templates & Client Extension Scripts

**Files:**
- Modify: `web/login.html`
- Modify: `web/family.html`
- Modify: `web/admin.html`
- Modify: `web/laptop.js`

**Interfaces:**
- Consumes: M3 classes and tokens from `web/portal.css`.
- Produces: Clean, accessible markup across admin, family, login, and dynamic DOM renderers, preserving all IDs, forms, routes, and onclick handlers.

- [x] **Step 1: Modernize `web/login.html`**
  - Remove `.bg-wallpaper` blur/gradient clutter and `.golden-glass` styling.
  - Replace form card with `.m3-card` / `.m3-dialog` centered surface.
  - Update `#username`, `#password`, and `#submit-btn` to M3 text field and filled button classes with 48px touch targets.
  - Preserve `#login-form`, input autocomplete attributes, `#error-msg`, and submission script intact.

- [x] **Step 2: Modernize `web/family.html`**
  - Update header, welcome banner, and 5 family service cards (Cloud Storage, Family Photos, Music Jukebox, Document Converter, Wealthfolio) to M3 card and button styles.
  - Update Quick Notes card and formatting buttons (`insertBulletList`, `insertNumberedList`) with 48px hit targets.
  - Update Document Converter modal markup to M3 dialog layout with 48px action buttons and close trigger.
  - Add Escape key listener to close `#converter-modal` and restore focus.
  - Preserve all functional IDs, notes auto-save debounce, and conversion file list/download contracts.

- [x] **Step 3: Modernize `web/admin.html`**
  - Update header, hardware telemetry cards (Battery, RAM, Storage, CPU), and Live Network Monitor to M3 surfaces and sans typography.
  - Update Tor SOCKS proxy section, AGY section, laptop section, and converter modal to M3 components.
  - Update `renderServices()` template generator to produce M3 service cards with 48px buttons and switches while preserving `.badge-positive`, `.switch`, and `.slider` compatibility hooks.
  - Add Escape key listener to close converter modal and restore focus.
  - Ensure all metrics and percentage widths (`#battery-bar`, `#ram-bar`, `#storage-bar`) bind seamlessly.

- [x] **Step 4: Update `web/laptop.js` dynamic controls**
  - Update dynamically generated terminal session rows to M3 list item containers.
  - Style Reconnect link and Close button as coherent M3 buttons with 48px touch targets.
  - Preserve exact event handler logic, accessible label `Close terminal ${session.id.slice(0,8)}`, and confirmation dialog.

---

### Task 3: Focused Material 3 Visual and Acceptance Test Suite (`tests/material3-browser.cjs`)

**Files:**
- Create: `tests/material3-browser.cjs`
- Modify: `package.json`
- Modify: `docs/SOURCE-OWNERSHIP.md`

**Interfaces:**
- Consumes: Rendered pages served with synthetic fixtures via local Playwright harness.
- Produces: Automated verification of computed styles, viewport overflow, touch target sizes, contrast, keyboard focus, dialog dismissal, and service grid states.

- [x] **Step 1: Create `tests/material3-browser.cjs`**
  - Launch Chromium using `CHROMIUM_PATH` and isolated loopback synthetic fixtures.
  - Test login page: computed surface color, input 48px target, submit button 48px target, valid submission.
  - Test admin page at desktop (1280x900) and mobile (412x915 and 360x800):
    - Assert no horizontal overflow (`scrollWidth <= clientWidth`).
    - Assert computed `--md-sys-color-surface` is `#111414` and `--md-sys-color-primary` is `#70d6c5`.
    - Assert headings and stat numerals compute to sans-serif fonts (not Georgia/Times).
    - Assert all interactive buttons, links, inputs, and switches have `height >= 48px` and `width >= 48px` (or 48px touch bounding box).
    - Assert complete service grid renders all states: active (with memory), inactive, ondemand/disabled, unread badge.
    - Assert keyboard focus visibility on controls.
    - Assert converter modal opens, closes on Escape key press, and restores focus.
    - Assert notes formatting buttons and autosave function properly.
    - Assert conversion single file and batch zip download contracts work identically.
  - Test family page at mobile (412x915):
    - Assert no horizontal overflow.
    - Assert 5 service cards render with M3 styling and 48px action buttons.
    - Assert converter modal and notes function properly.

- [x] **Step 2: Wire into `package.json` and update `docs/SOURCE-OWNERSHIP.md`**
  - Add `node tests/material3-browser.cjs` to `package.json` `scripts.test`.
  - Update `docs/SOURCE-OWNERSHIP.md` to document M3 frontend presentation ownership and test coverage.

- [x] **Step 3: Run full verification suite**
  - Run focused test: `CHROMIUM_PATH=... NODE_PATH=... node tests/material3-browser.cjs`.
  - Run full `npm test` once.
  - Run full Python test suite: `python3 -m unittest discover tests`.
  - Generate and validate 42-entry release manifest: `python3 scripts/release-manifest.py generate --output .work/release-manifest.json && python3 scripts/release-manifest.py validate --manifest .work/release-manifest.json`.
  - Check git diff cleanliness: `git diff --check`.

---

### Task 4: Visual Screenshots, Unslop Audit Verification & Implementation Report

**Files:**
- Create: `.superpowers/sdd/material3-implementation-report.md`
- Screenshots generated in: `.superpowers/sdd/` (desktop admin, mobile admin, mobile family, mobile login, open dialog)

**Interfaces:**
- Consumes: Test and capture artifacts.
- Produces: Complete implementation report with exact SHAs, file sizes, test counts, contrast ratios, and visual checklist audit.

- [x] **Step 1: Capture representative screenshots**
  - Desktop Admin (1280x900) with populated service grid, truthful telemetry, and active/disabled states.
  - Mobile Admin (412x915 Redmi K20 Pro) showing responsive workstation layout.
  - Mobile Family (412x915) showing 5 service cards and notes.
  - Mobile Login (412x915) showing clean M3 card and input fields.
  - Open Converter Dialog showing drag-and-drop zone and 48px buttons.

- [x] **Step 2: Verify against Unslop design checklist**
  - No purple/gold hue gradients on surfaces, no button glows, no glass blur.
  - No serif telemetry numerals, no heavy tracked uppercase labels.
  - All interactive hit targets >= 48px.
  - Calm dark OLED surfaces with teal primary actions and restrained gold tertiary accents.

- [x] **Step 3: Record asset byte size comparisons and author implementation report**
  - Measure `web/portal.css` byte size before and after.
  - Author `.superpowers/sdd/material3-implementation-report.md` detailing changes, tests, screenshots, and verifications.
  - Commit implementation and plan.
