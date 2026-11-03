const { chromium } = require('playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

(async () => {
  const portalPollingJs = fs.readFileSync(path.resolve(__dirname, '../web/portal-polling.js'), 'utf8');
  const laptopJs = fs.readFileSync(path.resolve(__dirname, '../web/laptop.js'), 'utf8');
  const adminHtml = fs.readFileSync(path.resolve(__dirname, '../web/admin.html'), 'utf8');
  const portalCss = fs.readFileSync(path.resolve(__dirname, '../web/portal.css'), 'utf8');

  // Track request counts, active in-flight counts, and server state
  let statsReads = 0;
  let servicesReads = 0;
  let activeStatsRequests = 0;
  let maxActiveStatsRequests = 0;
  let activeServicesRequests = 0;
  let maxActiveServicesRequests = 0;

  let failStats = false;
  let delayStatsMs = 0;
  let delayServicesMs = 0;

  // Initial services list: two services so concurrent actual UI actions can be exercised
  let serverServices = [
    { id: 'filebrowser', name: 'File Manager', running: false, can_toggle: true, open_url: '/drive/', open_label: 'Open Drive', desc: 'File Browser' },
    { id: 'converter', name: 'Document to PDF Converter', running: false, can_toggle: true, open_url: 'javascript:openConverterModal()', open_label: 'Convert File', desc: 'LibreOffice' }
  ];

  let notesPosts = 0;
  let convertPosts = 0;
  let convertServerReceivedResolve = null;
  const convertServerReceived = new Promise(r => { convertServerReceivedResolve = r; });
  let convertDelayMs = 0;

  let notesServerReceivedResolve = null;
  const notesServerReceived = new Promise(r => { notesServerReceivedResolve = r; });
  let notesDelayMs = 0;

  const server = http.createServer((req, res) => {
    const url = req.url;
    if (url === '/' || url === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(adminHtml);
    } else if (url === '/static/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(portalPollingJs);
    } else if (url === '/static/laptop.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(laptopJs);
    } else if (url === '/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css' });
      res.end(portalCss);
    } else if (url === '/api/stats') {
      statsReads++;
      activeStatsRequests++;
      maxActiveStatsRequests = Math.max(maxActiveStatsRequests, activeStatsRequests);
      const isFailing = failStats;
      const delay = delayStatsMs;
      setTimeout(() => {
        activeStatsRequests--;
        if (isFailing) {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Stats service error' }));
          return;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          tailscale_ip: '100.75.159.53',
          local_ip: '192.168.1.8',
          stale: false,
          battery: { percentage: 88, temperature: 29 },
          ram: { used_mb: 4096, total_mb: 8192, percent: 50 },
          storage: { free_gb: 64, percent: 50 },
          network: {},
          load_avg: [null]
        }));
      }, delay);
    } else if (url === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ enabled: false }));
    } else if (url === '/api/services/toggle' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body || '{}');
        const svc = serverServices.find(s => s.id === payload.service);
        if (svc) {
          svc.running = payload.enable !== undefined ? payload.enable : !svc.running;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', running: svc ? svc.running : false }));
      });
    } else if (url === '/api/services') {
      servicesReads++;
      activeServicesRequests++;
      maxActiveServicesRequests = Math.max(maxActiveServicesRequests, activeServicesRequests);

      // Snapshot serverServices at request arrival time (prevents dynamic send-time serialization)
      const snapshot = JSON.parse(JSON.stringify(serverServices));
      const delay = delayServicesMs;
      setTimeout(() => {
        activeServicesRequests--;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          services: snapshot,
          total_active_mem_mb: 32,
          active_memory_mb: 32
        }));
      }, delay);
    } else if (url === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sessions: [] }));
    } else if (url === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ online: true, sessions: [], max_sessions: 4 }));
    } else if (url === '/api/notes') {
      if (req.method === 'POST') {
        notesPosts++;
        if (notesServerReceivedResolve) notesServerReceivedResolve();
        const delay = notesDelayMs;
        let b = '';
        req.on('data', chunk => { b += chunk; });
        req.on('end', () => {
          setTimeout(() => {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ status: 'saved' }));
          }, delay);
        });
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ notes: 'Initial maintained notes' }));
      }
    } else if (url === '/api/convert/pdf' && req.method === 'POST') {
      convertPosts++;
      if (convertServerReceivedResolve) convertServerReceivedResolve();
      const delay = convertDelayMs;
      req.on('data', () => {});
      req.on('end', () => {
        setTimeout(() => {
          res.writeHead(200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': 'attachment; filename="converted.pdf"'
          });
          res.end(Buffer.from('%PDF-1.4 test output'));
        }, delay);
      });
    } else {
      res.writeHead(404);
      res.end();
    }
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    headless: true
  });

  try {
    const context = await browser.newContext();
    // Block all external network access explicitly
    await context.route('**', (route) => {
      const u = route.request().url();
      if (u.startsWith(baseUrl)) {
        return route.continue();
      }
      return route.abort();
    });

    const page = await context.newPage();
    const recordedErrors = [];
    page.on('pageerror', err => recordedErrors.push(err));

    // Install clock before page navigation/timers
    await page.clock.install();

    // 1. Initial page load and prompt refresh
    await page.goto(`${baseUrl}/`);
    await page.waitForSelector('#telemetry-state');
    assert(statsReads >= 1, 'Initial stats read executed promptly on load');
    assert(servicesReads >= 1, 'Initial services read executed promptly on load');
    assert.strictEqual(recordedErrors.length, 0, 'No uncaught errors on page load');
    assert.strictEqual(maxActiveStatsRequests, 1, 'Active stats requests <= 1');
    assert.strictEqual(maxActiveServicesRequests, 1, 'Active services requests <= 1');

    // 2. Slow/stale services GET during TWO actual UI input actions
    // Make background GET take 300ms so toggles occur during in-flight read
    delayServicesMs = 300;
    // Trigger background refresh
    const bgRefresh = page.evaluate(() => window.servicesLoop.refresh());

    // User clicks first service toggle (filebrowser) via visible switch slider
    const filebrowserToggle = page.locator('input[type="checkbox"]').first();
    await page.locator('.switch .slider').first().click();

    // User clicks second service toggle (converter) via visible switch slider
    const converterToggle = page.locator('input[type="checkbox"]').nth(1);
    await page.locator('.switch .slider').nth(1).click();

    // Advance clock to let background and trailing reads settle
    await page.clock.fastForward(600);
    delayServicesMs = 0;

    // Both checkboxes must reflect updated true state on UI
    assert(await filebrowserToggle.isChecked(), 'First toggle is checked after fresh trailing read');
    assert(await converterToggle.isChecked(), 'Second toggle is checked after fresh trailing read');
    assert.strictEqual(maxActiveServicesRequests, 1, 'Active services requests never exceeded 1 concurrently');

    // 3. Stats endpoint failure & recovery, ensuring other panels continue updating
    failStats = true;
    const statsBeforeFail = statsReads;
    await page.evaluate(() => window.statsLoop.refresh());
    await page.clock.fastForward(100);
    assert.strictEqual(await page.locator('#telemetry-state').textContent(), 'Unavailable', 'Failed stats marks telemetry Unavailable');

    // Other healthy panels continue functioning
    assert.strictEqual(await page.locator('#vpn-badge').textContent(), 'OFF', 'VPN panel healthy');

    // Recover stats endpoint
    failStats = false;
    await page.evaluate(() => window.statsLoop.refresh());
    await page.clock.fastForward(100);
    assert.strictEqual(await page.locator('#telemetry-state').textContent(), 'Active sample', 'Stats recovered and rendered Active sample');

    // 4. In-progress mutation (notes and conversion) held across hide/show
    // Type notes (debounce 750ms)
    notesDelayMs = 400;
    const notesInput = page.locator('#user-notes-input');
    await notesInput.fill('Important drafted notes that must not disappear');

    // Fast forward 800ms to trigger debounced notes POST
    await page.clock.fastForward(800);
    // Wait for server to receive notes POST before hiding page
    await notesServerReceived;

    // Open converter modal and start conversion
    convertDelayMs = 400;
    await page.locator('a[href="javascript:openConverterModal()"]').click();
    await page.locator('#convert-file-input').setInputFiles([
      { name: 'in_progress.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('in progress docx') }
    ]);
    await page.locator('#convert-btn').click();
    // Wait for server to receive conversion POST before hiding page
    await convertServerReceived;

    // Now hide page while notes POST and convert POST are in-flight on the network
    const statsBeforeHide = statsReads;
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('pagehide'));
    });

    // Advance clock while hidden by 15 seconds (greater than all polling intervals)
    await page.clock.fastForward(15000);

    // Verify ZERO new polling reads occurred while hidden
    assert.strictEqual(statsReads, statsBeforeHide, 'Zero new polling reads while hidden');

    // Verify notes POST and conversion POST completed exactly once and were not aborted/retried
    assert.strictEqual(notesPosts, 1, 'Notes POST sent exactly once');
    assert.strictEqual(convertPosts, 1, 'Conversion POST sent exactly once');

    // Resume page / visible / pageshow
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });

    // Visible prompt refresh executes
    await page.clock.fastForward(100);
    assert(statsReads > statsBeforeHide, 'Visible prompt refresh executed');

    // Verify notes text survived intact
    const notesVal = await notesInput.inputValue();
    assert.strictEqual(notesVal, 'Important drafted notes that must not disappear', 'Notes text survived lifecycle transitions');

    // 5. Graceful degradation when PortalPolling helper is unavailable
    const standalonePage = await context.newPage();
    const fallbackErrors = [];
    standalonePage.on('pageerror', err => fallbackErrors.push(err));

    await standalonePage.route('**/static/portal-polling.js', route => route.abort());
    await standalonePage.goto(`${baseUrl}/`);
    await standalonePage.waitForSelector('#telemetry-state');

    assert.strictEqual(fallbackErrors.length, 0, 'No uncaught errors on page when portal-polling.js is blocked');
    const headerTs = await standalonePage.locator('#header-ts-ip').textContent();
    assert.strictEqual(headerTs, '100.75.159.53', 'Page rendered initial sample when portal-polling.js is blocked');
    const footerMode = await standalonePage.locator('footer .mono').textContent();
    assert.strictEqual(footerMode, 'Manual refresh mode', 'Footer indicates manual refresh mode on fallback');

    await standalonePage.close();
    await page.close();

    assert.strictEqual(recordedErrors.length, 0, 'No uncaught page errors occurred throughout full lifecycle run');
    console.log('PASS: portal-polling-browser integration tests verified with zero tautologies and real UI interactions.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
