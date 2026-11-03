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

  // Track request counts and server states
  let statsReads = 0;
  let servicesReads = 0;
  let delayServicesMs = 0;
  let serverServices = [
    { id: 'converter', name: 'Document to PDF Converter', running: false, can_toggle: true, open_url: 'javascript:openConverterModal()', open_label: 'Convert File', desc: 'LibreOffice' }
  ];

  let notesPosts = 0;
  let convertPosts = 0;
  let convertFinishedPromise = null;
  let resolveConvert = null;

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
      serverServices[0].running = !serverServices[0].running;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', running: serverServices[0].running }));
    } else if (url === '/api/services') {
      servicesReads++;
      const currentDelay = delayServicesMs;
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          services: serverServices,
          total_active_mem_mb: 32,
          active_memory_mb: 32
        }));
      }, currentDelay);
    } else if (url === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sessions: [] }));
    } else if (url === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ online: true, sessions: [], max_sessions: 4 }));
    } else if (url === '/api/notes') {
      if (req.method === 'POST') {
        notesPosts++;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'saved' }));
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ notes: 'Initial maintained notes' }));
      }
    } else if (url === '/api/convert/pdf' && req.method === 'POST') {
      convertPosts++;
      req.on('data', () => {});
      req.on('end', () => {
        setTimeout(() => {
          res.writeHead(200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': 'attachment; filename="converted.pdf"'
          });
          res.end(Buffer.from('%PDF-1.4 test output'));
          if (resolveConvert) resolveConvert();
        }, 300);
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
    assert(statsReads >= 1, 'Initial stats read should execute promptly on load');
    assert(servicesReads >= 1, 'Initial services read should execute promptly on load');
    assert.strictEqual(recordedErrors.length, 0, 'No uncaught errors on page load');

    // 2. Coalescing and single-flight verification
    const coalesceCount = await page.evaluate(async () => {
      let runs = 0;
      const loop = window.PortalPolling.createPolling(async () => {
        runs++;
        await new Promise(r => setTimeout(r, 200));
      }, 5000);
      const p1 = loop.refresh();
      const p2 = loop.refresh();
      const p3 = loop.refresh();
      await Promise.all([p1, p2, p3]);
      loop.dispose();
      return runs;
    });
    assert.strictEqual(coalesceCount, 1, 'Concurrent refresh() calls coalesce into a single execution');

    // 3. Mutation freshness and trailing read test
    // Toggle service: should trigger fresh trailing read
    const servicesBeforeToggle = servicesReads;
    await page.evaluate(async () => {
      await toggleService('converter', null);
    });
    assert(servicesReads > servicesBeforeToggle, 'Toggle service triggers fresh readAfterMutation');

    // 4. In-progress mutation and user input preservation across hide/resume
    // Type notes (debounce 750ms)
    const notesInput = page.locator('#user-notes-input');
    await notesInput.fill('Important drafted notes that must not disappear');

    // Select converter file
    await page.locator('a[href="javascript:openConverterModal()"]').click();
    await page.locator('#convert-file-input').setInputFiles([
      { name: 'in_progress.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('in progress docx') }
    ]);
    assert(await page.locator('#convert-file-summary').textContent(), 'File summary visible');

    // Start conversion
    convertFinishedPromise = new Promise(r => { resolveConvert = r; });
    await page.locator('#convert-btn').click();

    // Now hide page while conversion and notes autosave are in progress
    const statsBeforeHide = statsReads;
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('pagehide'));
    });

    // Advance clock by 10 seconds while hidden
    await page.clock.fastForward(10000);

    // Verify 0 new polling reads occurred while hidden
    assert.strictEqual(statsReads, statsBeforeHide, 'Zero new polling reads while hidden/pagehide even after interval elapsed');

    // Wait for in-progress conversion to finish
    await convertFinishedPromise;
    assert.strictEqual(convertPosts, 1, 'Conversion POST completed once and was not canceled or duplicated');

    // Resume page / visible / pageshow
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });

    // Wait for the async fetch to hit the synthetic server
    await page.waitForFunction((prev) => window.statsReads !== undefined || true);
    for (let i = 0; i < 50; i++) {
      if (statsReads > statsBeforeHide) break;
      await new Promise(r => setTimeout(r, 20));
    }

    // Prompt refresh should trigger immediately on becoming visible
    assert(statsReads > statsBeforeHide, 'Visible/pageshow promptly restarts refresh');

    // Advance clock past notes debounce (750ms) to ensure auto-save posts
    await page.clock.fastForward(1000);
    assert(notesPosts >= 1, 'Notes auto-save completed');

    // Verify user notes survived untouched
    const notesVal = await notesInput.inputValue();
    assert.strictEqual(notesVal, 'Important drafted notes that must not disappear', 'Notes draft survived lifecycle events');

    // 5. Fault isolation: failing endpoint does not break healthy updates
    const faultIsolation = await page.evaluate(async () => {
      let featureAFinished = false;
      let featureBFailHandled = false;

      const loopA = window.PortalPolling.createPolling(async () => {
        featureAFinished = true;
      }, 1000);

      const loopB = window.PortalPolling.createPolling(async () => {
        featureBFailHandled = true;
        throw new Error('Endpoint failure');
      }, 1000);

      await Promise.all([loopA.refresh(), loopB.refresh()]);
      loopA.dispose();
      loopB.dispose();
      return { featureAFinished, featureBFailHandled };
    });
    assert(faultIsolation.featureAFinished && faultIsolation.featureBFailHandled, 'Failing endpoint does not break healthy loops');

    // 6. Graceful degradation when PortalPolling module is unavailable
    const standalonePage = await context.newPage();
    const fallbackErrors = [];
    standalonePage.on('pageerror', err => fallbackErrors.push(err));

    // Block portal-polling.js
    await standalonePage.route('**/static/portal-polling.js', route => route.abort());
    await standalonePage.goto(`${baseUrl}/`);
    await standalonePage.waitForSelector('#telemetry-state');

    assert.strictEqual(fallbackErrors.length, 0, 'No uncaught errors on page when portal-polling.js is blocked');
    const headerTs = await standalonePage.locator('#header-ts-ip').textContent();
    assert.strictEqual(headerTs, '100.75.159.53', 'Page rendered initial sample when portal-polling.js is blocked');
    const footerMode = await standalonePage.locator('footer .mono').textContent();
    assert.strictEqual(footerMode, 'Manual refresh mode', 'Footer truthfully indicates manual refresh mode on fallback');

    await standalonePage.close();
    await page.close();

    console.log('PASS: portal-polling lifecycle, coalescing, isolation, fresh trailing read, and degradation verified.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
