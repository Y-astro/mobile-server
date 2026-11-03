const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

(async () => {
  const adminHtmlPath = path.resolve(__dirname, '../web/admin.html');
  const adminHtml = fs.readFileSync(adminHtmlPath, 'utf8');

  const portalCssPath = path.resolve(__dirname, '../web/portal.css');
  const portalCss = fs.existsSync(portalCssPath) ? fs.readFileSync(portalCssPath, 'utf8') : '';

  const laptopJsPath = path.resolve(__dirname, '../web/laptop.js');
const portalPollingJsPath = path.resolve(__dirname, '../web/portal-polling.js');
const portalPollingJs = fs.readFileSync(portalPollingJsPath, 'utf8');
  const laptopJs = fs.existsSync(laptopJsPath) ? fs.readFileSync(laptopJsPath, 'utf8') : '';

  let currentServicesPayload = {
    services: [
      {
        id: 'filebrowser',
        name: 'Cloud Storage & Media',
        category: 'Storage & Media',
        running: true,
        ondemand: false,
        pid: 1234,
        rss_mb: 24.5,
        status_badge: 'Active (24.5 MB)',
        port: 8085,
        open_url: '/drive/',
        open_label: 'Master Drive',
        can_toggle: true,
        memory_complete: true
      }
    ],
    active_memory_mb: 24.5,
    total_active_mem_mb: 24.5,
    memory_complete: true,
    estimated_saved_mem_mb: null,
    sleeping_count: 0
  };

  let servicePollCount = 0;

  const server = http.createServer((req, res) => {
    const url = req.url.split('?')[0];
    if (url === '/' || url === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(adminHtml);
    } else if (url === '/portal.css' || url === '/static/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      res.end(portalCss);
    } else if (url === '/static/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(portalPollingJs);
    } else if (url === '/laptop.js' || url === '/static/laptop.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(laptopJs);
    } else if (url === '/api/services') {
      servicePollCount++;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(currentServicesPayload));
    } else if (url === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ cpu: 5.0, mem: 40.0, disk: 30.0 }));
    } else if (url === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ connected: false }));
    } else if (url === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sessions: [] }));
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok' }));
    }
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH,
    headless: true
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  // Guard against any external network requests
  await page.route('**', (route) => {
    const requestUrl = route.request().url();
    if (!requestUrl.startsWith(baseUrl)) {
      console.error(`Blocked outbound request to: ${requestUrl}`);
      route.abort();
    } else {
      route.continue();
    }
  });

  try {
    // 1. Initial page load check
    await page.goto(baseUrl);

    // Initial placeholder should not pretend measured zero before data arrives:
    // in raw html it's "-- MB", and after fetchServices() resolves with 24.5 MB:
    await page.waitForSelector('#svc-active-mem');

    // Wait for the initial fetchServices poll to populate active memory
    await page.waitForFunction(() => {
      const el = document.getElementById('svc-active-mem');
      return el && el.textContent.includes('24.5 MB');
    });

    let memText = await page.textContent('#svc-active-mem');
    console.log('PASS: Initial poll rendered known active memory:', memText.trim());
    if (!memText.includes('24.5 MB')) {
      throw new Error(`Expected 24.5 MB, got ${memText}`);
    }

    // Check service card rendered with status badge
    const badgeText = await page.textContent('#services-grid .badge-positive');
    console.log('PASS: Service card badge rendered:', badgeText.trim());
    if (!badgeText.includes('Active (24.5 MB)')) {
      throw new Error(`Expected badge 'Active (24.5 MB)', got ${badgeText}`);
    }

    // 2. Change synthetic service response to incomplete memory
    currentServicesPayload = {
      services: [
        {
          id: 'filebrowser',
          name: 'Cloud Storage & Media',
          category: 'Storage & Media',
          running: true,
          ondemand: false,
          pid: 1234,
          rss_mb: null,
          status_badge: 'Active (Memory Unavailable)',
          port: 8085,
          open_url: '/drive/',
          open_label: 'Master Drive',
          can_toggle: true,
          memory_complete: false
        }
      ],
      active_memory_mb: null,
      total_active_mem_mb: null,
      memory_complete: false,
      estimated_saved_mem_mb: null,
      sleeping_count: 0
    };

    const pollCountBefore = servicePollCount;

    // Wait for normal scheduled poll request to /api/services (polling interval is 4000ms)
    // Do NOT call fetchServices() directly via page.evaluate
    await page.waitForResponse(response => {
      return response.url().includes('/api/services') && response.status() === 200;
    }, { timeout: 10000 });

    if (servicePollCount <= pollCountBefore) {
      throw new Error('Scheduled service poll request did not occur');
    }
    console.log(`PASS: Page executed scheduled service poll (poll count: ${servicePollCount})`);

    // Verify UI dynamically clears old numeric value to 'N/A'
    await page.waitForFunction(() => {
      const el = document.getElementById('svc-active-mem');
      return el && el.textContent.trim() === 'N/A';
    }, { timeout: 5000 });

    memText = await page.textContent('#svc-active-mem');
    console.log('PASS: Polled update cleared old numeric value and rendered:', memText.trim());
    if (memText.trim() !== 'N/A') {
      throw new Error(`Expected 'N/A' on incomplete memory poll, got '${memText.trim()}'`);
    }

    const updatedBadge = await page.textContent('#services-grid span.px-2.py-0\\.5');
    console.log('PASS: Polled service badge updated:', updatedBadge.trim());
    if (!updatedBadge.includes('Active (Memory Unavailable)')) {
      throw new Error(`Expected badge 'Active (Memory Unavailable)', got ${updatedBadge}`);
    }

    console.log('ALL BROWSER TESTS PASSED');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
