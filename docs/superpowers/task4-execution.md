# Task 4 Execution Record: Cloud Storage and Terminal Wrappers

**Branch:** `ui/expressive-cloud-terminals`  
**Base Commit:** `0bec71ba9b919ac2b4a8c69cf41a5f3fbd6ced5d`  
**Status:** Complete (Refined and verified against Root findings; ready for cherry-pick)

---

## 1. Scope & Execution Overview

Task 4 implements the Material 3 Expressive Workbench UI redesign for FileBrowser native cloud storage and terminal wrappers (`web/agy_index.html`, `web/termux_index.html`), while preserving all operational boundaries, file capabilities, xterm rendering, session lifecycle, and authentication invariants.

### Surfaces Redesigned & Refinements:
1. **FileBrowser Native Cloud Storage UI (`web/filebrowser.css`)**:
   - **Header & Desktop Action Toolbar**: Scoped desktop header `#dropdown` to `position: static; display: flex; flex-direction: row; height: 48px; gap: 4px;` within the 64px header. All actions ("Switch view", "Download", "Upload", "Info", "Select multiple", "Appearance") remain in a single horizontal row within header bounds (`top: 7.5px, bottom: 55.5px`). Zero vertical stacking outside header.
   - **Mobile Header**: Fixed popup dropdown for `#dropdown` triggered by `#more`. Both `#more` and `#cloud-appearance-btn` meet >= 48px touch targets in both dimensions.
   - **M3 Matugen-Only Tokens**: Replaced all hardcoded `rgba(112, 214, 197, ...)` fills with `color-mix(in srgb, var(--md-sys-color-primary) ...)` and `--md-sys-color-*` tokens.
   - **Touch Targets**: All action buttons, cards, list items, and modal controls enforce `min-height: 48px !important; min-width: 48px !important;`.
   - **Dialogs & Fallbacks**: Prompts (`.card`, `.prompt`) updated to M3 modal geometry (24px corner radius, frosted glass backdrop blur 24px, 48px pill buttons). Added `@media (prefers-reduced-motion)` and `@media (prefers-reduced-transparency)` fallbacks.
   - **Preserved Boundaries**: Ace Code Editor syntax highlighting, unblurred preview canvases, Vue bindings, and admin-only appearance gating strictly preserved.

2. **Terminal Wrappers (`web/agy_index.html` & `web/termux_index.html`)**:
   - **Portal Token Integration**: Linked same-origin portal resources (`<link rel="stylesheet" href="/theme-tokens.css">`, `<link rel="stylesheet" href="/appearance.css">`, `<script src="/appearance.js"></script>`). Chrome styling derives dynamically from Matugen tokens via `color-mix`.
   - **Quiet Chrome & Static Dot**: Removed rainbow quick key accents and pulsing glow. `.agy-pulse-dot` is static and styled with semantic positive token `var(--md-sys-color-positive, #70d6c5)`. No false connection or health claims.
   - **Touch Targets**: Verified true >= 48px in BOTH dimensions across all controls (`#agy-focus-btn`: 97.3x48, `#agy-upload-btn`: 127.4x48, `#agy-send-btn`: 88.8x48, `.agy-kbtn`: 53.4x48, `.agy-badge`: 149.4x48).
   - **Terminal Container Viewport Bounds**: Sized `#terminal-container` with `top: 64px !important; height: auto !important; bottom: 76px !important;` on desktop and `bottom: 144px !important;` on mobile. Floating chrome bars never overlay terminal output lines, cursor, or prompts.
   - **Laptop Terminal Mode (`web/termux_index.html`)**: Detects `window.location.pathname.startsWith('/laptop')` at runtime, setting title to "Laptop Terminal", badge text to "Laptop Session", hiding `#agy-upload-btn`, and removing phone-only drop/upload elements.
   - **Transport Invariant**: Bundled ttyd script byte payload preserved byte-identical to base commit with SHA256 `42bb3a80a3cb5207e88f18b8dfe603a08f535196de1145fb65277dbbcabf0ac2`.

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
1. Provide `laptop_index.html` (or link to `termux_index.html`) in deploy manifests under `$HOME/.config/ttyd/laptop_index.html` or `$HOME/.config/ttyd/termux_index.html`.
2. Update `app/service_catalog.py:69` to append `-I`, `f'{h}/.config/ttyd/termux_index.html'`.
3. Update `tests/fixtures/service-catalog-baseline.json` lines 134-142 & 321-329 accordingly.

