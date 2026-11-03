const { test, expect } = require('@playwright/test');
const { chromium } = require('playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

(async () => {
  const portalPollingJs = fs.readFileSync(path.resolve(__dirname, '../web/portal-polling.js'), 'utf8');
  const laptopJs = fs.readFileSync(path.resolve(__dirname, '../web/laptop.js'), 'utf8');
  const adminHtml = fs.readFileSync(path.resolve(__dirname, '../web/admin.html'), 'utf8');

  // Track request counts
  let statsReads = 0;
  let servicesReads = 0;
  let delayServicesMs = 0;

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
      res.end('/* portal css */');
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
    } else if (url === '/api/services') {
      servicesReads++;
      const currentDelay = delayServicesMs;
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          services: [
            { id: 'converter', name: 'Document to PDF Converter', running: false, can_toggle: false, open_url: 'javascript:openConverterModal()', open_label: 'Convert File', desc: 'LibreOffice' }
          ],
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
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'saved' }));
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ notes: 'Initial maintained notes' }));
      }
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
    const page = await context.newPage();

    // 1. Initial page load and prompt refresh
    await page.goto(`${baseUrl}/`);
    await page.waitForSelector('#telemetry-state');
    assert(statsReads >= 1, 'Initial stats read should execute promptly on load');
    assert(servicesReads >= 1, 'Initial services read should execute promptly on load');

    // 2. Coalescing and single-flight verification
    // Calling refresh multiple times while one is in-flight returns same promise
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
    assert.strictEqual(coalesceCount, 1, 'Concurrent refresh() calls must coalesce into a single execution');

    // 3. User state preservation across visibilitychange and pagehide/pageshow
    // Enter user notes
    const notesInput = page.locator('#user-notes-input');
    await notesInput.fill('User draft note that must survive lifecycle transitions');

    // Open converter modal and select a file
    await page.locator('a[href="javascript:openConverterModal()"]').click();
    await page.locator('#convert-file-input').setInputFiles([
      { name: 'test_lifecycle.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('lifecycle test') }
    ]);
    const summaryBefore = await page.locator('#convert-file-summary').textContent();
    assert(summaryBefore.includes('test_lifecycle.docx') || summaryBefore.includes('1 file selected'), 'Converter file selection rendered');

    // Pause / document hidden
    const statsBeforeHide = statsReads;
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('pagehide'));
    });

    // Wait 200ms and verify no new reads started while hidden
    await page.waitForTimeout(200);
    assert.strictEqual(statsReads, statsBeforeHide, 'Zero new polling reads while hidden/pagehide');

    // Resume / visible / pageshow
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });

    // Verify prompt refresh happened
    await page.waitForFunction((prev) => window.statsReads !== undefined || true);
    await page.waitForTimeout(100);
    assert(statsReads > statsBeforeHide, 'Visible/pageshow promptly restarts refresh');

    // Verify user state survived untouched
    const notesVal = await notesInput.inputValue();
    assert.strictEqual(notesVal, 'User draft note that must survive lifecycle transitions', 'Notes draft must survive lifecycle events');
    const summaryAfter = await page.locator('#convert-file-summary').textContent();
    assert.strictEqual(summaryAfter, summaryBefore, 'Converter file selection must survive lifecycle events');

    // Close converter modal
    await page.locator('button:has-text("Cancel")').click();

    // 4. Deadline / abort isolation test
    const abortHandled = await page.evaluate(async () => {
      let aborted = false;
      const loop = window.PortalPolling.createPolling(async (signal) => {
        await new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => {
            aborted = true;
            reject(new Error('aborted'));
          });
        });
      }, 5000, { deadlineMs: 50 });
      await new Promise(r => setTimeout(r, 120));
      loop.dispose();
      return aborted;
    });
    assert(abortHandled, 'Background reads are deadline-bounded and aborted via AbortSignal');

    // 5. Fault isolation: slow or failing endpoint does not block others
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
    assert(faultIsolation.featureAFinished && faultIsolation.featureBFailHandled, 'Failure in one loop does not impact independent loops');

    // 6. Graceful degradation when PortalPolling module is unavailable
    const standalonePage = await context.newPage();
    // Block portal-polling.js to verify page loads and does not throw uncaught error
    await standalonePage.route('**/static/portal-polling.js', route => route.abort());
    await standalonePage.goto(`${baseUrl}/`);
    await standalonePage.waitForSelector('#telemetry-state');
    const headerTs = await standalonePage.locator('#header-ts-ip').textContent();
    assert.strictEqual(headerTs, '100.75.159.53', 'Page gracefully initializes even if portal-polling.js fails to load');
    await standalonePage.close();

    console.log('PASS: portal-polling lifecycle, coalescing, isolation, and user state preservation verified.');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
