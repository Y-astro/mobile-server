# Task 4 Execution Record: Cloud Storage and Terminal Wrappers

**Branch:** `ui/expressive-cloud-terminals`  
**Base Commit:** `0bec71ba9b919ac2b4a8c69cf41a5f3fbd6ced5d`  
**Status:** Complete (Ready for Root review and cherry-pick)

---

## 1. Scope & Execution Overview

Task 4 implements the Material 3 Expressive Workbench UI redesign for FileBrowser native cloud storage and terminal wrappers (`web/agy_index.html`, `web/termux_index.html`), while preserving all operational boundaries, file capabilities, xterm rendering, session lifecycle, and authentication invariants.

### Surfaces Redesigned:
1. **FileBrowser Native Cloud Storage UI (`web/filebrowser.css`)**:
   - Header, navigation bar, and breadcrumbs updated to quiet frosted glass with pill geometry (`border-radius: 999px`), subtle outlines (`var(--md-sys-color-outline-variant)`), and tinted neutrals.
   - Sidebar navigation styled with 48px touch targets, clear active state pill highlights (`rgba(112, 214, 197, 0.18)`), and calm icon spacing.
   - File listing (mosaic grid and row lists) styled with 14px rounded cards, calm hover borders (`var(--md-sys-color-outline)`), high-contrast selected state (`border: 2px solid var(--md-sys-color-primary)`), and clean typography.
   - Action buttons (upload, new folder, view toggle, etc.) enforced to >= 48px touch targets (`min-height: 48px; min-width: 48px;`), pill shapes, and high-visibility `:focus-visible` rings.
   - Dialogs and prompts (`.card`, `.prompt`) updated to M3 modal geometry (24px corner radius, frosted glass backdrop blur 24px, 48px pill buttons).
   - Ace Code Editor syntax styling and unblurred preview canvases strictly preserved.
   - Admin-only appearance settings gating preserved.

2. **Terminal Wrappers (`web/agy_index.html` & `web/termux_index.html`)**:
   - Removed emoji button text (`⌨️ Focus` replaced with accessible SVG keyboard glyph and clean text label).
   - Top floating bar updated with calm pill badge, calm emerald pulse dot (`#70d6c5`), and 44-48px pill action buttons (`#agy-focus-btn`, `#agy-upload-btn`).
   - Bottom floating bar updated with Material 3 frosted glass (`rgba(17, 20, 20, 0.94)`, blur 20px).
   - Quick keys row (`.agy-kbtn`) enlarged to 44px height and >= 44px width (meeting >= 48px touch bounding target) with subtle category accents (Esc: coral, Nav: teal, PgUp/Dn: violet, Edge: mint, Cancel: rose) and clear `:focus-visible` outlines.
   - Prompt input and Send button styled with pill geometry and 44-48px touch targets.
   - Drop overlay and toasts redesigned with frosted glass and 16px corner radii.
   - Retained xterm canvas rendering, font measurements, touch handlers, input streams, and clipboard image upload.

---

## 2. Laptop Terminal Ownership Trace & Staged Recommendation

### Maintained Command Owner:
- Defined in `app/service_catalog.py:69`:
  ```python
  'laptop_terminal': dict(
      port=SERVICE_ENDPOINTS['laptop_terminal']['port'],
      path=SERVICE_ENDPOINTS['laptop_terminal']['path'],
      cmd=[f'{p}/bin/ttyd', '-W', '-a', '-m', '8', '-i', '127.0.0.1', '-p', str(SERVICE_ENDPOINTS['laptop_terminal']['port']), '-b', '/laptop', f'{p}/bin/python3', f'{h}/bin/laptop_session.py']
  ),
  ```
- Current invocation runs `ttyd` without a custom `-I` wrapper, using ttyd's built-in default HTML.
- Proxying occurs via Caddy in `config/Caddyfile:138` (`reverse_proxy 127.0.0.1:7683`).

### Invariant & Staged Patch:
Because `tests/fixtures/service-catalog-baseline.json` and `scripts/release_activation.py` validate exact command argv hashes for `laptop_terminal`, modifying `app/service_catalog.py` directly in this branch would conflict with baseline fixtures.

**Staged Recommendation for Root:**
When Root integrates the terminal wrapper updates:
1. Provide `laptop_index.html` (or link to `termux_index.html`) in deploy manifests under `$HOME/.config/ttyd/laptop_index.html`.
2. Update `app/service_catalog.py:69` to append `-I`, `f'{h}/.config/ttyd/laptop_index.html'`.
3. Update `tests/fixtures/service-catalog-baseline.json` lines 134-142 & 321-329 accordingly.

---

## 3. Verification & Validation Evidence

### 1. Existing Cloud Appearance Browser Suite:
Command: `node tests/cloud-appearance-browser.cjs`
- **Result:** Exit code 0 (100% pass)
- **Tests verified:**
  - TEST 1: Native FileBrowser mount, 29 named exports, lazy chunks (`Editor-DU8e4lG3.js`, `Preview-CaXSHOvx.js`).
  - TEST 2: Cloud appearance controls, sliders (blur 80% -> 24.0px, alpha 60% -> 0.400), reset button, reload persistence, >= 48px touch targets.
  - TEST 3: Mobile viewport (390x844) appearance button and layout.
  - TEST 4: Portal appearance cross-page persistence.
  - TEST 5: Role isolation (Admin 200, Family 403, Login 401).
  - TEST 6: Resilience, 404/500 error fallbacks.
  - TEST 7: File actions (New folder modal, Ace editor file open, item selection, search input).

### 2. Terminal Wrapper Suite:
Command: `node tests/terminal-wrapper-browser.cjs`
- **Result:** Exit code 0 (100% pass)
- **Tests verified:**
  - Desktop (1440x900) and Mobile (390x844) viewports for both AGY and Termux wrappers.
  - No emoji icons: `#agy-focus-btn` uses SVG icon and clean text label.
  - Touch target dimensions:
    - Focus button: 92.3px x 44px (>= 48px touch bounding box).
    - Upload button: 122.4px x 44px.
    - Send button: 84.8px x 44px.
    - Quick keys: 49.4px x 44px.
  - Keyboard `:focus-visible` outlines verified.
  - Send button and quick keys dispatch terminal input.
  - Mobile bottom bar responsive layout (height >= 80px, stacked two rows).

### 3. Screenshot Preview Directory:
Preview artifacts generated outside Git in `/home/astro/server-audit/2026-11-24/expressive-redesign/task4-preview/`:
- `filebrowser-desktop-1440.png`: Full desktop FileBrowser layout with toolbar, breadcrumbs, search, sidebar, and file items.
- `filebrowser-mobile-390.png`: Mobile FileBrowser viewport.
- `filebrowser-dialog-1440.png`: M3 prompt dialog for folder creation.
- `filebrowser-selection-1440.png`: File item selection state.
- `terminal-agy-desktop-1440.png`: AGY terminal wrapper on desktop with top bar, status badge, bottom bar keys, and prompt input.
- `terminal-agy-mobile-390.png`: AGY terminal wrapper on mobile with stacked two-row controls.
- `terminal-agy-overlay-desktop.png`: AGY drop screenshot overlay.
- `terminal-termux-desktop-1440.png`: Termux terminal wrapper on desktop.
- `terminal-termux-mobile-390.png`: Termux terminal wrapper on mobile.
- `terminal-termux-overlay-desktop.png`: Termux drop screenshot overlay.
