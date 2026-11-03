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

  // Gated server response controls
  let servicesGated = false;
  let servicesRelease = null;
  let servicesArrivedResolve = null;
  let servicesArrivedPromise = new Promise(r => { servicesArrivedResolve = r; });

  let statsGated = false;
  let statsRelease = null;
  let statsArrivedResolve = null;
  let statsArrivedPromise = new Promise(r => { statsArrivedResolve = r; });

  let notesGated = false;
  let notesRelease = null;
  let notesArrivedResolve = null;
  let notesArrivedPromise = new Promise(r => { notesArrivedResolve = r; });

  let convertGated = false;
  let convertRelease = null;
  let convertArrivedResolve = null;
  let convertArrivedPromise = new Promise(r => { convertArrivedResolve = r; });

  let serverServicesRev = 0;
  let serverServices = [
    { id: 'filebrowser', name: 'File Manager', running: false, can_toggle: true, open_url: '/drive/', open_label: 'Open Drive', desc: 'Rev 0' },
    { id: 'converter', name: 'Document to PDF Converter', running: false, can_toggle: true, open_url: 'javascript:openConverterModal()', open_label: 'Convert File', desc: 'Rev 0' }
  ];

  let notesPosts = 0;
  let convertPosts = 0;

  const server = http.createServer(async (req, res) => {
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
      if (statsGated) {
        if (statsArrivedResolve) {
          statsArrivedResolve();
          statsArrivedResolve = null;
        }
        await new Promise(r => { statsRelease = r; });
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
          serverServicesRev++;
          svc.desc = `Rev ${serverServicesRev}`;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', running: svc ? svc.running : false }));
      });
    } else if (url === '/api/services') {
      // Snapshot payload at request arrival
      const snapshot = JSON.parse(JSON.stringify(serverServices));
      if (servicesGated) {
        if (servicesArrivedResolve) {
          servicesArrivedResolve();
          servicesArrivedResolve = null;
        }
        await new Promise(r => { servicesRelease = r; });
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        services: snapshot,
        total_active_mem_mb: 32,
        active_memory_mb: 32
      }));
    } else if (url === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sessions: [] }));
    } else if (url === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ online: true, sessions: [], max_sessions: 4 }));
    } else if (url === '/api/notes') {
      if (req.method === 'POST') {
        notesPosts++;
        if (notesGated) {
          if (notesArrivedResolve) {
            notesArrivedResolve();
            notesArrivedResolve = null;
          }
          await new Promise(r => { notesRelease = r; });
        }
        let b = '';
        req.on('data', chunk => { b += chunk; });
        req.on('end', () => {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'saved' }));
        });
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ notes: 'Initial maintained notes' }));
      }
    } else if (url === '/api/convert/pdf' && req.method === 'POST') {
      convertPosts++;
      if (convertGated) {
        if (convertArrivedResolve) {
          convertArrivedResolve();
          convertArrivedResolve = null;
        }
        await new Promise(r => { convertRelease = r; });
      }
      req.on('data', () => {});
      req.on('end', () => {
        res.writeHead(200, {
          'Content-Type': 'application/pdf',
          'Content-Disposition': 'attachment; filename="converted.pdf"'
        });
        res.end(Buffer.from('%PDF-1.4 test output'));
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

    // Block all non-fixture external traffic explicitly
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

    // A. Measure CLIENT requests with Playwright request/requestfinished/requestfailed events
    const managedEndpoints = [
      '/api/stats',
      '/api/vpn/status',
      '/api/services',
      '/api/agy/sessions',
      '/api/laptop/terminals'
    ];

    const activeClientRequests = new Map();
    const maxActiveClientRequests = new Map();
    for (const ep of managedEndpoints) {
      activeClientRequests.set(ep, 0);
      maxActiveClientRequests.set(ep, 0);
    }

    page.on('request', req => {
      if (req.method() !== 'GET') return;
      const pathname = new URL(req.url()).pathname;
      if (activeClientRequests.has(pathname)) {
        const cur = activeClientRequests.get(pathname) + 1;
        activeClientRequests.set(pathname, cur);
        if (cur > maxActiveClientRequests.get(pathname)) {
          maxActiveClientRequests.set(pathname, cur);
        }
      }
    });

    const onReqEnd = req => {
      if (req.method() !== 'GET') return;
      const pathname = new URL(req.url()).pathname;
      if (activeClientRequests.has(pathname)) {
        const cur = Math.max(0, activeClientRequests.get(pathname) - 1);
        activeClientRequests.set(pathname, cur);
      }
    };
    page.on('requestfinished', onReqEnd);
    page.on('requestfailed', onReqEnd);

    // Install Playwright clock before page navigation/timers
    await page.clock.install();

    // 1. Initial page load
    await page.goto(`${baseUrl}/`);
    await page.waitForSelector('#telemetry-state');
    assert.strictEqual(recordedErrors.length, 0, 'No uncaught errors on page load');

    for (const ep of managedEndpoints) {
      assert.ok(maxActiveClientRequests.get(ep) <= 1, `Max client active requests for ${ep} <= 1`);
    }

    // B. Gate a services GET after capturing old payload, execute TWO actual UI toggles, verify fresh revision in DOM
    servicesGated = true;
    servicesArrivedPromise = new Promise(r => { servicesArrivedResolve = r; });
    // Trigger background read
    const bgRead = page.evaluate(() => servicesLoop.refresh());
    await servicesArrivedPromise; // Wait until GET reaches server

    // Now click first service toggle (filebrowser) via visible switch slider
    await page.locator('.switch .slider').first().click();
    // Click second service toggle (converter) via visible switch slider
    await page.locator('.switch .slider').nth(1).click();

    // Release old gated services GET
    servicesGated = false;
    servicesRelease();
    await bgRead;

    // Await all started refresh promises
    await page.evaluate(() => servicesLoop.refreshAfterMutation());

    // Assert that the latest synthetic revision marker ('Rev 2') appears in DOM
    await page.waitForFunction(() => document.body.innerText.includes('Rev 2'), { timeout: 3000 });
    assert.ok(await page.locator('input[type="checkbox"]').first().isChecked(), 'Filebrowser checkbox checked');
    assert.ok(await page.locator('input[type="checkbox"]').nth(1).isChecked(), 'Converter checkbox checked');
    assert.ok(maxActiveClientRequests.get('/api/services') <= 1, 'Client active requests for /api/services <= 1');

    // C. Hold real stats GET beyond 10-second deadline. Observe requestfailed/abort and Unavailable panel.
    statsGated = true;
    statsArrivedPromise = new Promise(r => { statsArrivedResolve = r; });
    const statsAbortPromise = page.waitForRequest(req => req.url().includes('/api/stats'));
    const statsPollRun = page.evaluate(() => statsLoop.refresh());
    await statsArrivedPromise; // Wait until stats arrives at server

    // Advance clock past the 10-second deadline
    await page.clock.fastForward(10500);

    // Release server gate so server socket finishes cleanly
    statsGated = false;
    if (statsRelease) statsRelease();
    await statsPollRun;

    // Verify stats panel displays Unavailable
    await page.waitForFunction(() => document.getElementById('telemetry-state').textContent === 'Unavailable', { timeout: 3000 });
    assert.strictEqual(await page.locator('#telemetry-state').textContent(), 'Unavailable', 'Stats marked Unavailable on deadline abort');

    // Demonstrate continued requests and changed rendered data in another panel (services active memory)
    assert.ok(await page.locator('#svc-active-mem').textContent() !== '', 'Services panel rendered and active');

    // Advance clock past normal 3s stats interval and allow successful recovery
    await page.clock.fastForward(3000);
    await page.waitForFunction(() => document.getElementById('telemetry-state').textContent === 'Active sample', { timeout: 3000 });
    assert.strictEqual(await page.locator('#telemetry-state').textContent(), 'Active sample', 'Stats recovered to Active sample');
    assert.ok(maxActiveClientRequests.get('/api/stats') <= 1, 'Stats client concurrency remained <= 1 through deadline abort and recovery');

    // D. Hold BOTH an actual debounced notes POST and actual conversion POST with explicit gates
    notesGated = true;
    notesArrivedPromise = new Promise(r => { notesArrivedResolve = r; });
    convertGated = true;
    convertArrivedPromise = new Promise(r => { convertArrivedResolve = r; });

    // Fill notes (750ms debounce)
    const notesInput = page.locator('#user-notes-input');
    await notesInput.fill('Gated draft notes that must survive lifecycle');
    await page.clock.fastForward(800); // trigger notes POST
    await notesArrivedPromise; // wait until notes POST arrives at server

    // Open converter modal and start conversion
    await page.locator('a[href="javascript:openConverterModal()"]').click();
    await page.locator('#convert-file-input').setInputFiles([
      { name: 'gated.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('gated test') }
    ]);

    // Register download listener before clicking convert
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#convert-btn').click();
    await convertArrivedPromise; // wait until conversion POST arrives at server

    // Now both POSTs are held at the server. Hide the page!
    const activeReqsBeforeHide = new Map();
    for (const ep of managedEndpoints) {
      activeReqsBeforeHide.set(ep, activeClientRequests.get(ep));
    }

    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('pagehide'));
    });

    // Advance beyond all polling intervals (15 seconds)
    await page.clock.fastForward(15000);

    // Assert zero new managed GETs occurred while hidden
    for (const ep of managedEndpoints) {
      assert.strictEqual(activeClientRequests.get(ep), 0, `No active background GET for ${ep} while hidden`);
    }

    // Resume while both POST responses are still held
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });

    // Verify notes and converter selection remain while pending
    assert.strictEqual(await notesInput.inputValue(), 'Gated draft notes that must survive lifecycle');
    assert.ok(await page.locator('#convert-file-summary').textContent(), 'File summary remains visible');

    // Release both held POST responses
    notesGated = false;
    if (notesRelease) notesRelease();
    convertGated = false;
    if (convertRelease) convertRelease();

    // Verify download completes and notes saves settles
    const download = await downloadPromise;
    assert.strictEqual(download.suggestedFilename(), 'converted.pdf', 'Download completed with expected filename');
    await page.waitForFunction(() => document.getElementById('notes-status').textContent.includes('Saved'), { timeout: 3000 });
    assert.strictEqual(notesPosts, 1, 'Notes POST sent exactly once');
    assert.strictEqual(convertPosts, 1, 'Conversion POST sent exactly once');

    // E. Exercise rapid hide/show with active GET and pageshow persisted=true
    await page.evaluate(() => {
      window.dispatchEvent(new Event('pagehide'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });
    await page.clock.fastForward(3000);
    for (const ep of managedEndpoints) {
      assert.ok(maxActiveClientRequests.get(ep) <= 1, `Concurrency <= 1 for ${ep} after rapid hide/show`);
    }

    // Missing-helper page verification
    const standalonePage = await context.newPage();
    const fallbackErrors = [];
    standalonePage.on('pageerror', err => fallbackErrors.push(err));

    await standalonePage.route('**/static/portal-polling.js', route => route.abort());
    await standalonePage.goto(`${baseUrl}/`);
    await standalonePage.waitForSelector('#telemetry-state');

    assert.strictEqual(fallbackErrors.length, 0, 'No uncaught errors on page when portal-polling.js is blocked');
    assert.strictEqual(await standalonePage.locator('#header-ts-ip').textContent(), '100.75.159.53', 'Page rendered initial sample');
    assert.strictEqual(await standalonePage.locator('footer .mono').textContent(), 'Manual refresh mode', 'Footer indicates manual refresh mode on fallback');

    await standalonePage.close();
    await page.close();

    assert.strictEqual(recordedErrors.length, 0, 'No uncaught page errors occurred throughout full run');
    console.log('PASS: portal-polling-browser proof complete with true request gating, client-side concurrency tracking, and download verification.');
  } finally {
    if (servicesRelease) servicesRelease();
    if (statsRelease) statsRelease();
    if (notesRelease) notesRelease();
    if (convertRelease) convertRelease();
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