---

## 3. Verification & Validation Evidence

### 1. Existing Cloud Appearance Browser Suite:
Command: `node tests/cloud-appearance-browser.cjs`
- **Result:** Exit code 0 (100% pass)
- **Tests verified:**
  - TEST 1: Native FileBrowser mount, 29 named exports, lazy chunks (`Editor-DU8e4lG3.js`, `Preview-CaXSHOvx.js`). Desktop header action group bounding box: `#dropdown` height 48px inside 64px header, all actions aligned horizontally (`top: 7.5px, bottom: 55.5px`), zero vertical stacking (`max(top) - min(top) <= 2px`), >= 48px in both dimensions.
  - TEST 2: Cloud appearance controls, sliders (blur 80% -> 24.0px, alpha 60% -> 0.400), reset button, reload persistence, >= 48px touch targets.
  - TEST 3: Mobile viewport (390x844) appearance button and `#more` button layout (both visible and >= 48px in both dimensions).
  - TEST 4: Portal appearance cross-page persistence.
  - TEST 5: Role isolation (Admin 200, Family 403, Login 401).
  - TEST 6: Resilience, 404/500 error fallbacks, `prefers-reduced-transparency`.
  - TEST 7: File actions (New folder modal, Ace editor file open, item selection, search input).

### 2. Terminal Wrapper Suite:
Command: `node tests/terminal-wrapper-browser.cjs`
- **Result:** Exit code 0 (100% pass)
- **Tests verified:**
  - Bundled script SHA256 verified as exact `42bb3a80a3cb5207e88f18b8dfe603a08f535196de1145fb65277dbbcabf0ac2` for both wrappers.
  - Desktop (1440x900), Mobile (390x844), and Short (640x420) viewports for both AGY and Termux wrappers.
  - No emoji icons: `#agy-focus-btn` uses SVG icon and clean text label.
  - True >= 48px touch targets in BOTH dimensions:
    - Focus button: 97.3px x 48.0px.
    - Upload button: 127.4px x 48.0px.
    - Send button: 88.8px x 48.0px.
    - Quick keys: 53.4px x 48.0px.
    - Badge: 149.4px x 48.0px.
  - Static status dot without animation or glow (`dotHasAnimation: false`).
  - Terminal container viewport bounds:
    - Desktop: top 64px (reserves top bar), bottom 824px (reserves 76px bottom clearance).
    - Mobile: top 64px, bottom 700px (reserves 144px bottom clearance for 2-row controls).
    - Short: top 64px, bottom 344px (usable terminal height 280px).
  - Dynamic Matugen palette color switching: computed color changes dynamically from `rgb(112, 214, 197)` to `rgb(229, 196, 108)`.
  - Keyboard `:focus-visible` outlines verified.
  - Send button and quick keys dispatch terminal input.
  - Laptop mode (`/laptop/` URL): title updated to "Laptop Terminal", badge text "Laptop Session", upload button hidden, drop overlay removed.

### 3. Screenshot Preview Directory:
Portable preview artifacts generated in `EXPRESSIVE_PREVIEW_DIR` (or OS temp):
- `filebrowser-desktop-1440.png`: Full desktop FileBrowser layout with horizontal header action row within 64px header bounds.
- `filebrowser-mobile-390.png`: Mobile FileBrowser viewport.
- `filebrowser-dialog-1440.png`: M3 prompt dialog for folder creation.
- `filebrowser-selection-1440.png`: File item selection state.
- `terminal-agy-desktop-1440.png`: AGY terminal wrapper on desktop with top bar, status badge, bottom bar keys, and prompt input.
- `terminal-agy-mobile-390.png`: AGY terminal wrapper on mobile with stacked two-row controls and clearance.
- `terminal-agy-overlay-desktop.png`: AGY drop screenshot overlay.
- `terminal-termux-desktop-1440.png`: Termux terminal wrapper on desktop.
- `terminal-termux-mobile-390.png`: Termux terminal wrapper on mobile.
- `terminal-termux-overlay-desktop.png`: Termux drop screenshot overlay.
