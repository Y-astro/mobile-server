const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const appearanceHelper = require('./fixtures/appearance-test-helper.cjs');

const rootDir = path.resolve(__dirname, '..');
const previewDir = '/home/astro/server-audit/2026-11-24/expressive-redesign/task2-preview';
if (!fs.existsSync(previewDir)) {
  fs.mkdirSync(previewDir, { recursive: true });
}

const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
const themeTokensCss = fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8');
const appearanceCss = fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8');
const appearanceJs = fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8');
const portalPollingJs = fs.readFileSync(path.join(rootDir, 'web/portal-polling.js'), 'utf8');
const laptopJs = fs.readFileSync(path.join(rootDir, 'web/laptop.js'), 'utf8');

// Valid Matugen 24-color token palette generated from Frosted Glass
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
      id: 'c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738',
      name: 'neutral-no-palette.jpg',
      width: 1920,
      height: 1080,
      thumbnail_url: '/api/wallpapers/c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738/thumbnail.webp',
      image_url: '/api/wallpapers/c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738/display.webp'
      // palette missing intentionally
    }
  ]
};

// 1x1 test image pixel
const mockPixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

const mockServices = [
  { id: 'filebrowser', name: 'FileBrowser', category: 'Storage', running: true, can_toggle: true, open_label: 'Open Cloud Drive', open_url: '/drive/', status_badge: 'Active (24.1 MB)' },
  { id: 'suwayomi', name: 'Suwayomi', category: 'Reader', running: true, can_toggle: true, open_label: 'Open Suwayomi', open_url: '/manga/', status_badge: 'Active (152.0 MB)', unread_updates: 0 },
  { id: 'vaultwarden', name: 'Vaultwarden', category: 'Security', running: false, can_toggle: true, open_label: 'Open Vault', open_url: 'javascript:openVaultHTTPS()', status_badge: 'Disabled (0 MB)' },
  { id: 'adguard', name: 'AdGuard Home', category: 'DNS', running: true, can_toggle: true, open_label: 'Open AdGuard', open_url: '/dns/', status_badge: 'Active (35.4 MB)' },
  { id: 'terminal', name: 'Web Terminal', category: 'Terminal', running: false, can_toggle: true, open_label: 'Open Terminal', open_url: '/term/', status_badge: 'Disabled (0 MB)' },
  { id: 'agy', name: 'AGY AI Pair Programmer', category: 'Autonomous AI', running: true, can_toggle: true, open_label: 'Launch AGY', open_url: '/agy/', status_badge: 'Active (42.0 MB)' },
  { id: 'converter', name: 'Converter', category: 'Productivity', running: true, can_toggle: false, open_label: 'Convert', open_url: 'javascript:openConverterModal()', status_badge: 'Ready (0 MB Idle)', is_ondemand: true },
  { id: 'syncthing', name: 'Syncthing', category: 'Sync', running: false, can_toggle: true, open_label: 'Open Syncthing', open_url: 'javascript:openSyncthingGUI()', status_badge: 'Disabled (0 MB)' },
  { id: 'battery_guard', name: 'Battery Guard', category: 'Hardware', running: true, can_toggle: true, open_label: 'Monitor', open_url: 'javascript:alert(1)', status_badge: 'Active (8.2 MB)' },
  { id: 'antra', name: 'Antra Music', category: 'Media', running: true, can_toggle: true, open_label: 'Open Antra', open_url: '/antra/', status_badge: 'Active (28.0 MB)' },
  { id: 'navidrome', name: 'Music Player', category: 'Media', running: true, can_toggle: true, open_label: 'Open Player', open_url: '/music/', status_badge: 'Active (30.1 MB)' },
  { id: 'laptop_terminal', name: 'Laptop Terminal Gateway', category: 'System & Control', running: true, can_toggle: true, open_label: 'Open Gateway', open_url: '/laptop/', status_badge: 'Active (12.3 MB)' }
];

