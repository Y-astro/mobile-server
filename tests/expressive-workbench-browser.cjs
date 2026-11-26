const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const appearanceHelper = require('./fixtures/appearance-test-helper.cjs');

const rootDir = path.resolve(__dirname, '..');
const previewDir = process.env.EXPRESSIVE_PREVIEW_DIR || path.join(os.tmpdir(), 'expressive-preview');
if (!fs.existsSync(previewDir)) {
  fs.mkdirSync(previewDir, { recursive: true });
}

const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
const loginHtml = fs.readFileSync(path.join(rootDir, 'web/login.html'), 'utf8');
const antraHtml = fs.readFileSync(path.join(rootDir, 'web/antra.html'), 'utf8');
const antraCss = fs.readFileSync(path.join(rootDir, 'web/antra.css'), 'utf8');
const antraJs = fs.readFileSync(path.join(rootDir, 'web/antra.js'), 'utf8');
const repertoireHtml = fs.readFileSync(path.join(rootDir, 'web/repertoire.html'), 'utf8');
const repertoireCss = fs.readFileSync(path.join(rootDir, 'web/repertoire.css'), 'utf8');
const repertoireJs = fs.readFileSync(path.join(rootDir, 'web/repertoire.js'), 'utf8');
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

// Valid SVG gradient wallpaper fixture (proves wallpaper & glass rendering visually)
const mockWallpaperSvg = Buffer.from(
  `<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080">
    <defs>
      <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#101417"/>
        <stop offset="50%" stop-color="#1b2a38"/>
        <stop offset="100%" stop-color="#0b0f12"/>
      </linearGradient>
    </defs>
    <rect width="100%" height="100%" fill="url(#g)"/>
  </svg>`
);

