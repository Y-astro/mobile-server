/**
 * =========================================================================
 * SIDEBAR SERVICE CONTROLS BROWSER ACCEPTANCE SUITE
 * Exercises:
 * 1. Grid isolation: NO agy or laptop cards in services grid; exactly 1 right
 *    AGY panel and 1 right Laptop panel.
 * 2. 48px accessible switches and status badges on right panels.
 * 3. AGY service toggle off/on: explicit enable false/true POST, guards New
 *    instance and session open links without closing sessions.
 * 4. Laptop gateway toggle off/on: explicit enable false/true POST, guards New
 *    and Reconnect, labels Off, broker poll cannot re-enable while gateway off,
 *    NO DELETE fired on toggle.
 * 5. Reorder & localStorage persistence with hidden IDs safely ignored.
 * 6. Responsive 1280px 3-column and 390px mobile 0-overflow.
 * 7. Error/403 rollback and focus/in-flight poll protection.
 * 8. Family role boundary: zero sidebar service controls.
 * =========================================================================
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');

const rootDir = path.resolve(__dirname, '..');
const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
const themeTokensCss = fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8');
const appearanceCss = fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8');
const appearanceJs = fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8');
const portalPollingJs = fs.readFileSync(path.join(rootDir, 'web/portal-polling.js'), 'utf8');
const laptopJs = fs.readFileSync(path.join(rootDir, 'web/laptop.js'), 'utf8');

const initialMockServices = [
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
  let servicesState = JSON.parse(JSON.stringify(initialMockServices));
  let toggleLog = [];
  let deleteLog = [];
  let simulateToggleFailure = false;
  let simulateToggleForbidden = false;

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1:8000');
    const pathname = url.pathname;

    if (pathname === '/' || pathname === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(adminHtml);
    }
    if (pathname === '/family') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(familyHtml);
    }
    if (pathname === '/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css' });
      return res.end(portalCss);
    }
    if (pathname === '/theme-tokens.css') {
      res.writeHead(200, { 'Content-Type': 'text/css' });
      return res.end(themeTokensCss);
    }
    if (pathname === '/appearance.css') {
      res.writeHead(200, { 'Content-Type': 'text/css' });
      return res.end(appearanceCss);
    }
    if (pathname === '/appearance.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript' });
      return res.end(appearanceJs);
    }
    if (pathname === '/static/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript' });
      return res.end(portalPollingJs);
    }
    if (pathname === '/static/laptop.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript' });
      return res.end(laptopJs);
    }
    if (pathname === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        battery: { percentage: 88, temperature: 31 },
        memory: { active_memory_mb: 320.5, total_active_mem_mb: 320.5 },
        cpu: { load: 12 },
        local_ip: '192.168.1.50',
        tailscale_ip: '100.75.159.53'
      }));
    }
    if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        services: servicesState,
        active_memory_mb: 320.5,
        total_active_mem_mb: 320.5
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
          svc.status_badge = svc.running ? 'Active (15.0 MB)' : 'Disabled (0 MB)';
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
          { id: 'primary', name: 'main', is_primary: true }
        ]
      }));
    }
    if (pathname === '/api/laptop/terminals') {
      if (req.method === 'POST') {
        res.writeHead(201, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ id: 'a'.repeat(32) }));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        online: true,
        sessions: [{ id: '1234567890abcdef1234567890abcdef' }],
        max_sessions: 4
      }));
    }
    if (pathname.startsWith('/api/laptop/terminals/') && req.method === 'DELETE') {
      deleteLog.push(pathname);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ closed: true }));
    }
    if (pathname === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ enabled: false }));
    }
    if (pathname === '/api/notes') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ content: 'test note' }));
    }
    if (pathname === '/api/wallpapers/current') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ current_wallpaper: null }));
    }
    if (pathname === '/api/appearance') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ theme: 'dark', font_size: 'normal' }));
    }

    res.writeHead(404);
    res.end();
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

    // -------------------------------------------------------------
    // TEST 1: GRID CONFINEMENT & RIGHT SIDEBAR PANELS
    // -------------------------------------------------------------
    console.log('--- TEST 1: Services Grid & Right Sidebar Panels ---');
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${baseUrl}/`);
    await page.waitForSelector('#services-grid .m3-card');

    // Verify Services Grid has NO agy and NO laptop_terminal cards
    const gridCards = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    console.log('Services Grid Cards:', gridCards);
    assert.equal(gridCards.includes('agy'), false, 'Grid MUST NOT render agy card');
    assert.equal(gridCards.includes('laptop_terminal'), false, 'Grid MUST NOT render laptop_terminal card');
    assert.equal(gridCards.length, 10, 'Grid must render exactly 10 visible cards');

    // Verify exactly ONE right AGY panel and ONE right Laptop panel
    const agyPanels = await page.locator('section[aria-labelledby="agy-heading"]').count();
    const laptopPanels = await page.locator('section[aria-labelledby="laptop-heading"]').count();
    assert.equal(agyPanels, 1, 'Must have exactly ONE right AGY panel');
    assert.equal(laptopPanels, 1, 'Must have exactly ONE right Laptop panel');

    // Verify Accessible 48px switches and badges
    const agyToggle = page.locator('#agy-service-toggle');
    const laptopToggle = page.locator('#laptop-service-toggle');
    const agySwitch = page.locator('label:has(#agy-service-toggle)');
    const laptopSwitch = page.locator('label:has(#laptop-service-toggle)');
    assert(await agySwitch.isVisible(), 'AGY service switch must be visible');
    assert(await laptopSwitch.isVisible(), 'Laptop service switch must be visible');
    assert(await agyToggle.isChecked(), 'AGY toggle should initially be checked');
    assert(await laptopToggle.isChecked(), 'Laptop toggle should initially be checked');

    const agySwitchRect = await agySwitch.boundingBox();
    const laptopSwitchRect = await laptopSwitch.boundingBox();
    assert(agySwitchRect.height >= 48, `AGY switch hit target height must be >= 48px, got ${agySwitchRect.height}`);
    assert(agySwitchRect.width >= 48, `AGY switch hit target width must be >= 48px, got ${agySwitchRect.width}`);
    assert(laptopSwitchRect.height >= 48, `Laptop switch hit target height must be >= 48px, got ${laptopSwitchRect.height}`);
    assert(laptopSwitchRect.width >= 48, `Laptop switch hit target width must be >= 48px, got ${laptopSwitchRect.width}`);

    // Verify initial badge text
    const agyBadge = await page.locator('#agy-service-status-badge').textContent();
    const laptopBadge = await page.locator('#laptop-service-status-badge').textContent();
    console.log('Initial Badges:', { agyBadge, laptopBadge });
    assert(agyBadge.includes('Active'), 'AGY badge must show Active initially');
    assert(laptopBadge.includes('Active'), 'Laptop badge must show Active initially');

    // -------------------------------------------------------------
    // TEST 2: RESPONSIVE LAYOUT & ZERO OVERFLOW
    // -------------------------------------------------------------
    console.log('--- TEST 2: Responsive Layout (1280px & 390px) ---');
    await page.setViewportSize({ width: 1280, height: 800 });
    const rightColWidth = await page.evaluate(() => {
      const col = document.querySelectorAll('.workstation-col')[1];
      return col.getBoundingClientRect().width;
    });
    assert(rightColWidth >= 330 && rightColWidth <= 380, `Right column width should be ~340-360px at 1280px, got ${rightColWidth}`);
    const noOverflow1280 = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    assert(noOverflow1280, '1280px viewport must have 0 horizontal overflow');

    await page.setViewportSize({ width: 390, height: 844 });
    const noOverflow390 = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    assert(noOverflow390, '390px mobile viewport must have 0 horizontal overflow');

    // Restore desktop
    await page.setViewportSize({ width: 1440, height: 900 });

    // -------------------------------------------------------------
    // TEST 3: AGY SERVICE TOGGLE (OFF -> ON)
    // -------------------------------------------------------------
    console.log('--- TEST 3: AGY Service Toggle ---');
    await page.waitForSelector('#agy-sessions-container a[href^="/agy/"]');
    const sessionLink = page.locator('#agy-sessions-container a[href^="/agy/"]').first();
    assert.equal(await sessionLink.getAttribute('aria-disabled'), null, 'Session link active when AGY on');

    // Click AGY switch to turn OFF
    toggleLog = [];
    await agySwitch.click();
    await page.waitForFunction(() => document.querySelector('#agy-new').disabled);
    assert.equal(toggleLog.length, 1);
    assert.deepEqual(toggleLog[0], { service: 'agy', enable: false });

    // Verify New AGY button is disabled and session link is disabled
    assert(await page.locator('#agy-new').isDisabled(), 'New AGY button must be disabled when AGY service off');
    assert.equal(await sessionLink.getAttribute('aria-disabled'), 'true', 'Session link must be aria-disabled when service off');
    const agyBadgeOff = await page.locator('#agy-service-status-badge').textContent();
    assert(agyBadgeOff.includes('Disabled'), 'AGY badge must show Disabled');

    // Click AGY switch to turn ON
    toggleLog = [];
    await agySwitch.click();
    await page.waitForFunction(() => !document.querySelector('#agy-new').disabled);
    assert.equal(toggleLog.length, 1);
    assert.deepEqual(toggleLog[0], { service: 'agy', enable: true });
    assert.equal(await page.locator('#agy-new').isDisabled(), false, 'New AGY button must re-enable when service on');
    assert.equal(await sessionLink.getAttribute('aria-disabled'), null, 'Session link re-enabled');

    // -------------------------------------------------------------
    // TEST 4: LAPTOP TERMINAL GATEWAY TOGGLE & POLL ISOLATION
    // -------------------------------------------------------------
    console.log('--- TEST 4: Laptop Terminal Gateway Toggle ---');
    await page.waitForSelector('#laptop-sessions a[href^="/laptop/"]');
    const laptopReconnect = page.locator('#laptop-sessions a').first();
    assert.equal(await page.locator('#laptop-new').isDisabled(), false, 'New Terminal enabled when online and gateway on');
    assert.equal(await laptopReconnect.getAttribute('aria-disabled'), null, 'Reconnect enabled when gateway on');

    // Click Laptop switch to turn OFF
    toggleLog = [];
    deleteLog = [];
    await laptopSwitch.click();
    await page.waitForFunction(() => !document.querySelector('#laptop-service-toggle').checked);
    assert.equal(toggleLog.length, 1);
    assert.deepEqual(toggleLog[0], { service: 'laptop_terminal', enable: false });
    assert.equal(deleteLog.length, 0, 'Turning off gateway must NEVER fire DELETE to sessions');

    // Verify controls disabled and status clearly labels Off
    await page.waitForFunction(() => document.querySelector('#laptop-new').disabled);
    const laptopStatusOff = await page.locator('#laptop-status').textContent();
    console.log('Laptop Status after Off:', laptopStatusOff);
    assert(laptopStatusOff.toLowerCase().includes('off'), 'Laptop status must clearly label Off');
    assert.equal(await laptopReconnect.getAttribute('aria-disabled'), 'true', 'Reconnect must be aria-disabled');
    assert.equal(await laptopReconnect.getAttribute('href'), null, 'Reconnect href removed while gateway off');

    // Trigger services/laptop poll while off - must NOT re-enable
    await page.evaluate(() => fetchServices());
    await page.waitForTimeout(200);
    assert(await page.locator('#laptop-new').isDisabled(), 'Laptop poll must NOT re-enable New button while gateway off');
    assert.equal(await page.locator('#laptop-service-toggle').isChecked(), false, 'Toggle must stay off');

    // Click Laptop switch to turn ON
    toggleLog = [];
    await laptopSwitch.click();
    await page.waitForFunction(() => document.querySelector('#laptop-service-toggle').checked);
    assert.equal(toggleLog.length, 1);
    assert.deepEqual(toggleLog[0], { service: 'laptop_terminal', enable: true });
    await page.waitForFunction(() => !document.querySelector('#laptop-new').disabled);
    assert.equal(await page.locator('#laptop-new').isDisabled(), false, 'New Terminal re-enabled');
    assert(await laptopReconnect.getAttribute('href'), '/laptop/?arg=1234567890abcdef1234567890abcdef');

    // -------------------------------------------------------------
    // TEST 5: PERSISTED ORDERING WITH HIDDEN IDS IN LOCALSTORAGE
    // -------------------------------------------------------------
    console.log('--- TEST 5: Persisted Ordering with Hidden IDs ---');
    // Seed localStorage with stored order containing hidden IDs
    await page.evaluate(() => {
      localStorage.setItem('mobile.services.order.v1', JSON.stringify(['filebrowser', 'agy', 'suwayomi', 'laptop_terminal', 'vaultwarden']));
    });
    await page.reload();
    await page.waitForSelector('#services-grid .m3-card');
    const restoredCards = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('#services-grid .m3-card')).map(c => c.getAttribute('data-service-id'));
    });
    console.log('Cards after reload with stored order:', restoredCards);
    assert.equal(restoredCards.includes('agy'), false, 'Grid reconciliation must NEVER re-add agy');
    assert.equal(restoredCards.includes('laptop_terminal'), false, 'Grid reconciliation must NEVER re-add laptop_terminal');
    assert.equal(restoredCards[0], 'filebrowser');
    assert.equal(restoredCards[1], 'suwayomi');
    assert.equal(restoredCards[2], 'vaultwarden');

    // -------------------------------------------------------------
    // TEST 6: TOGGLE ERROR ROLLBACK & 403 HANDLING
    // -------------------------------------------------------------
    console.log('--- TEST 6: Error Rollback & Non-Stuck Controls ---');
    // 1. Simulate 500 error
    simulateToggleFailure = true;
    page.once('dialog', dialog => dialog.accept());
    await agySwitch.click();
    await page.waitForTimeout(200);
    assert(await agyToggle.isChecked(), 'AGY toggle must revert to checked on error');
    assert.equal(await agyToggle.isDisabled(), false, 'AGY toggle must not be stuck disabled');
    simulateToggleFailure = false;

    // 2. Simulate 403 Forbidden
    simulateToggleForbidden = true;
    page.once('dialog', dialog => dialog.accept());
    await laptopSwitch.click();
    await page.waitForTimeout(200);
    assert(await laptopToggle.isChecked(), 'Laptop toggle must revert on 403');
    assert.equal(await laptopToggle.isDisabled(), false, 'Laptop toggle must not be stuck disabled');
    simulateToggleForbidden = false;

    // -------------------------------------------------------------
    // TEST 7: FAMILY ROLE BOUNDARY
    // -------------------------------------------------------------
    console.log('--- TEST 7: Family Role Boundary ---');
    await page.goto(`${baseUrl}/family`);
    assert.equal(await page.locator('#agy-service-toggle').count(), 0, 'Family template must have NO agy service toggle');
    assert.equal(await page.locator('#laptop-service-toggle').count(), 0, 'Family template must have NO laptop service toggle');
    assert.equal(await page.locator('#services-grid').count(), 0, 'Family template must have NO services grid');

    console.log('ALL SIDEBAR SERVICE CONTROLS ACCEPTANCE CHECKS PASSED');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(err => {
  console.error(err);
  process.exit(1);
});