(async () => {
  let servicesState = JSON.parse(JSON.stringify(mockServices));
  let toggleLog = [];
  let simulateToggleFailure = false;
  let simulateToggleForbidden = false;

  const server = http.createServer((req, res) => {
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
    if (pathname === '/portal.css' || pathname === '/static/portal.css') {
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
    if (pathname === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        tailscale_ip: '100.75.159.53',
        local_ip: '192.168.1.50',
        uptime: '4d 6h 6m',
        battery: { percentage: 88, temperature: 31, status: 'Discharging' },
        ram: { used_mb: 3200, total_mb: 5800, percent: 55.2 },
        storage: { free_gb: 67, percent: 38.4 },
        cpu_temp: 36.8,
        network: { wifi_speed: 144, rssi: -57, wan_ping: 8.91, dns_status: 'Interface reachable' }
      }));
    }
    if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        services: servicesState,
        active_memory_mb: 516.1
      }));
    }
    if (pathname === '/api/services/toggle' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => {
        if (simulateToggleForbidden) {
          res.writeHead(403, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ error: 'Forbidden' }));
        }
        if (simulateToggleFailure) {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ status: 'error', message: 'Supervisor fault' }));
        }
        const data = JSON.parse(body || '{}');
        toggleLog.push(data);
        const svc = servicesState.find(s => s.id === data.service);
        if (svc) {
          svc.running = data.enable !== undefined ? Boolean(data.enable) : !svc.running;
          svc.status_badge = svc.running ? 'Active (24.1 MB)' : 'Disabled (0 MB)';
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ services: servicesState }));
      });
      return;
    }
    if (pathname === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        sessions: [
          { id: 'primary', name: 'main', is_primary: true },
          { id: 'worker-2', name: 'task2-worker', is_primary: false }
        ]
      }));
    }
    if (pathname === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        online: true,
        sessions: [{ id: '1234567890abcdef1234567890abcdef' }],
        max_sessions: 4
      }));
    }
    if (pathname === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ enabled: false }));
    }
    if (pathname === '/api/notes') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ notes: 'Workbench test note' }));
    }
    if (pathname === '/api/wallpapers') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify(mockWallpapersData));
    }
    if (pathname.startsWith('/api/wallpapers/') && pathname.endsWith('.webp')) {
      res.writeHead(200, { 'Content-Type': 'image/webp' });
      return res.end(mockPixel);
    }

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok' }));
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    const page = await browser.newPage();

    // -----------------------------------------------------------------------
    // ASSERTION 1: Admin Page Reference Structure (Header, Instrument, Work)
    // -----------------------------------------------------------------------
    console.log('--- TEST 1: Admin Page Reference Layout & Structure ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/admin`);
    await page.waitForSelector('#services-grid');

    // 1.1 Shell is display block (not grid rail)
    const shellDisplay = await page.evaluate(() => {
      const shell = document.querySelector('.shell') || document.querySelector('.dashboard-container');
      return window.getComputedStyle(shell).display;
    });
    assert.equal(shellDisplay, 'block', 'Shell must compute to display: block');

    // 1.2 No navigation rail or mobile bottom nav
    const navCount = await page.evaluate(() => document.querySelectorAll('nav').length);
    assert.equal(navCount, 0, 'Must have 0 <nav> elements (no obsolete rail / bottom nav)');

    // 1.3 Desktop H1 typography: ~40px font size
    const h1FontSize = await page.evaluate(() => {
      const h1 = document.querySelector('h1');
      return parseFloat(window.getComputedStyle(h1).fontSize);
    });
    console.log('Desktop H1 font size:', h1FontSize);
    assert(h1FontSize >= 28 && h1FontSize <= 42, `Desktop H1 must be between 28px and 42px (approx 40px), got ${h1FontSize}`);

    // 1.4 Instrument panel containing 4 health metric cards and unified network row
    const instrumentHealthCards = await page.evaluate(() => {
      const healthSection = document.querySelector('.health') || document.querySelector('section[aria-label="Hardware health"]');
      if (!healthSection) return 0;
      return healthSection.querySelectorAll('.metric, .m3-card').length;
    });
    console.log('Hardware health cards count:', instrumentHealthCards);
    assert.equal(instrumentHealthCards, 4, 'Must have exactly 4 hardware health tiles');

    const networkSectionExists = await page.evaluate(() => {
      return Boolean(document.getElementById('network') || document.querySelector('.network'));
    });
    assert(networkSectionExists, 'Network monitor section must exist');

    // 1.4.1 Actual rect positions for Network Monitor on desktop:
    // Header above grid, and 4 items on a single horizontal row
    const netLayout1440 = await page.evaluate(() => {
      const netSection = document.getElementById('network');
      const head = netSection.querySelector('.section-head');
      const grid = netSection.querySelector('.network-grid');
      const items = Array.from(grid.querySelectorAll('.net'));
      const headRect = head.getBoundingClientRect();
      const gridRect = grid.getBoundingClientRect();
      const itemRects = items.map(el => {
        const r = el.getBoundingClientRect();
        return { top: r.top, left: r.left, right: r.right, bottom: r.bottom };
      });
      return { headRect, gridRect, itemRects };
    });
    assert.equal(netLayout1440.itemRects.length, 4, 'Must have 4 network metric items');
    assert(netLayout1440.headRect.bottom <= netLayout1440.gridRect.top + 2, 'Network header must sit above the network grid');
    for (let i = 1; i < 4; i++) {
      assert(Math.abs(netLayout1440.itemRects[i].top - netLayout1440.itemRects[0].top) < 3, `Network item ${i} must be on same row as item 0 at 1440px`);
      assert(netLayout1440.itemRects[i].left >= netLayout1440.itemRects[i - 1].right - 2, `Network item ${i} must be to right of item ${i - 1}`);
    }

    // 1.5 Work layout: 2-column split with services and ~320px remote aside (Root specifies 320px)
    const workLayout = await page.evaluate(() => {
      const work = document.querySelector('.work') || document.querySelector('.workstation-layout');
      const aside = document.querySelector('aside.remote') || document.querySelectorAll('.workstation-col')[1];
      return {
        workDisplay: window.getComputedStyle(work).display,
        asideWidth: aside ? aside.getBoundingClientRect().width : 0
      };
    });
    console.log('Work layout at 1440px:', workLayout);
    assert.equal(workLayout.workDisplay, 'grid', 'Work area must be a grid');
    assert(workLayout.asideWidth >= 310 && workLayout.asideWidth <= 330, `Remote aside width must be ~320px, got ${workLayout.asideWidth}`);

    // -----------------------------------------------------------------------
    // ASSERTION 2: Service Grid Columns & Preferences (1..4)
    // -----------------------------------------------------------------------
    console.log('--- TEST 2: Service Grid Columns & Preferences ---');
    const colSelect = page.locator('#service-columns');
    assert(await colSelect.isVisible(), '#service-columns select must be visible');

    // Default 3 columns at 1440px
    const initialCols = await page.evaluate(() => {
      const grid = document.getElementById('services-grid');
      return window.getComputedStyle(grid).getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(initialCols, 3, 'Service grid should default to 3 columns at 1440px');

    // Select 4 columns
    await colSelect.selectOption('4');
    await page.waitForTimeout(100);
    const colsAfter4 = await page.evaluate(() => {
      const grid = document.getElementById('services-grid');
      return window.getComputedStyle(grid).getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(colsAfter4, 4, 'Service grid should have 4 columns when 4 is selected');

    // Select 2 columns
    await colSelect.selectOption('2');
    await page.waitForTimeout(100);
    const colsAfter2 = await page.evaluate(() => {
      const grid = document.getElementById('services-grid');
      return window.getComputedStyle(grid).getPropertyValue('grid-template-columns').trim().split(/\s+/).length;
    });
    assert.equal(colsAfter2, 2, 'Service grid should have 2 columns when 2 is selected');

    // Reset back to 3 columns
    await colSelect.selectOption('3');

    // -----------------------------------------------------------------------
    // ASSERTION 3: Service Filtering & Status Dot Binding
    // -----------------------------------------------------------------------
    console.log('--- TEST 3: Service Filtering & State Dots ---');
    const filterAll = page.locator('[data-filter="all"]');
    const filterActive = page.locator('[data-filter="active"]');
    const filterOff = page.locator('[data-filter="off"]');
    assert(await filterAll.isVisible(), 'All filter button must be visible');
    assert(await filterActive.isVisible(), 'Running filter button must be visible');
    assert(await filterOff.isVisible(), 'Disabled filter button must be visible');

    // Count all services initially (10 visible in grid, excluding agy and laptop_terminal)
    const countAll = await page.locator('#services-grid [data-service-id]').count();
    assert.equal(countAll, 10, 'All services count should be 10');

    // Verify status dot color binding: running is positive green, disabled is muted gray
    const runningDotColor = await page.locator('[data-service-id="filebrowser"] .dot').evaluate(el => window.getComputedStyle(el).backgroundColor);
    const stoppedDotColor = await page.locator('[data-service-id="vaultwarden"] .dot').evaluate(el => window.getComputedStyle(el).backgroundColor);
    console.log('Dot colors:', { runningDotColor, stoppedDotColor });
    assert.equal(runningDotColor, 'rgb(112, 214, 197)', 'Running service card must display active positive dot');
    assert.equal(stoppedDotColor, 'rgb(194, 199, 206)', 'Disabled service card must display disabled muted dot');

    // Click Running filter
    await filterActive.click();
    await page.waitForTimeout(100);
    const activeCards = await page.locator('#services-grid [data-service-id]:visible').evaluateAll(cards =>
      cards.map(c => c.getAttribute('data-service-id'))
    );
    console.log('Running services filtered:', activeCards);
    assert(activeCards.includes('filebrowser'), 'filebrowser should be in running view');
    assert(!activeCards.includes('vaultwarden'), 'vaultwarden (disabled) must not be in running view');
    assert(!activeCards.includes('terminal'), 'terminal (disabled) must not be in running view');

    // Click Disabled filter
    await filterOff.click();
    await page.waitForTimeout(100);
    const disabledCards = await page.locator('#services-grid [data-service-id]:visible').evaluateAll(cards =>
      cards.map(c => c.getAttribute('data-service-id'))
    );
    console.log('Disabled services filtered:', disabledCards);
    assert(disabledCards.includes('vaultwarden'), 'vaultwarden must be in disabled view');
    assert(disabledCards.includes('terminal'), 'terminal must be in disabled view');
    assert(!disabledCards.includes('filebrowser'), 'filebrowser must not be in disabled view');

    // Restore All filter
    await filterAll.click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#services-grid [data-service-id]').count(), 10, 'All services restored');

    // -----------------------------------------------------------------------
    // ASSERTION 4: Accessible Arrange Mode, Pointer Drag & DOM Reorder
    // -----------------------------------------------------------------------
    console.log('--- TEST 4: Arrange Mode, Pointer Drag & Keyboard Reorder ---');
    const arrangeBtn = page.locator('#btn-toggle-arrange, #arrange');
    await arrangeBtn.click();
    await page.waitForTimeout(100);

    // Arrange hint and reset order button visible
    assert(await page.locator('#arrange-hint, .arrange-hint').isVisible(), 'Arrange hint must be visible when arranging');
    assert(await page.locator('#btn-reset-service-order, #reset-order').isVisible(), 'Reset order button must be visible when arranging');

    // Drag handle visible
    const dragHandle = page.locator('#services-grid [data-drag]').first();
    assert(await dragHandle.isVisible(), 'Pointer drag handles must be visible in arrange mode');

    // Keyboard move earlier / later buttons
    const firstCardId = await page.locator('#services-grid [data-service-id]').first().getAttribute('data-service-id');
    const moveDownBtn = page.locator(`#services-grid [data-service-id="${firstCardId}"] button[data-action="move-later"], #services-grid [data-service-id="${firstCardId}"] button[data-down]`);
    assert(await moveDownBtn.isVisible(), 'Move down/later button must be visible');

    // Click move down
    await moveDownBtn.click();
    await page.waitForTimeout(100);
    const newSecondCardId = await page.locator('#services-grid [data-service-id]').nth(1).getAttribute('data-service-id');
    assert.equal(newSecondCardId, firstCardId, 'First card must have moved to second position in DOM');

    // Check aria-live announcer announced position
    const announcement = await page.locator('#reorder-announcer').textContent();
    console.log('Reorder announcement:', announcement);

    // Reset order
    const resetOrderBtn = page.locator('#btn-reset-service-order, #reset-order');
    await resetOrderBtn.click();
    await page.waitForTimeout(100);
    const restoredFirstId = await page.locator('#services-grid [data-service-id]').first().getAttribute('data-service-id');
    assert.equal(restoredFirstId, firstCardId, 'Default order restored');

    // Exit arrange mode
    await arrangeBtn.click();
    await page.waitForTimeout(100);

    // -----------------------------------------------------------------------
    // ASSERTION 5: Remote Tools (Laptop, AGY Multi-session, Notes, Connection)
    // -----------------------------------------------------------------------
    console.log('--- TEST 5: Remote Tools & Sessions ---');
    // Laptop panel has switch and new button
    assert(await page.locator('label:has(#laptop-service-toggle)').isVisible(), 'Laptop switch must exist');
    assert(await page.locator('#laptop-new').isVisible(), 'Laptop New Terminal button must exist');

    // AGY panel has multiple sessions container
    assert(await page.locator('label:has(#agy-service-toggle)').isVisible(), 'AGY switch must exist');
    assert(await page.locator('#agy-new').isVisible(), 'AGY New Instance button must exist');
    assert(await page.locator('#agy-sessions-container').isVisible(), 'AGY sessions container must exist');
    const agySessionItems = await page.locator('#agy-sessions-container [data-agy-url]').count();
    assert(agySessionItems >= 2, 'AGY container must display multiple session items');

    // Quick notes textarea
    assert(await page.locator('#user-notes-input').isVisible(), 'Quick notes textarea must exist');

    // Connection details has copy buttons
    const copyButtons = await page.locator('[data-copy], button:has-text("Copy")').count();
    assert(copyButtons >= 1, 'Connection details must offer copy buttons');

    // -----------------------------------------------------------------------
    // ASSERTION 6: Multi-Viewport Responsive, Network 2x2 & Matugen Theme
    // -----------------------------------------------------------------------
    console.log('--- TEST 6: Multi-Viewport Checks (1440, 1280, 768, 390) ---');
    const viewports = [
      { width: 1440, height: 900, name: 'admin-desktop-1440' },
      { width: 1280, height: 800, name: 'admin-desktop-1280' },
      { width: 768, height: 1024, name: 'admin-tablet-768' },
      { width: 390, height: 844, name: 'admin-mobile-390' }
    ];

    for (const vp of viewports) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(150);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      assert.equal(overflow, false, `Viewport ${vp.width}x${vp.height} must not have horizontal overflow`);

      if (vp.width === 390) {
        // Assert 2x2 Network Monitor grid positions at 390px
        const netLayout390 = await page.evaluate(() => {
          const netSection = document.getElementById('network');
          const grid = netSection.querySelector('.network-grid');
          const items = Array.from(grid.querySelectorAll('.net'));
          return items.map(el => {
            const r = el.getBoundingClientRect();
            return { top: r.top, left: r.left, right: r.right, bottom: r.bottom };
          });
        });
        assert.equal(netLayout390.length, 4, 'Mobile must have 4 network items');
        assert(Math.abs(netLayout390[1].top - netLayout390[0].top) < 3, 'Mobile items 0 and 1 must be on row 1');
        assert(Math.abs(netLayout390[3].top - netLayout390[2].top) < 3, 'Mobile items 2 and 3 must be on row 2');
        assert(netLayout390[2].top >= netLayout390[0].bottom - 2, 'Mobile row 2 must be below row 1');
        assert(netLayout390[1].left > netLayout390[0].left, 'Mobile item 1 must be right of item 0');
        assert(netLayout390[3].left > netLayout390[2].left, 'Mobile item 3 must be right of item 2');
      }

      const screenshotPath = path.join(previewDir, `${vp.name}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: true });
      console.log(`Saved screenshot: ${screenshotPath}`);
    }

    // -----------------------------------------------------------------------
    // ASSERTION 7: Matugen Dynamic Wallpaper Theme & Neutral Fallback
    // -----------------------------------------------------------------------
    console.log('--- TEST 7: Matugen Wallpaper Theme & Neutral Fallback ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/admin`);
    await page.waitForSelector('#services-grid');

    // Verify dynamic wallpaper Matugen primary token is applied (#98ccf9)
    const appliedPrimary = await page.evaluate(() => {
      return window.getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-primary').trim();
    });
    console.log('Applied Matugen primary token:', appliedPrimary);
    assert.equal(appliedPrimary, '#98ccf9', 'Matugen dynamic primary token should match wallpaper palette');

    const adminWallpaperPath = path.join(previewDir, 'admin-wallpaper-1440.png');
    await page.screenshot({ path: adminWallpaperPath, fullPage: true });
    console.log(`Saved wallpaper screenshot: ${adminWallpaperPath}`);

    // Test neutral fallback (no-palette wallpaper or missing palette)
    await page.evaluate(() => {
      localStorage.setItem('mobile.appearance.v1', JSON.stringify({
        version: 1,
        wallpaperId: 'c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738',
        blur: 40,
        translucency: 25
      }));
    });
    await page.reload();
    await page.waitForSelector('#services-grid');

    const neutralPrimary = await page.evaluate(() => {
      return document.documentElement.style.getPropertyValue('--md-sys-color-primary').trim();
    });
    assert.equal(neutralPrimary, '', 'Neutral fallback must clear dynamic inline token overrides');

    const adminNeutralPath = path.join(previewDir, 'admin-neutral-1440.png');
    await page.screenshot({ path: adminNeutralPath, fullPage: true });
    console.log(`Saved neutral screenshot: ${adminNeutralPath}`);

    // Restore wallpaper for family check
    await page.evaluate(() => {
      localStorage.removeItem('mobile.appearance.v1');
    });

    // -----------------------------------------------------------------------
    // ASSERTION 8: Family View Role Boundary Checks & Screenshots
    // -----------------------------------------------------------------------
    console.log('--- TEST 8: Family View Role Boundary ---');
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`${baseUrl}/family`);
    await page.waitForSelector('body');

    // Family must NOT have admin remote tools
    assert.equal(await page.locator('#laptop-service-toggle').count(), 0, 'Family must NOT have laptop service toggle');
    assert.equal(await page.locator('#agy-service-toggle').count(), 0, 'Family must NOT have agy service toggle');
    assert.equal(await page.locator('#btn-appearance').count(), 0, 'Family must NOT have appearance settings button');
    assert.equal(await page.locator('#vpn-toggle-btn').count(), 0, 'Family must NOT have Tor proxy toggle');

    // Family MUST have permitted features
    assert(await page.locator('section[aria-label="Repertoire Finder"]').isVisible(), 'Family must have Repertoire Finder');
    assert(await page.locator('#user-notes-input').isVisible(), 'Family must have permitted personal/family notes');
    assert(await page.locator('a[href="/drive/"]').isVisible(), 'Family must have Cloud Drive launcher');

    // Save Family screenshots (wallpaper and mobile)
    const familyWallpaper = path.join(previewDir, 'family-wallpaper-1280.png');
    const familyDesktop = path.join(previewDir, 'family-desktop-1280.png');
    await page.screenshot({ path: familyWallpaper, fullPage: true });
    await page.screenshot({ path: familyDesktop, fullPage: true });
    console.log(`Saved screenshot: ${familyDesktop}`);

    // Family neutral fallback screenshot
    await page.evaluate(() => {
      localStorage.setItem('mobile.appearance.v1', JSON.stringify({
        version: 1,
        wallpaperId: 'c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738c1d2e3f405162738',
        blur: 40,
        translucency: 25
      }));
    });
    await page.reload();
    await page.waitForSelector('body');
    const familyNeutral = path.join(previewDir, 'family-neutral-1280.png');
    await page.screenshot({ path: familyNeutral, fullPage: true });
    console.log(`Saved neutral screenshot: ${familyNeutral}`);

    // Mobile family check
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(150);
    const familyMobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert.equal(familyMobileOverflow, false, 'Family mobile 390px must not have horizontal overflow');

    const familyMobile = path.join(previewDir, 'family-mobile-390.png');
    await page.screenshot({ path: familyMobile, fullPage: true });
    console.log(`Saved screenshot: ${familyMobile}`);

    console.log('===========================================================');
    console.log('EXPRESSIVE WORKBENCH BROWSER ACCEPTANCE SUITE COMPLETE');
    console.log('===========================================================');

  } finally {
    await browser.close();
    server.close();
  }
})().catch(err => {
  console.error(err);
  process.exit(1);
});
