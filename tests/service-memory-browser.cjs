const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const adminHtmlPath = path.resolve(__dirname, '../web/admin.html');
  const adminHtml = fs.readFileSync(adminHtmlPath, 'utf8');

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

  const server = http.createServer((req, res) => {
    if (req.url === '/' || req.url === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(adminHtml);
    } else if (req.url === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(currentServicesPayload));
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

  try {
    // 1. Initial page load check
    await page.goto(baseUrl);

    // Initial placeholder should not pretend measured zero before data arrives:
    // in raw html it's "-- MB", and after fetchServices() resolves with 24.5 MB:
    await page.waitForSelector('#svc-active-mem');

    // Wait for the fetchServices response to populate the active memory element
    await page.waitForFunction(() => {
      const el = document.getElementById('svc-active-mem');
      return el && el.textContent.includes('24.5 MB');
    });

    let memText = await page.textContent('#svc-active-mem');
    console.log('PASS: Initial fetch rendered known active memory:', memText.trim());
    if (!memText.includes('24.5 MB')) {
      throw new Error(`Expected 24.5 MB, got ${memText}`);
    }

    // Check service card rendered with status badge
    const badgeText = await page.textContent('#services-grid .badge-positive');
    console.log('PASS: Service card badge rendered:', badgeText.trim());
    if (!badgeText.includes('Active (24.5 MB)')) {
      throw new Error(`Expected badge 'Active (24.5 MB)', got ${badgeText}`);
    }

    // 2. Change server payload to unknown/incomplete memory
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

    // User interaction or poll triggering fetchServices()
    // In web/admin.html, user can trigger refresh or fetchServices can be evaluated
    await page.evaluate(async () => {
      await fetchServices();
    });

    await page.waitForFunction(() => {
      const el = document.getElementById('svc-active-mem');
      return el && el.textContent.trim() === 'N/A';
    });

    memText = await page.textContent('#svc-active-mem');
    console.log('PASS: Transition from known to unknown memory cleared stale text and displayed:', memText.trim());
    if (memText.trim() !== 'N/A') {
      throw new Error(`Expected 'N/A' on null memory, got '${memText.trim()}'`);
    }

    const updatedBadge = await page.textContent('#services-grid span.px-2.py-0\\.5');
    console.log('PASS: Updated service badge rendered:', updatedBadge.trim());
    if (!updatedBadge.includes('Active (Memory Unavailable)')) {
      throw new Error(`Expected badge 'Active (Memory Unavailable)', got ${updatedBadge}`);
    }

    console.log('ALL BROWSER TESTS PASSED');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