const mockServices = [
  { id: 'filebrowser', name: 'FileBrowser', desc: 'Shared \"family\" files and photos', category: 'Storage', running: true, can_toggle: true, open_label: 'Open Cloud Drive', open_url: '/drive/', status_badge: 'Active (24.1 MB)' },
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
    if (pathname === '/login') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(loginHtml);
    }
    if (pathname === '/api/login' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => {
        let creds = {};
        try { creds = JSON.parse(body); } catch (e) {}
        if (creds.username === 'astro' && creds.password === 'secret') {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ status: 'ok', user: 'astro', role: 'admin', redirect: '/admin' }));
        }
        res.writeHead(401, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Invalid username or password' }));
      });
      return;
    }
    if (pathname === '/api/auth-check-admin') {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8', 'X-Forwarded-User': 'astro' });
      return res.end('OK');
    }
    if (pathname === '/antra' || pathname === '/antra/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(antraHtml);
    }
    if (pathname === '/antra.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(antraCss);
    }
    if (pathname === '/antra.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(antraJs);
    }
    if (pathname === '/api/antra/jobs') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        jobs: [
          {
            id: 'job-1',
            title: 'Symphony No. 5 in C Minor',
            artist: 'Ludwig van Beethoven',
            source_url: 'https://example.com/stream/track1',
            status: 'completed',
            destination: 'private',
            progress: 100,
            quality: 'FLAC 24-bit 96kHz',
            total_tracks: 4,
            completed_tracks: 4,
            created_at: 1700000000,
            updated_at: 1700000100
          },
          {
            id: 'job-2',
            title: 'Carnatic Classical Concert',
            artist: 'Madurai Mani Iyer',
            source_url: 'https://example.com/stream/album2',
            status: 'queued',
            destination: 'family',
            progress: 0,
            quality: 'FLAC 16-bit 44.1kHz',
            total_tracks: 8,
            completed_tracks: 0,
            priority: 1,
            created_at: 1700000200,
            updated_at: 1700000200
          }
        ],
        worker: { active: true, status: 'idle' },
        settings: {
          output_directories: { private: 'Music/Astro', family: 'Music/Family' },
          quality_filter: 'best_available_flac'
        }
      }));
    }
    const matchAntraDetail = pathname.match(/^\/api\/antra\/jobs\/([^/]+)$/);
    if (matchAntraDetail && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        job: { id: matchAntraDetail[1], title: 'Carnatic Classical Concert', status: 'queued' },
        tracks: [
          { id: 't1', title: 'Track 1', progress: 100, status: 'completed' },
          { id: 't2', title: 'Track 2', progress: 0, status: 'queued' }
        ]
      }));
    }
    if (pathname === '/api/antra/settings') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        output_directories: { private: 'Music/Astro', family: 'Music/Family' },
        quality_filter: 'best_available_flac'
      }));
    }
    if (pathname === '/repertoire' || pathname === '/repertoire/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(repertoireHtml);
    }
    if (pathname === '/repertoire/app.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(repertoireCss);
    }
    if (pathname === '/repertoire/app.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(repertoireJs);
    }
    if (pathname === '/api/repertoire') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        fields: [
          { key: 'Composition', label: 'Composition' },
          { key: 'rAga', label: 'Raga' },
          { key: 'tALa', label: 'Tala' },
          { key: 'Composer', label: 'Composer' },
          { key: 'Comp_type', label: 'Composition type' },
          { key: 'Language', label: 'Language' },
          { key: 'Subject', label: 'Subject' },
          { key: 'Repertoire', label: 'Repertoire' },
          { key: 'pATAntara', label: 'Patantara' },
          { key: 'ConcertFit', label: 'Concert fit' },
          { key: 'Teaching', label: 'Teaching' },
          { key: 'Rep date', label: 'Repertoire date' },
          { key: 'Remarks', label: 'Remarks' },
          { key: 'Reference', label: 'Reference' }
        ],
        records: [
          { id: '1', Composition: 'Vatapi Ganapatim', rAga: 'Hamsadhvani', tALa: 'Adi', Composer: 'Muthuswami Dikshitar', Comp_type: 'Kriti', Language: 'Sanskrit', Repertoire: 'Yes', Rep_date: '2024-01-01' },
          { id: '2', Composition: 'Endaro Mahanubhavulu', rAga: 'Sri', tALa: 'Adi', Composer: 'Tyagaraja', Comp_type: 'Kriti', Language: 'Telugu', Repertoire: 'Yes', Rep_date: '2024-01-01' },
          { id: '3', Composition: 'Bhavayami Raghuramam', rAga: 'Ragamalika', tALa: 'Rupaka', Composer: 'Swathi Thirunal', Comp_type: 'Kriti', Language: 'Sanskrit', Repertoire: 'Yes', Rep_date: '2024-01-01' }
        ],
        source: 'database repertoire 202610.xlsx',
        revision: 'rev-test-1',
        updated: '2026-11-24T12:00:00Z',
        versions: {
          active: 'current',
          limit: 8388608,
          current: '2026-11-24T12:00:00Z',
          previous: null
        }
      }));
    }
    if (pathname === '/api/repertoire/versions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        active: 'current',
        limit: 8388608,
        current: '2026-11-24T12:00:00Z',
        previous: null
      }));
    }
    if (pathname === '/api/notes') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ notes: 'Workbench test note' }));
    }
    if (pathname === '/api/wallpapers') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify(mockWallpapersData));
    }
    if (pathname.startsWith('/api/wallpapers/')) {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
      return res.end(mockWallpaperSvg);
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

    // 1.4.2 Numeric reading font size inheritance (24px per v14 Quiet Glass) and unit label (12px)
    const wifiSpeedFontSize = await page.locator('#net-wifi-speed').evaluate(el => window.getComputedStyle(el).fontSize);
    const wifiUnitFontSize = await page.locator('.net .value .unit').first().evaluate(el => window.getComputedStyle(el).fontSize);
    console.log('Network telemetry font sizes:', { wifiSpeedFontSize, wifiUnitFontSize });
    assert.equal(wifiSpeedFontSize, '24px', 'Numeric telemetry reading #net-wifi-speed must inherit 24px');
    assert.equal(wifiUnitFontSize, '12px', 'Unit label .unit must be styled to 12px');

    // 1.5 Work layout: 2-column split with services and 315px ruled remote aside + visible heading
    const workLayout = await page.evaluate(() => {
      const work = document.querySelector('.work') || document.querySelector('.workstation-layout');
      const aside = document.querySelector('aside.remote') || document.querySelectorAll('.workstation-col')[1];
      const heading = aside ? aside.querySelector(':scope > h2') : null;
      return {
        workDisplay: window.getComputedStyle(work).display,
        asideWidth: aside ? aside.getBoundingClientRect().width : 0,
        headingVisible: heading ? window.getComputedStyle(heading).display !== 'none' && !heading.classList.contains('sr-only') && heading.getBoundingClientRect().height > 0 : false
      };
    });
    console.log('Work layout at 1440px:', workLayout);
    assert.equal(workLayout.workDisplay, 'grid', 'Work area must be a grid');
    assert(workLayout.asideWidth >= 310 && workLayout.asideWidth <= 320, `Remote aside width must be ~315px, got ${workLayout.asideWidth}`);
    assert.equal(workLayout.headingVisible, true, 'Remote access heading must be visible in v14');

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

    // Column preference is an intrinsic maximum, including intermediate widths.
    await page.setViewportSize({ width: 1280, height: 800 });
    await colSelect.selectOption('4');
    assert.equal(await page.locator('#services-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(/\s+/).length), 4,
      '1280px must not impose the old two-column cap');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#services-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(/\s+/).length), 2,
      'Four-column preference must fit two readable tiles on mobile');
    await colSelect.selectOption('1');
    assert.equal(await page.locator('#services-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(/\s+/).length), 1,
      'One-column preference must remain effective on mobile');
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.evaluate(() => localStorage.setItem('k20-v6-layout', JSON.stringify({ columns: 'invalid' })));
    await page.reload();
    await page.waitForSelector('#services-grid .svc');
    assert.equal(await colSelect.inputValue(), '3', 'Invalid stored density must fall back to three columns');

    // Reset back to 3 columns
    await colSelect.selectOption('3');
    const topbarBox = await page.locator('.topbar').boundingBox();
    assert(topbarBox.height <= 64, 'Reference topbar must remain a single compact row on desktop');
    assert.equal(await page.locator('.topbar #header-local-ip').count(), 0, 'Connection details belong outside the compact header');
    assert.equal(await page.locator('.connection #header-local-ip').count(), 1, 'Wi-Fi navigation must remain available');
    const firstTile = page.locator('#services-grid .svc').first();
    assert((await firstTile.boundingBox()).height <= 150, 'Descriptions must not expand service tiles');
    const firstLaunch = firstTile.locator('.service-open-btn');
    assert((await firstLaunch.getAttribute('title')).includes(' · '), 'Launch hint must retain service description');
    assert.equal((await firstLaunch.boundingBox()).width, 32, 'Service launch must match the 32px reference');
    assert.equal((await firstLaunch.boundingBox()).height, 32, 'Service launch must be a compact square');

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

    const cardSurfaces = await page.evaluate(() => {
      const background=id=>getComputedStyle(document.querySelector(`[data-service-id="${id}"]`)).backgroundColor;
      return { active:background('filebrowser'), disabled:background('vaultwarden'), ready:background('converter') };
    });
    assert.notEqual(cardSurfaces.active, cardSurfaces.disabled, 'Active service must show the wallpaper accent tint');
    assert.equal(cardSurfaces.ready, cardSurfaces.disabled, 'Ready on-demand utility must retain a neutral surface');
    const glassAlpha = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim());
    await page.evaluate(() => document.documentElement.style.setProperty('--app-panel-alpha', '0'));
    await page.waitForFunction(() => ['filebrowser', 'vaultwarden'].every(id => {
      const color = getComputedStyle(document.querySelector(`[data-service-id="${id}"]`)).backgroundColor;
      return color.endsWith('/ 0)') || color.endsWith(', 0)');
    }), null, { timeout: 1500 });
    await page.evaluate(value => document.documentElement.style.setProperty('--app-panel-alpha', value || '0.65'), glassAlpha);

    const driveToggle = page.locator('[data-service-id="filebrowser"] input[data-action="toggle"]');
    await driveToggle.locator('..').click();
    await page.waitForFunction(() => document.querySelector('[data-service-id="filebrowser"]').dataset.state === 'stopped');
    await page.waitForFunction(color => getComputedStyle(document.querySelector('[data-service-id="filebrowser"]')).backgroundColor === color, cardSurfaces.disabled);
    assert.equal(await page.locator('[data-service-id="filebrowser"]').evaluate(el=>getComputedStyle(el).backgroundColor), cardSurfaces.disabled,
      'Turning a service off must remove the state tint');
    await driveToggle.locator('..').click();
    await page.waitForFunction(() => document.querySelector('[data-service-id="filebrowser"]').dataset.state === 'running');


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

    // Input-driven pointer drag and drop test
    const firstCard = page.locator('#services-grid [data-service-id]').first();
    const firstCardId = await firstCard.getAttribute('data-service-id');
    const secondCard = page.locator('#services-grid [data-service-id]').nth(1);
    const secondBox = await secondCard.boundingBox();

    await firstCard.scrollIntoViewIfNeeded();
    await dragHandle.hover();
    await page.mouse.down();
    await page.mouse.move(secondBox.x + secondBox.width / 2, secondBox.y + secondBox.height / 2, { steps: 5 });
    await page.mouse.up();
    await page.waitForTimeout(150);

    const orderAfterPointerDrag = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid [data-service-id]')).map(c => c.getAttribute('data-service-id'));
    });
    console.log('Order after pointer drag:', orderAfterPointerDrag);
    assert.notEqual(orderAfterPointerDrag[0], firstCardId, 'First card must have moved via pointer drag');

    const storedOrder = await page.evaluate(() => localStorage.getItem('mobile.services.order.v1'));
    assert(storedOrder, 'Pointer drag order must persist in localStorage');
    assert.deepEqual(JSON.parse(storedOrder), orderAfterPointerDrag, 'Exact stored order must match DOM order after pointer drag');

    // Reset order between pointer and keyboard checks to provide clean baseline
    const resetOrderBtn = page.locator('#btn-reset-service-order, #reset-order');
    await resetOrderBtn.click();
    await page.waitForTimeout(100);
    const resetFirstId = await page.locator('#services-grid [data-service-id]').first().getAttribute('data-service-id');
    assert.equal(resetFirstId, firstCardId, 'Order must reset before keyboard checks');

    // Keyboard move earlier / later buttons (accessible equivalent)
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
    assert.equal((await page.locator('#laptop-new').boundingBox()).height, 34, 'New Terminal must match reference density');
    assert.equal((await page.locator('#agy-new').boundingBox()).height, 34, 'New AGY must match reference density');

    // Laptop session ID display in 320px aside
    const laptopLabelText = await page.locator('#laptop-sessions .mono').first().textContent();
    const laptopLabelTitle = await page.locator('#laptop-sessions .mono').first().getAttribute('title');
    const laptopLabelWhiteSpace = await page.locator('#laptop-sessions .mono').first().evaluate(el => window.getComputedStyle(el).whiteSpace);
    console.log('Laptop session label in 320px aside:', { laptopLabelText, laptopLabelTitle, laptopLabelWhiteSpace });
    assert(laptopLabelText.startsWith('Terminal '), 'Laptop session label must display Terminal title');
    assert.equal(laptopLabelTitle, '1234567890abcdef1234567890abcdef', 'Laptop session must have full ID in accessible title');
    assert.equal(laptopLabelWhiteSpace, 'nowrap', 'Laptop session id in 320px aside must not split into scraps');

    const laptopPanel = page.locator('[aria-labelledby="laptop-heading"]');
    const agyPanel = page.locator('[aria-labelledby="agy-heading"]');
    console.log('Compact panel heights:', JSON.stringify({laptop:(await laptopPanel.boundingBox()).height, agy:(await agyPanel.boundingBox()).height}));
    assert.equal(await laptopPanel.evaluate(el=>getComputedStyle(el).backgroundColor), await agyPanel.evaluate(el=>getComputedStyle(el).backgroundColor),
      'Both remote panels must use the same neutral surface');
    assert((await laptopPanel.boundingBox()).height <= 235, 'One-session laptop panel must stay compact');
    assert((await agyPanel.boundingBox()).height <= 290, 'Two-session AGY panel must stay compact');
    assert((await page.locator('#laptop-sessions > .session').first().boundingBox()).height <= 52,
      'Laptop session name and actions should share one row');
    for (const [panel, newId] of [[laptopPanel, '#laptop-new'], [agyPanel, '#agy-new']]) {
      const details = panel.locator('details');
      const summary = details.locator('summary');
      const actionBox = await panel.locator(newId).boundingBox();
      const summaryBox = await summary.boundingBox();
      assert(Math.abs(actionBox.y - summaryBox.y) <= 4, 'New and Session details must share a compact toolbar');
      await summary.focus();
      await summary.press('Enter');
      assert.equal(await details.evaluate(el=>el.open), true, 'Keyboard must open lifecycle guidance');
      assert(await details.locator('p').last().isVisible(), 'Lifecycle guidance must remain available');
      assert((await details.boundingBox()).width <= (await panel.boundingBox()).width, 'Expanded help must stay inside panel');
      await summary.press('Enter');
      assert.equal(await details.evaluate(el=>el.open), false);
    }
    const longLabel = page.locator('#agy-sessions-container .session-label .mono').last();
    const originalLabel = await longLabel.textContent();
    await longLabel.evaluate(el => { el.textContent = 'AGY Instance: ' + 'long-session-name-'.repeat(12); });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
      'Long session names must not spill outside the dashboard');
    await longLabel.evaluate((el, text) => { el.textContent = text; }, originalLabel);

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
    console.log('--- TEST 6: Multi-Viewport Checks (1440, 1280, 1050, 768, 390, 320) ---');
    const viewports = [
      { width: 1440, height: 900, name: 'admin-desktop-1440' },
      { width: 1280, height: 800, name: 'admin-desktop-1280' },
      { width: 1050, height: 900, name: 'admin-stack-1050' },
      { width: 768, height: 1024, name: 'admin-tablet-768' },
      { width: 390, height: 844, name: 'admin-mobile-390' },
      { width: 320, height: 768, name: 'admin-mobile-320' }
    ];

    for (const vp of viewports) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(150);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      assert.equal(overflow, false, `Viewport ${vp.width}x${vp.height} must not have horizontal overflow`);

      if (vp.width === 1050) {
        const stackCols = await page.evaluate(() => {
          const workCols = getComputedStyle(document.querySelector('.work')).gridTemplateColumns.trim().split(/\s+/).length;
          const remoteCols = getComputedStyle(document.querySelector('.remote')).gridTemplateColumns.trim().split(/\s+/).length;
          return { workCols, remoteCols };
        });
        assert.equal(stackCols.workCols, 1, 'Work must stack to 1 column at 1050px');
        assert.equal(stackCols.remoteCols, 2, 'Remote cards must arrange in 2 columns at 1050px');
      }

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
        const metricHeaderOverflow = await page.locator('.metric-head').evaluateAll(items => items.some(el => el.scrollWidth > el.clientWidth));
        assert.equal(metricHeaderOverflow, false, 'Mobile metric labels must fit without spilling');
        assert.equal(netLayout390.length, 4, 'Mobile must have 4 network items');
        assert(Math.abs(netLayout390[1].top - netLayout390[0].top) < 3, 'Mobile items 0 and 1 must be on row 1');
        assert(Math.abs(netLayout390[3].top - netLayout390[2].top) < 3, 'Mobile items 2 and 3 must be on row 2');
        assert(netLayout390[2].top >= netLayout390[0].bottom - 2, 'Mobile row 2 must be below row 1');
        assert(netLayout390[1].left > netLayout390[0].left, 'Mobile item 1 must be right of item 0');
        assert(netLayout390[3].left > netLayout390[2].left, 'Mobile item 3 must be right of item 2');
      }

      const screenshotPath = path.join(previewDir, `${vp.name}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: true });
      if (vp.width === 390) await page.screenshot({ path: path.join(previewDir, 'admin-mobile-first-fold.png') });
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

    // Assert wallpaper load
    await page.waitForFunction(() => {
      const appBg = document.documentElement.style.getPropertyValue('--app-wallpaper-image');
      return appBg && appBg.includes('/api/wallpapers/');
    }, { timeout: 5000 });
    const loadedWallpaperUrl = await page.evaluate(() => document.documentElement.style.getPropertyValue('--app-wallpaper-image'));
    console.log('Loaded wallpaper image URL:', loadedWallpaperUrl);
    assert(loadedWallpaperUrl.includes('/api/wallpapers/'), 'Wallpaper image must be loaded into --app-wallpaper-image');

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

    // Verify scoped card title typography matches neighbouring titles without breaking heading semantics
    const repTitleSize = await page.locator('section[aria-label="Repertoire Finder"] .svc-title').evaluate(el => window.getComputedStyle(el).fontSize);
    const driveTitleSize = await page.locator('[data-service-id="filebrowser"] .svc-title').evaluate(el => window.getComputedStyle(el).fontSize);
    console.log('Family card title font sizes:', { repTitleSize, driveTitleSize });
    assert.equal(repTitleSize, driveTitleSize, 'Family Repertoire h2.svc-title must match neighbouring card title font size');

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

    // -----------------------------------------------------------------------
    // ASSERTION 9: Login Page (Desktop 1440 & Mobile 390)
    // -----------------------------------------------------------------------
    console.log('--- TEST 9: Login Page Acceptance ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/login`);
    await page.waitForSelector('#login-form');

    // Panel border radius 28px
    const loginCardRadius = await page.locator('.login-panel, .m3-card').first().evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Login card border-radius:', loginCardRadius);
    assert.equal(loginCardRadius, '28px', 'Login card must have 28px outer border radius');

    // Touch targets >= 48px
    const userBox = await page.locator('#username').boundingBox();
    const passBox = await page.locator('#password').boundingBox();
    const submitBox = await page.locator('#submit-btn').boundingBox();
    assert(userBox.height >= 48, `Username field height must be >= 48px, got ${userBox.height}`);
    assert(passBox.height >= 48, `Password field height must be >= 48px, got ${passBox.height}`);
    assert(submitBox.height >= 48, `Submit button height must be >= 48px, got ${submitBox.height}`);

    // Desktop screenshot
    const loginDesktop = path.join(previewDir, 'login-desktop-1440.png');
    await page.screenshot({ path: loginDesktop, fullPage: true });
    console.log(`Saved screenshot: ${loginDesktop}`);

    // Mobile check at 390px
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    const loginOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert.equal(loginOverflow, false, 'Login mobile 390px must not have horizontal overflow');

    // Trigger synthetic error & test error container
    await page.locator('#username').fill('invalid');
    await page.locator('#password').fill('wrong');
    await page.locator('#submit-btn').click();
    await page.waitForSelector('#error-msg:not(.hidden)');
    const errorMsgVisible = await page.locator('#error-msg').isVisible();
    assert(errorMsgVisible, 'Error message must be visible upon failed login');

    const loginMobile = path.join(previewDir, 'login-mobile-390.png');
    await page.screenshot({ path: loginMobile, fullPage: true });
    console.log(`Saved screenshot: ${loginMobile}`);

    // -----------------------------------------------------------------------
    // ASSERTION 10: Dialog Consistency (Appearance & Converter)
    // -----------------------------------------------------------------------
    console.log('--- TEST 10: Dialog Consistency (Appearance & Converter) ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/admin`);
    await page.waitForSelector('#services-grid');

    // 10.1 Appearance Dialog
    await page.waitForSelector('#btn-appearance');
    await page.locator('#btn-appearance').click();
    await page.waitForSelector('dialog#appearance-dialog[open]');
    const appearanceDialogRadius = await page.locator('dialog#appearance-dialog').evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Appearance dialog border-radius:', appearanceDialogRadius);
    assert.equal(appearanceDialogRadius, '28px', 'Appearance dialog outer panel must have 28px border radius');

    const appearanceCloseBtnBox = await page.locator('.appearance-close-btn').boundingBox();
    assert(appearanceCloseBtnBox.width >= 48 && appearanceCloseBtnBox.height >= 48, 'Appearance close button touch target >= 48px');

    const appearanceScreenshot = path.join(previewDir, 'appearance-dialog-1440.png');
    await page.screenshot({ path: appearanceScreenshot, fullPage: true });
    console.log(`Saved screenshot: ${appearanceScreenshot}`);

    // Dismiss with Escape
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    const appearanceOpenAfterEsc = await page.evaluate(() => Boolean(document.querySelector('dialog#appearance-dialog[open]')));
    assert.equal(appearanceOpenAfterEsc, false, 'Appearance dialog must dismiss on Escape');

    // 10.2 Converter Dialog
    const converterLaunchBtn = page.locator('#services-grid [data-service-id="converter"] .service-open-btn');
    await converterLaunchBtn.scrollIntoViewIfNeeded();
    await converterLaunchBtn.click();
    await page.waitForSelector('#converter-modal:not(.hidden)');
    const converterDialogRadius = await page.locator('#converter-modal > div').evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Converter dialog border-radius:', converterDialogRadius);
    assert.equal(converterDialogRadius, '28px', 'Converter dialog outer panel must have 28px border radius');

    const convertCloseBtn = page.locator('#converter-modal button[onclick="closeConverterModal()"]').first();
    const convertCloseBox = await convertCloseBtn.boundingBox();
    assert(convertCloseBox.width >= 48 && convertCloseBox.height >= 48, 'Converter close button touch target >= 48px');

    const convertBtnBox = await page.locator('#convert-btn').boundingBox();
    assert(convertBtnBox.height >= 48, 'Convert button height >= 48px');

    const converterScreenshot = path.join(previewDir, 'converter-dialog-1440.png');
    await page.screenshot({ path: converterScreenshot, fullPage: true });
    console.log(`Saved screenshot: ${converterScreenshot}`);

    // Dismiss with Escape
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#converter-modal').evaluate(el => el.classList.contains('hidden')), true, 'Converter dialog must close on Escape');

    // -----------------------------------------------------------------------
    // ASSERTION 11: Antra Downloader Acceptance
    // -----------------------------------------------------------------------
    console.log('--- TEST 11: Antra Downloader Acceptance ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/antra/`);
    await page.waitForSelector('#antra-form');

    // Outer panel radius
    const antraFormPanelRadius = await page.locator('#antra-form').first().evaluate(el => {
      const panel = el.closest('.golden-glass-card') || el.closest('.m3-card');
      return window.getComputedStyle(panel).borderRadius;
    });
    console.log('Antra form panel border-radius:', antraFormPanelRadius);
    assert.equal(antraFormPanelRadius, '28px', 'Antra outer panels must have 28px radius');

    // Touch targets >= 48px
    const antraUrlBox = await page.locator('#antra-url-input').boundingBox();
    const antraDestBox = await page.locator('#antra-destination-select').boundingBox();
    const antraSubmitBox = await page.locator('#antra-submit-btn').boundingBox();
    assert(antraUrlBox.height >= 48, `Antra URL input height must be >= 48px, got ${antraUrlBox.height}`);
    assert(antraDestBox.height >= 48, `Antra destination select height must be >= 48px, got ${antraDestBox.height}`);
    assert(antraSubmitBox.height >= 48, `Antra submit button height must be >= 48px, got ${antraSubmitBox.height}`);

    // Inner job card radius
    await page.waitForSelector('.job-card');
    const jobCardRadius = await page.locator('.job-card').first().evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Antra job card border-radius:', jobCardRadius);
    assert.equal(jobCardRadius, '14px', 'Antra inner job cards must have 14px radius');

    const antraDesktop = path.join(previewDir, 'antra-desktop-1440.png');
    await page.screenshot({ path: antraDesktop, fullPage: true });
    console.log(`Saved screenshot: ${antraDesktop}`);

    // Mobile check at 390px
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    const antraOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert.equal(antraOverflow, false, 'Antra mobile 390px must not have horizontal overflow');

    const antraMobile = path.join(previewDir, 'antra-mobile-390.png');
    await page.screenshot({ path: antraMobile, fullPage: true });
    console.log(`Saved screenshot: ${antraMobile}`);

    // -----------------------------------------------------------------------
    // ASSERTION 12: Repertoire Finder Acceptance
    // -----------------------------------------------------------------------
    console.log('--- TEST 12: Repertoire Finder Acceptance ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/repertoire/`);
    await page.waitForSelector('.search-panel');

    // Outer panel radius
    const searchPanelRadius = await page.locator('.search-panel').evaluate(el => window.getComputedStyle(el).borderRadius);
    const workbookPanelRadius = await page.locator('.workbook-panel').evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Repertoire panel border-radii:', { searchPanelRadius, workbookPanelRadius });
    assert.equal(searchPanelRadius, '28px', 'Repertoire search panel must have 28px radius');
    assert.equal(workbookPanelRadius, '28px', 'Repertoire workbook panel must have 28px radius');

    // Touch targets >= 48px
    const searchBox = await page.locator('#search').boundingBox();
    const uploadBtnBox = await page.locator('#open-upload').boundingBox();
    const refreshBtnBox = await page.locator('#refresh').boundingBox();
    assert(searchBox.height >= 48, `Repertoire search input height >= 48px, got ${searchBox.height}`);
    assert(uploadBtnBox.height >= 48, `Repertoire upload button height >= 48px, got ${uploadBtnBox.height}`);
    assert(refreshBtnBox.height >= 48, `Repertoire refresh button height >= 48px, got ${refreshBtnBox.height}`);

    // Inner song card radius
    await page.waitForSelector('.song-card');
    const songCardRadius = await page.locator('.song-card').first().evaluate(el => window.getComputedStyle(el).borderRadius);
    console.log('Repertoire song card border-radius:', songCardRadius);
    assert.equal(songCardRadius, '14px', 'Repertoire song cards must have 14px radius');

    const repertoireDesktop = path.join(previewDir, 'repertoire-desktop-1440.png');
    await page.screenshot({ path: repertoireDesktop, fullPage: true });
    console.log(`Saved screenshot: ${repertoireDesktop}`);

    // Mobile check at 390px
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    const repertoireOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert.equal(repertoireOverflow, false, 'Repertoire mobile 390px must not have horizontal overflow');

    const repertoireMobile = path.join(previewDir, 'repertoire-mobile-390.png');
    await page.screenshot({ path: repertoireMobile, fullPage: true });
    console.log(`Saved screenshot: ${repertoireMobile}`);

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
