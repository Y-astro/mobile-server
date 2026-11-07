/**
 * =========================================================================
 * DASHBOARD UPGRADES & NOTES BROWSER ACCEPTANCE SUITE
 * Exercises:
 * 1. 3-column service grid at >=1280px (1280 & 1440) and 2-col at 1024px.
 * 2. Right sidebar order: Laptop Terminals + AGY + Quick Notes.
 * 3. Custom service reordering with touch/keyboard support, reload persistence,
 *    polling invariance, focus retention, and order reset.
 * 4. Mobile 390px 1-column layout with 0 horizontal overflow and >=48px targets.
 * 5. Family role boundaries: zero appearance or reordering controls.
 * 6. Matugen wallpaper-derived colour mode with 2 real palettes, portal + cloud
 *    mapping, and malformed/missing palette fallback.
 * =========================================================================
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const appearanceHelper = require('./fixtures/appearance-test-helper.cjs');

const rootDir = path.resolve(__dirname, '..');
const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
const themeTokensCss = fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8');
const appearanceCss = fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8');
const appearanceJs = fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8');
const portalPollingJs = fs.readFileSync(path.join(rootDir, 'web/portal-polling.js'), 'utf8');
const laptopJs = fs.readFileSync(path.join(rootDir, 'web/laptop.js'), 'utf8');

// Two real Matugen palettes generated from real wallpaper images
const paletteFrostedGlass = {
  surface: '#101417',
  surface_dim: '#101417',
  surface_bright: '#363a3e',
  surface_container_lowest: '#0b0f12',
  surface_container_low: '#181c20',
  surface_container: '#1c2024',
  surface_container_high: '#272a2e',
  surface_container_highest: '#313539',
  on_surface: '#e0e3e8',
  on_surface_variant: '#c2c7ce',
  outline: '#8c9198',
  outline_variant: '#42474d',
  primary: '#98ccf9',
  on_primary: '#003350',
  primary_container: '#034b71',
  on_primary_container: '#cce5ff',
  tertiary: '#d1bfe7',
  on_tertiary: '#372a4a',
  tertiary_container: '#4e4162',
  on_tertiary_container: '#ecdcff',
  error: '#ffb4ab',
  on_error: '#690005',
  error_container: '#93000a',
  on_error_container: '#ffdad6'
};

const paletteSaturnRings = {
  surface: '#181115',
  surface_dim: '#181115',
  surface_bright: '#3f363b',
  surface_container_lowest: '#120c10',
  surface_container_low: '#20191e',
  surface_container: '#251d22',
  surface_container_high: '#2f282c',
  surface_container_highest: '#3b3237',
  on_surface: '#ecdfdc',
  on_surface_variant: '#d5c2c6',
  outline: '#9e8c90',
  outline_variant: '#514347',
  primary: '#fbb0d8',
  on_primary: '#4c1e39',
  primary_container: '#653450',
  on_primary_container: '#ffd8e9',
  tertiary: '#f3ba9c',
  on_tertiary: '#482713',
  tertiary_container: '#623d26',
  on_tertiary_container: '#ffdbca',
  error: '#ffb4ab',
  on_error: '#690005',
  error_container: '#93000a',
  on_error_container: '#ffdad6'
};

const mockWallpapersData = {
  version: 1,
  default_id: 'db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc',
  wallpapers: [
    {
      id: 'db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc',
      name: '06. Frosted Glass.png',
      width: 2560,
      height: 1440,
      thumbnail_url: '/api/wallpapers/db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc/thumbnail.webp',
      image_url: '/api/wallpapers/db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc/display.webp',
      palette: paletteFrostedGlass
    },
    {
      id: 'a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890',
      name: 'saturn-rings.jpg',
      width: 1920,
      height: 1080,
      thumbnail_url: '/api/wallpapers/a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890/thumbnail.webp',
      image_url: '/api/wallpapers/a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890/display.webp',
      palette: paletteSaturnRings
    },
    {
      id: 'c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738',
      name: 'no-palette.jpg',
      width: 1920,
      height: 1080,
      thumbnail_url: '/api/wallpapers/c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738/thumbnail.webp',
      image_url: '/api/wallpapers/c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738/display.webp'
      // palette missing intentionally
    },
    {
      id: 'd1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890',
      name: 'partial-palette.jpg',
      width: 1920,
      height: 1080,
      thumbnail_url: '/api/wallpapers/d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890/thumbnail.webp',
      image_url: '/api/wallpapers/d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890/display.webp',
      palette: {
        primary: '#112233',
        surface: '#445566'
        // Missing other 22 roles intentionally to test partial palette fallback
      }
    }
  ]
};

const mockServices = [
  { id: 'filebrowser', name: 'FileBrowser', category: 'Storage', running: true, can_toggle: true, open_label: 'Open Cloud Drive', open_url: '/drive/' },
  { id: 'suwayomi', name: 'Suwayomi', category: 'Reader', running: true, can_toggle: true, open_label: 'Open Suwayomi', open_url: '/manga/', unread_updates: 2 },
  { id: 'vaultwarden', name: 'Vaultwarden', category: 'Security', running: false, can_toggle: true, open_label: 'Open Vault', open_url: 'javascript:openVaultHTTPS()' },
  { id: 'adguard', name: 'AdGuard Home', category: 'DNS', running: true, can_toggle: true, open_label: 'Open AdGuard', open_url: '/dns/' },
  { id: 'terminal', name: 'Web Terminal', category: 'Terminal', running: false, can_toggle: true, open_label: 'Open Terminal', open_url: '/term/' },
  { id: 'agy', name: 'AGY Web UI', category: 'AI Agent', running: true, can_toggle: true, open_label: 'Open AGY', open_url: '/agy/' }
];

(async () => {
  let currentServicesPayload = [...mockServices];

  const server = http.createServer((req, res) => {
    if (appearanceHelper.handleSharedAssetHttp(req, res, mockWallpapersData)) return;
    const parsed = new URL(req.url, 'http://127.0.0.1');
    const pathname = parsed.pathname;

    if (pathname === '/' || pathname === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(adminHtml);
    }
    if (pathname === '/family') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(familyHtml);
    }
    if (pathname === '/drive/' || pathname.startsWith('/drive/files')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Cloud Drive</title>
  <link rel="stylesheet" href="/theme-tokens.css">
  <link rel="stylesheet" href="/appearance.css">
  <script src="/appearance.js" defer></script>
</head>
<body>
  <div id="app">
    <header>
      <div class="logo">Cloud Drive</div>
    </header>
    <main>
      <p>FileBrowser Content</p>
    </main>
  </div>
</body>
</html>`);
    }
    if (pathname === '/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(portalCss);
    }
    if (pathname === '/theme-tokens.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(themeTokensCss);
    }
    if (pathname === '/appearance.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(appearanceCss);
    }
    if (pathname === '/appearance.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(appearanceJs);
    }
    if (pathname === '/static/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(portalPollingJs);
    }
    if (pathname === '/static/laptop.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(laptopJs);
    }
    if (pathname === '/api/auth-check-admin') {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
      return res.end('OK');
    }
    if (pathname === '/api/wallpapers') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify(mockWallpapersData));
    }
    if (pathname === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ tailscale_ip: '100.75.159.53', uptime: '3d', battery: { percentage: 95 } }));
    }
    if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ services: currentServicesPayload, active_memory_mb: 180 }));
    }
    if (pathname === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ enabled: false }));
    }
    if (pathname === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ sessions: [{ id: 'primary', is_primary: true }] }));
    }
    if (pathname === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ online: true, sessions: [] }));
    }
    if (pathname === '/api/notes') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ notes: 'Note' }));
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok' }));
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH,
    headless: true
  });

  try {
    const context = await browser.newContext({
      extraHTTPHeaders: { 'X-Forwarded-User': 'astro' }
    });
    const page = await context.newPage();
    page.on('console', msg => console.log('PAGE LOG [' + msg.type() + ']:', msg.text()));
    page.on('pageerror', err => console.log('PAGE ERROR:', err.message));
    page.on('requestfailed', req => console.log('REQ FAILED:', req.url(), req.failure()?.errorText));

    // -------------------------------------------------------------
    // TEST 1: DESKTOP 3-COLUMNS AND RIGHT SIDEBAR ORDER
    // -------------------------------------------------------------
    console.log('--- TEST 1: Desktop Layout (1440 & 1280) ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/admin`);
    await page.waitForSelector('#services-grid .m3-card');

    // Verify 3 columns grid template at 1440px
    const gridCols1440 = await page.evaluate(() => {
      const el = document.getElementById('services-grid');
      const style = window.getComputedStyle(el);
      return style.getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(gridCols1440, 3, 'Service grid must have exactly 3 columns at 1440px');

    // Verify 3 columns at 1280px
    await page.setViewportSize({ width: 1280, height: 900 });
    const gridCols1280 = await page.evaluate(() => {
      const el = document.getElementById('services-grid');
      const style = window.getComputedStyle(el);
      return style.getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(gridCols1280, 3, 'Service grid must have exactly 3 columns at 1280px');

    // Verify 2 columns at 1024px
    await page.setViewportSize({ width: 1024, height: 800 });
    const gridCols1024 = await page.evaluate(() => {
      const el = document.getElementById('services-grid');
      const style = window.getComputedStyle(el);
      return style.getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(gridCols1024, 2, 'Service grid must have 2 columns at 1024px');

    // Verify Right Column Section Order: Laptop Terminals FIRST, AGY SECOND, Quick Notes THIRD
    const rightColOrder = await page.evaluate(() => {
      const rightCol = document.querySelectorAll('.workstation-col')[1];
      const children = Array.from(rightCol.children).filter(el => el.tagName === 'SECTION' || el.classList.contains('m3-card'));
      return children.map(el => {
        const heading = el.querySelector('h2, h3');
        return heading ? heading.textContent.trim() : '';
      });
    });
    console.log('Right Column Section Order:', rightColOrder);
    assert(rightColOrder[0].toLowerCase().includes('laptop terminal'), 'Right column 1st section must be Laptop terminals');
    assert(rightColOrder[1].toLowerCase().includes('agy'), 'Right column 2nd section must be AGY');
    assert(rightColOrder[2].toLowerCase().includes('quick note'), 'Right column 3rd section must be Quick notes');

    // Verify Right Column Width & Laptop Header layout in narrow column (340-360px)
    const rightSidebarLayout = await page.evaluate(() => {
      const rightCol = document.querySelectorAll('.workstation-col')[1];
      const laptopSection = rightCol.querySelector('section[aria-labelledby="laptop-heading"]');
      const headerDiv = laptopSection.querySelector('div');
      const btn = laptopSection.querySelector('#laptop-new');
      const rightRect = rightCol.getBoundingClientRect();
      const cardRect = laptopSection.getBoundingClientRect();
      const btnRect = btn.getBoundingClientRect();
      return {
        colWidth: rightRect.width,
        flexDirection: window.getComputedStyle(headerDiv).flexDirection,
        hasOverflow: laptopSection.scrollWidth > laptopSection.clientWidth,
        btnFitsInside: btnRect.right <= cardRect.right + 2
      };
    });
    assert(rightSidebarLayout.colWidth >= 330 && rightSidebarLayout.colWidth <= 380, `Right column width must be ~360px at 1280px, got ${rightSidebarLayout.colWidth}`);
    assert.equal(rightSidebarLayout.flexDirection, 'column', 'Laptop card header must use column layout in narrow sidebar');
    assert.equal(rightSidebarLayout.hasOverflow, false, 'Laptop card must not have horizontal overflow');
    assert.equal(rightSidebarLayout.btnFitsInside, true, 'Laptop New button must fit inside card boundaries');
    const agyLabelFits = await page.locator('#agy-sessions-container').getByText('Primary AGY', { exact: true }).evaluate(el => {
      const range = document.createRange();
      range.selectNodeContents(el);
      return range.getClientRects().length === 1;
    });
    assert.equal(agyLabelFits, true, 'AGY session label must remain readable on one line in narrow sidebar');

    // -------------------------------------------------------------
    // TEST 2: CUSTOM SERVICE ORDERING & PERSISTENCE
    // -------------------------------------------------------------
    console.log('--- TEST 2: Custom Service Ordering & Persistence ---');
    await page.setViewportSize({ width: 1280, height: 900 });

    // Initial state: Arrange services toggle is aria-pressed="false"
    const arrangeToggle = page.locator('#btn-toggle-arrange');
    assert(await arrangeToggle.isVisible(), 'Arrange services toggle must be visible');
    assert.equal(await arrangeToggle.getAttribute('aria-pressed'), 'false', 'Arrange toggle initial aria-pressed must be false');

    // Ordinary card look: reorder buttons row and Reset button must be hidden
    const initialOrderRowsVisible = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.service-order-row'));
      return rows.some(r => window.getComputedStyle(r).display !== 'none');
    });
    assert.equal(initialOrderRowsVisible, false, 'Reorder buttons must be hidden by default (ordinary card look)');
    const initialResetVisible = await page.locator('#btn-reset-service-order').isVisible();
    assert.equal(initialResetVisible, false, 'Reset Order button must be hidden by default');

    // Click Arrange services toggle -> enters arrangement mode
    await arrangeToggle.click();
    assert.equal(await arrangeToggle.getAttribute('aria-pressed'), 'true', 'Arrange toggle must be aria-pressed="true"');
    assert(await page.locator('#btn-reset-service-order').isVisible(), 'Reset Order button must be visible when arranging');
    const arrangeRowsVisible = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.service-order-row'));
      return rows.every(r => window.getComputedStyle(r).display !== 'none');
    });
    assert.equal(arrangeRowsVisible, true, 'Reorder buttons must be visible when arranging');

    // Initial order: filebrowser, suwayomi, vaultwarden, adguard, terminal, agy
    let cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'filebrowser');
    assert.equal(cardIds[1], 'suwayomi');

    // Click Move Later on filebrowser (moves to index 1)
    const moveLaterBtn = page.locator('.m3-card[data-service-id="filebrowser"] button[data-action="move-later"]');
    await moveLaterBtn.click();

    // Check reordered DOM
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'suwayomi');
    assert.equal(cardIds[1], 'filebrowser');

    // Focus retention: verify focus stayed on the moved card's action button
    const activeDataAction = await page.evaluate(() => {
      return document.activeElement ? document.activeElement.getAttribute('data-action') : null;
    });
    assert.equal(activeDataAction, 'move-later', 'Active focus must remain on the reorder action button');

    // Keyboard navigation alternative: press Enter on the focused button to move it again
    await page.keyboard.press('Enter');
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'suwayomi');
    assert.equal(cardIds[1], 'vaultwarden');
    assert.equal(cardIds[2], 'filebrowser');

    // Polling invariance: trigger renderServices via periodic fetch, order and focus must NOT reset
    await page.evaluate(() => fetchServices());
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[2], 'filebrowser', 'Periodic polling must NOT reset custom service order');

    // Reload persistence: reload page and verify order is intact from localStorage
    await page.reload();
    await page.waitForSelector('#services-grid .m3-card');
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'suwayomi');
    assert.equal(cardIds[2], 'filebrowser', 'Reload must preserve custom service order');

    // Open arrangement mode to access Reset Order button
    await page.locator('#btn-toggle-arrange').click();
    const resetOrderBtn = page.locator('#btn-reset-service-order');
    assert(await resetOrderBtn.isVisible(), 'Reset Order button must be visible in arrange mode');
    await resetOrderBtn.click();
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'filebrowser', 'Reset order must restore default catalog order');

    // Corrupt storage invariance: corrupt localStorage must be gracefully ignored
    await page.evaluate(() => localStorage.setItem('mobile.services.order.v1', 'corrupt-not-json{'));
    await page.evaluate(() => fetchServices());
    cardIds = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    assert.equal(cardIds[0], 'filebrowser', 'Corrupt localStorage must be safely ignored');

    // -------------------------------------------------------------
    // TEST 3: MOBILE 390PX VIEWPORT & OVERFLOW
    // -------------------------------------------------------------
    console.log('--- TEST 3: Mobile 390px Viewport ---');
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileOverflow = await page.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(mobileOverflow, 'Mobile 390px must have no horizontal overflow');

    const mobileCols = await page.evaluate(() => {
      const el = document.getElementById('services-grid');
      return window.getComputedStyle(el).getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(mobileCols, 1, 'Mobile 390px service grid must have exactly 1 column');

    // Verify touch targets >= 48px
    const touchDeviations = await page.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('.service-order-btn, #btn-reset-service-order, .service-open-btn'));
      return elements.filter(el => {
        const rect = el.getBoundingClientRect();
        return rect.width < 48 || rect.height < 48;
      }).map(el => ({ tag: el.tagName, id: el.id, action: el.getAttribute('data-action'), w: rect.width, h: rect.height }));
    });
    assert.equal(touchDeviations.length, 0, 'All service controls must meet >=48px hit target');

    // -------------------------------------------------------------
    // TEST 4: FAMILY ROLE BOUNDARIES
    // -------------------------------------------------------------
    console.log('--- TEST 4: Family Role Boundary Checks ---');
    const familyContext = await browser.newContext({
      extraHTTPHeaders: { 'X-Forwarded-User': 'family' }
    });
    const familyPage = await familyContext.newPage();
    await familyPage.goto(`${baseUrl}/family`);
    await familyPage.waitForSelector('.family-services-grid');

    const familyAppearanceBtn = await familyPage.locator('#btn-appearance').count();
    assert.equal(familyAppearanceBtn, 0, 'Family template must not contain Appearance button');

    const familyReorderBtns = await familyPage.locator('.service-order-btn, #btn-reset-service-order').count();
    assert.equal(familyReorderBtns, 0, 'Family template must not contain service reordering controls');
    await familyPage.close();
    await familyContext.close();

    // -------------------------------------------------------------
    // TEST 5: MATUGEN WALLPAPER COLOUR MODES & FALLBACKS
    // -------------------------------------------------------------
    console.log('--- TEST 5: Matugen Wallpaper-Derived Colours ---');
    await page.goto(`${baseUrl}/admin`);
    await page.locator('#btn-appearance').waitFor({ state: 'visible' });
    await page.locator('#btn-appearance').click();
    await page.waitForSelector('#appearance-dialog[open], dialog#appearance-dialog:modal');

    // Verify Default M3 colors before enabling wallpaper mode
    let currentPrimary = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-primary').trim());
    assert.equal(currentPrimary, '#70d6c5', 'Default primary must be Teal #70d6c5');

    // Click "Wallpaper (Matugen)" segmented button
    const wallColorBtn = page.locator('#color-mode-wallpaper-btn');
    await wallColorBtn.click();

    // Verify Frosted Glass palette applied
    let themeColors = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return {
        primary: s.getPropertyValue('--md-sys-color-primary').trim(),
        tertiary: s.getPropertyValue('--md-sys-color-tertiary').trim(),
        surface: s.getPropertyValue('--md-sys-color-surface').trim()
      };
    });
    console.log('Applied Frosted Glass Palette:', themeColors);
    assert.equal(themeColors.primary, paletteFrostedGlass.primary);
    assert.equal(themeColors.tertiary, paletteFrostedGlass.tertiary);
    assert.equal(themeColors.surface, paletteFrostedGlass.surface);

    // Verify inline role was set and semantic positive default preserved
    const onPrimaryA = await page.evaluate(() => document.documentElement.style.getPropertyValue('--md-sys-color-on-primary'));
    assert.equal(onPrimaryA, paletteFrostedGlass.on_primary, 'Inline on-primary must match Frosted Glass palette');
    const positiveDefault = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-positive').trim());
    assert.equal(positiveDefault, '#70d6c5', 'Semantic positive default must be preserved');

    // Switch wallpaper to Saturn Rings
    const saturnThumb = page.locator('.appearance-thumb-btn[data-id="a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890"]');
    await saturnThumb.click();

    themeColors = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return {
        primary: s.getPropertyValue('--md-sys-color-primary').trim(),
        tertiary: s.getPropertyValue('--md-sys-color-tertiary').trim(),
        surface: s.getPropertyValue('--md-sys-color-surface').trim()
      };
    });
    console.log('Applied Saturn Rings Palette:', themeColors);
    assert.equal(themeColors.primary, paletteSaturnRings.primary);
    assert.equal(themeColors.tertiary, paletteSaturnRings.tertiary);
    assert.equal(themeColors.surface, paletteSaturnRings.surface);

    // Switch to wallpaper with partial/malformed palette B -> fallback to Teal & Gold
    const partialPalThumb = page.locator('.appearance-thumb-btn[data-id="d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890d1e2f3a4b5c67890"]');
    await partialPalThumb.click();

    themeColors = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return {
        primary: s.getPropertyValue('--md-sys-color-primary').trim(),
        tertiary: s.getPropertyValue('--md-sys-color-tertiary').trim(),
        surface: s.getPropertyValue('--md-sys-color-surface').trim()
      };
    });
    console.log('Partial Palette Fallback:', themeColors);
    assert.equal(themeColors.primary, '#70d6c5', 'Partial palette must fall back to Teal #70d6c5');
    assert.equal(themeColors.tertiary, '#e5c46c', 'Partial palette must fall back to Gold #e5c46c');

    // Regression check: verify no stale-state leak from Palette A (on-primary must be empty string)
    const staleRoleLeak = await page.evaluate(() => document.documentElement.style.getPropertyValue('--md-sys-color-on-primary'));
    assert.equal(staleRoleLeak, '', 'Fallback must reset all owned roles without leaking previous palette');

    // Switch to wallpaper without palette -> fallback to Teal & Gold
    const noPalThumb = page.locator('.appearance-thumb-btn[data-id="c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738"]');
    await noPalThumb.click();

    themeColors = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return {
        primary: s.getPropertyValue('--md-sys-color-primary').trim(),
        tertiary: s.getPropertyValue('--md-sys-color-tertiary').trim(),
        surface: s.getPropertyValue('--md-sys-color-surface').trim()
      };
    });
    console.log('Fallback to Default Palette:', themeColors);
    assert.equal(themeColors.primary, '#70d6c5', 'Missing palette must fall back to Teal #70d6c5');
    assert.equal(themeColors.tertiary, '#e5c46c', 'Missing palette must fall back to Gold #e5c46c');

    // Switch back to Teal & Gold manual mode explicitly
    const manualColorBtn = page.locator('#color-mode-manual-btn');
    await manualColorBtn.click();
    assert.equal(
      await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-primary').trim()),
      '#70d6c5'
    );
    // Verify all inline palette variables are removed
    const inlinePrimaryManual = await page.evaluate(() => document.documentElement.style.getPropertyValue('--md-sys-color-primary'));
    assert.equal(inlinePrimaryManual, '', 'Manual mode must remove inline palette CSS variables');

    // Reset button in dialog resets mode to manual
    await wallColorBtn.click();
    await page.locator('#appearance-reset-btn').click();
    assert.equal(
      await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-primary').trim()),
      '#70d6c5',
      'Reset button must reset theme colours to default'
    );

    // -------------------------------------------------------------
    // TEST 6: FOCUSED CLOUD STORAGE FIXTURE & PALETTES
    // -------------------------------------------------------------
    console.log('--- TEST 6: Focused Cloud Storage Fixture & Matugen Palettes ---');
    // Set wallpaper mode with Frosted Glass in localStorage
    await page.evaluate((frostedId) => {
      localStorage.setItem('mobile.appearance.v1', JSON.stringify({
        version: 1,
        wallpaperId: frostedId,
        blur: 15,
        translucency: 45,
        colorMode: 'wallpaper'
      }));
    }, mockWallpapersData.wallpapers[0].id);

    await page.goto(`${baseUrl}/drive/files`);
    await page.waitForSelector('#cloud-appearance-btn');
    await page.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-primary').trim() === '#98ccf9');

    // Verify Cloud actual CSS colors update with Frosted Glass palette
    const cloudThemeColors = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return {
        primary: s.getPropertyValue('--md-sys-color-primary').trim(),
        tertiary: s.getPropertyValue('--md-sys-color-tertiary').trim(),
        surface: s.getPropertyValue('--md-sys-color-surface').trim(),
        positive: s.getPropertyValue('--md-sys-color-positive').trim()
      };
    });
    console.log('Cloud Theme Colors (Matugen mode):', cloudThemeColors);
    assert.equal(cloudThemeColors.primary, paletteFrostedGlass.primary, 'Cloud must apply Matugen primary');
    assert.equal(cloudThemeColors.tertiary, paletteFrostedGlass.tertiary, 'Cloud must apply Matugen tertiary');
    assert.equal(cloudThemeColors.surface, paletteFrostedGlass.surface, 'Cloud must apply Matugen surface');
    assert.equal(cloudThemeColors.positive, '#70d6c5', 'Cloud must preserve semantic positive default');

    // Verify Cloud Appearance button has min 48px touch targets
    const cloudBtnBox = await page.locator('#cloud-appearance-btn').boundingBox();
    assert(cloudBtnBox.width >= 48 && cloudBtnBox.height >= 48, 'Cloud appearance button must meet 48px min touch targets');

    // Family role context check in Cloud: ensure #cloud-appearance-btn is hidden
    const familyCloudContext = await browser.newContext({
      extraHTTPHeaders: { 'X-Forwarded-User': 'family' }
    });
    const familyCloudPage = await familyCloudContext.newPage();
    await familyCloudPage.goto(`${baseUrl}/drive/files`);
    await familyCloudPage.waitForTimeout(200);
    const familyCloudBtnCount = await familyCloudPage.locator('#cloud-appearance-btn').count();
    assert.equal(familyCloudBtnCount, 0, 'Cloud Appearance button must be hidden for family role');
    await familyCloudPage.close();
    await familyCloudContext.close();

    console.log('ALL DASHBOARD-NOTES ACCEPTANCE CHECKS PASSED');
    await page.close();
  } finally {
    await browser.close();
    server.close();
  }
})();
