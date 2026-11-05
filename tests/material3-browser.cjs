const http = require('http');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const appearanceHelper = require('./fixtures/appearance-test-helper.cjs');

(async () => {
  const rootDir = path.resolve(__dirname, '..');
  const sddDir = path.resolve(rootDir, '.superpowers/sdd');
  if (!fs.existsSync(sddDir)) {
    fs.mkdirSync(sddDir, { recursive: true });
  }

  const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
  const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
  const loginHtml = fs.readFileSync(path.join(rootDir, 'web/login.html'), 'utf8');
  const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
  const portalPollingJs = fs.readFileSync(path.join(rootDir, 'web/portal-polling.js'), 'utf8');
  const laptopJs = fs.readFileSync(path.join(rootDir, 'web/laptop.js'), 'utf8');

  // Derive pure catalog metadata from app/service_catalog.py
  const catalogRaw = JSON.parse(
    execSync('python3 -c "import json, sys; from app.service_catalog import dashboard_services; json.dump(dashboard_services(), sys.stdout)"', {
      cwd: rootDir,
      encoding: 'utf8'
    })
  );

  const runtimeOverrides = {
    filebrowser: { running: true, ondemand: false, pid: 1234, rss_mb: 24.5, status_badge: 'Active (24.5 MB)', memory_complete: true },
    suwayomi: { running: true, ondemand: false, pid: 1400, rss_mb: 120.0, status_badge: 'Active (120 MB)', unread_updates: 3, memory_complete: true },
    vaultwarden: { running: false, ondemand: false, pid: null, rss_mb: 0, status_badge: 'Disabled', memory_complete: true },
    adguard: { running: true, ondemand: false, pid: 1550, rss_mb: 45.2, status_badge: 'Active (45.2 MB)', memory_complete: true },
    terminal: { running: false, ondemand: false, pid: null, rss_mb: 0, status_badge: 'Disabled', memory_complete: true },
    agy: { running: true, ondemand: false, pid: 1800, rss_mb: 85.0, status_badge: 'Active (85 MB)', memory_complete: true },
    converter: { running: false, ondemand: true, pid: null, rss_mb: 0, status_badge: 'Idle (0 MB)', memory_complete: true },
    syncthing: { running: false, ondemand: false, pid: null, rss_mb: 0, status_badge: 'Disabled', memory_complete: true },
    battery_guard: { running: true, ondemand: false, pid: 2100, rss_mb: null, status_badge: 'Active (daemon)', memory_complete: false }
  };

  const fullServices = Object.keys(catalogRaw).map(id => {
    return {
      ...catalogRaw[id],
      ...(runtimeOverrides[id] || { running: false, ondemand: false, pid: null, rss_mb: null, status_badge: 'Disabled', memory_complete: true })
    };
  });

  let currentNotes = 'Default note content for M3 testing';

  let torState = {
    enabled: true,
    verified_via_tor: true,
    stale: false,
    direct_ip: '192.168.1.8',
    masked_ip: '185.220.101.5',
    error: null
  };
  let vpnToggleRequests = [];
  let simulateVpnFailure = false;

  const server = http.createServer((req, res) => {
    if (appearanceHelper.handleSharedAssetHttp(req, res)) return;
    const parsed = new URL(req.url, 'http://127.0.0.1');
    const pathname = parsed.pathname;

    if (pathname === '/' || pathname === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(adminHtml);
    } else if (pathname === '/family') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(familyHtml);
    } else if (pathname === '/login') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(loginHtml);
    } else if (pathname === '/portal.css' || pathname === '/static/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      res.end(portalCss);
    } else if (pathname === '/static/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(portalPollingJs);
    } else if (pathname === '/laptop.js' || pathname === '/static/laptop.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      res.end(laptopJs);
    } else if (pathname === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        tailscale_ip: '100.75.159.53',
        local_ip: '192.168.1.8',
        stale: false,
        uptime: '14d 6h',
        battery: { percentage: 88, temperature: 31, status: 'UPS Protected' },
        ram: { used_mb: 3276, total_mb: 6144, percent: 53.3 },
        storage: { free_gb: 72, percent: 40.5 },
        cpu_temp: 38,
        network: { wifi_speed: 433, rssi: -52, wan_ping: 18, dns_status: 'Filtering Active' },
        load_avg: [0.45, 0.52, 0.48]
      }));
    } else if (pathname === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(torState));
    } else if (pathname === '/api/vpn/toggle' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        let parsedBody = null;
        try { parsedBody = JSON.parse(body); } catch (e) {}
        vpnToggleRequests.push({
          headers: req.headers,
          body: parsedBody
        });
        if (simulateVpnFailure) {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Tor daemon unavailable' }));
          return;
        }
        const desired = (parsedBody && typeof parsedBody.enabled === 'boolean')
          ? parsedBody.enabled
          : !torState.enabled;
        torState.enabled = desired;
        torState.verified_via_tor = desired;
        torState.masked_ip = desired ? '185.220.101.5' : 'unavailable';
        res.writeHead(200, { 'Content-Type': 'application/json' });
        // NOTE: Truthful state response, no status property
        res.end(JSON.stringify({
          enabled: torState.enabled,
          verified_via_tor: torState.verified_via_tor,
          stale: false,
          direct_ip: torState.direct_ip,
          masked_ip: torState.masked_ip,
          error: null
        }));
      });
    } else if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        services: fullServices,
        active_memory_mb: null,
        total_active_mem_mb: null,
        memory_complete: false,
        estimated_saved_mem_mb: null,
        sleeping_count: 4
      }));
    } else if (pathname === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        sessions: [
          { id: 'primary-session', name: 'Primary Work', is_primary: true },
          { id: 'worker-session-2', name: 'M3 Portal Task', is_primary: false }
        ]
      }));
    } else if (pathname === '/api/laptop/terminals') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        online: true,
        sessions: [{ id: 'a1b2c3d4e5f678901234567890abcdef' }],
        max_sessions: 4
      }));
    } else if (pathname === '/api/notes') {
      if (req.method === 'POST') {
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
          try {
            const data = JSON.parse(body);
            currentNotes = data.notes;
          } catch (e) {}
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'saved' }));
        });
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ notes: currentNotes }));
      }
    } else if (pathname === '/api/login' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        let creds = {};
        try { creds = JSON.parse(body); } catch (e) {}
        if (creds.username === 'wrong' || creds.password === 'badpassword') {
          res.writeHead(401, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'error', message: 'Invalid credentials provided' }));
          return;
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', user: 'astro', role: 'admin', jwt: 'mock-jwt-token', redirect: '/admin' }));
      });
    } else if (pathname === '/api/logout' && req.method === 'POST') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'logged_out' }));
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

  try {
    const context = await browser.newContext();
    const pageErrors = [];
    const externalRequests = [];
    context.on('page', page => page.on('pageerror', error => pageErrors.push(error.message)));
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === baseUrl) return route.continue();
      externalRequests.push(route.request().url());
      return route.abort();
    });

    // -------------------------------------------------------------
    // PASS 1: DESKTOP ADMIN (1280x900)
    // -------------------------------------------------------------
    console.log('--- TEST PASS 1: Desktop Admin (1280x900) ---');
    const desktopPage = await context.newPage();
    await desktopPage.setViewportSize({ width: 1280, height: 900 });
    await desktopPage.goto(`${baseUrl}/admin`);

    // Wait for services and stats to populate
    await desktopPage.waitForFunction(() => {
      const memEl = document.getElementById('svc-active-mem');
      const gridEl = document.getElementById('services-grid');
      return memEl && memEl.textContent.includes('N/A') && gridEl && gridEl.children.length === 9;
    });

    // 1. Verify computed Material 3 theme colors
    const themeColors = await desktopPage.evaluate(() => {
      const style = getComputedStyle(document.documentElement);
      const bodyStyle = getComputedStyle(document.body);
      return {
        surface: style.getPropertyValue('--md-sys-color-surface').trim(),
        surfaceContainer: style.getPropertyValue('--md-sys-color-surface-container').trim(),
        primary: style.getPropertyValue('--md-sys-color-primary').trim(),
        onPrimary: style.getPropertyValue('--md-sys-color-on-primary').trim(),
        tertiary: style.getPropertyValue('--md-sys-color-tertiary').trim(),
        bodyBg: bodyStyle.backgroundColor
      };
    });
    console.log('Computed M3 Tokens:', themeColors);
    assert.equal(themeColors.surface, '#111414', 'M3 Surface token must be #111414');
    assert.equal(themeColors.surfaceContainer, '#1d2223', 'M3 Surface Container token must be #1d2223');
    assert.equal(themeColors.primary, '#70d6c5', 'M3 Primary Teal token must be #70d6c5');
    assert.equal(themeColors.onPrimary, '#003734', 'M3 On-Primary token must be #003734');
    assert.equal(themeColors.tertiary, '#e5c46c', 'M3 Tertiary Gold token must be #e5c46c');

    // 2. Verify typography: Sans-serif Roboto stack on headings & telemetry (no Georgia/Times!)
    const typography = await desktopPage.evaluate(() => {
      const h1Font = getComputedStyle(document.querySelector('h1')).fontFamily;
      const batteryFont = getComputedStyle(document.getElementById('battery-pct')).fontFamily;
      const ramFont = getComputedStyle(document.getElementById('ram-used')).fontFamily;
      return { h1Font, batteryFont, ramFont };
    });
    console.log('Typography Check:', typography);
    assert(!typography.h1Font.includes('Georgia') && !typography.h1Font.includes('Times'), 'H1 must NOT use Georgia/Times');
    assert(!typography.batteryFont.includes('Georgia') && !typography.batteryFont.includes('Times'), 'Battery numerals must NOT use Georgia/Times');
    assert(!typography.ramFont.includes('Georgia') && !typography.ramFont.includes('Times'), 'RAM numerals must NOT use Georgia/Times');

    // 3. Verify actual computed text/action contrast on representative rendered pairs
    console.log('Checking computed contrast ratios on rendered elements...');
    const contrastChecks = await desktopPage.evaluate(() => {
      function luminance(r, g, b) {
        const a = [r, g, b].map(v => {
          v /= 255;
          return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        });
        return a[0] * 0.2126 + a[1] * 0.7152 + a[2] * 0.0722;
      }
      function parseRgb(str) {
        const m = str.match(/\d+(\.\d+)?/g);
        return m ? [parseFloat(m[0]), parseFloat(m[1]), parseFloat(m[2])] : [0, 0, 0];
      }
      function getRatio(c1, c2) {
        const [r1, g1, b1] = parseRgb(c1);
        const [r2, g2, b2] = parseRgb(c2);
        const l1 = luminance(r1, g1, b1);
        const l2 = luminance(r2, g2, b2);
        return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      }

      const pairs = [];
      const vpnBtn = document.getElementById('vpn-toggle-btn');
      if (vpnBtn) {
        const s = window.getComputedStyle(vpnBtn);
        pairs.push({ name: 'Primary Button', fg: s.color, bg: s.backgroundColor, ratio: getRatio(s.color, s.backgroundColor), min: 4.5 });
      }
      const card = document.querySelector('#services-grid .m3-card');
      if (card) {
        const cardBg = window.getComputedStyle(card).backgroundColor;
        const h3 = card.querySelector('h3');
        if (h3) {
          const s = window.getComputedStyle(h3);
          pairs.push({ name: 'Card Title vs Card Bg', fg: s.color, bg: cardBg, ratio: getRatio(s.color, cardBg), min: 4.5 });
        }
        const p = card.querySelector('p');
        if (p) {
          const s = window.getComputedStyle(p);
          pairs.push({ name: 'Card Desc vs Card Bg', fg: s.color, bg: cardBg, ratio: getRatio(s.color, cardBg), min: 4.5 });
        }
      }
      const tonalBtn = document.querySelector('.m3-btn-tonal');
      if (tonalBtn) {
        const s = window.getComputedStyle(tonalBtn);
        pairs.push({ name: 'Tonal Button', fg: s.color, bg: s.backgroundColor, ratio: getRatio(s.color, s.backgroundColor), min: 4.5 });
      }
      const goldBadge = document.querySelector('.badge-gold, .badge-accent');
      if (goldBadge) {
        const s = window.getComputedStyle(goldBadge);
        pairs.push({ name: 'Gold Badge', fg: s.color, bg: s.backgroundColor, ratio: getRatio(s.color, s.backgroundColor), min: 4.5 });
      }
      return pairs;
    });
    console.log('Measured Contrast Ratios:', contrastChecks);
    for (const name of ['Primary Button', 'Card Title vs Card Bg', 'Card Desc vs Card Bg', 'Tonal Button']) {
      assert(contrastChecks.some(item => item.name === name), `Missing rendered contrast pair: ${name}`);
    }
    for (const item of contrastChecks) {
      assert(item.ratio >= item.min, `${item.name} contrast ratio ${item.ratio.toFixed(2)} must be >= ${item.min}`);
    }

    // 4. Verify prefers-reduced-motion in browser
    console.log('Checking prefers-reduced-motion...');
    await desktopPage.emulateMedia({ reducedMotion: 'reduce' });
    const reducedMotionDuration = await desktopPage.evaluate(() => {
      const dummy = document.createElement('div');
      dummy.className = 'animate-spin';
      document.body.appendChild(dummy);
      const style = window.getComputedStyle(dummy);
      const dur = style.animationDuration;
      dummy.remove();
      return dur;
    });
    console.log('Reduced motion animation duration:', reducedMotionDuration);
    assert(reducedMotionDuration === '0.00001s' || reducedMotionDuration === '0.01ms' || parseFloat(reducedMotionDuration) < 0.001, 'Reduced motion must shorten animation duration');
    await desktopPage.emulateMedia({ reducedMotion: 'no-preference' });

    // 5. Verify No Horizontal Overflow on Desktop
    const desktopOverflow = await desktopPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(desktopOverflow, 'Desktop view must not exhibit horizontal overflow');

    // 6. Verify Interactive Touch Targets (all visible buttons, links, inputs, switches >= 48px hit area)
    const desktopTargetChecks = await desktopPage.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('button, a[href], a#header-local-ip, label.switch, input.m3-text-field'));
      const failures = [];
      for (const el of elements) {
        if (el.closest('.hidden')) continue;
        if (el.offsetParent === null && el.offsetWidth === 0 && el.offsetHeight === 0) continue;
        const rect = el.getBoundingClientRect();
        if (rect.height < 48 || rect.width < 48) {
          failures.push({
            tag: el.tagName,
            id: el.id,
            text: el.textContent.trim().slice(0, 20),
            w: Math.round(rect.width),
            h: Math.round(rect.height)
          });
        }
      }
      return failures;
    });
    console.log('Desktop touch target deviations count (>=48px):', desktopTargetChecks.length);
    assert(desktopTargetChecks.length === 0, `All interactive controls must meet >=48px touch boundary, found ${desktopTargetChecks.length} undersized: ${JSON.stringify(desktopTargetChecks)}`);

    // 7. Verify Populated Service Grid (9 cards from catalog, no undefined text)
    const cardCount = await desktopPage.locator('#services-grid .m3-card').count();
    assert.equal(cardCount, 9, 'Services grid must render all 9 services from catalog');
    const gridText = await desktopPage.locator('#services-grid').textContent();
    assert(!gridText.includes('undefined'), 'Services grid must not contain literal undefined text');
    const activeMemoryBadge = await desktopPage.textContent('#svc-active-mem');
    assert(activeMemoryBadge.includes('N/A'), 'Incomplete active-service memory must render N/A');

    // 8. Test Tor Toggle: Input-driven verification (click, 1 POST with boolean enabled, no status, truthful state, no spurious alert, re-enabled button, non-success path)
    console.log('Testing Tor Proxy Toggle (input-driven)...');
    vpnToggleRequests = [];
    let dialogAlerts = [];
    const dialogHandler = async dialog => {
      dialogAlerts.push(dialog.message());
      await dialog.dismiss();
    };
    desktopPage.on('dialog', dialogHandler);

    const vpnBtn = desktopPage.locator('#vpn-toggle-btn');
    assert.equal((await vpnBtn.textContent()).trim(), 'Disable Tor Proxy');
    assert.equal((await desktopPage.textContent('#vpn-badge')).trim(), 'Tor Exit Verified');

    // Click toggle to disable Tor
    await vpnBtn.click();
    await desktopPage.waitForFunction(() => {
      const btn = document.getElementById('vpn-toggle-btn');
      return btn && !btn.disabled && btn.textContent.includes('Enable Tor Proxy');
    });

    assert.equal(vpnToggleRequests.length, 1, 'Exactly one POST request to /api/vpn/toggle must occur');
    assert.equal(vpnToggleRequests[0].body.enabled, false, 'POST body must specify explicit boolean enabled: false');
    assert(vpnToggleRequests[0].headers['content-type'].includes('application/json'), 'Content-Type must be application/json');
    assert.equal(dialogAlerts.length, 0, 'No spurious alert must be shown on successful toggle');
    assert.equal(await desktopPage.textContent('#vpn-badge'), 'OFF');

    // Test controlled non-success path
    simulateVpnFailure = true;
    dialogAlerts = [];
    await vpnBtn.click();
    await desktopPage.waitForFunction(() => {
      const btn = document.getElementById('vpn-toggle-btn');
      return btn && !btn.disabled;
    });
    assert.equal(dialogAlerts.length, 1, 'Controlled non-success path must trigger exactly one alert');
    assert(dialogAlerts[0].includes('Tor daemon unavailable') || dialogAlerts[0].includes('Tor control failed'));
    simulateVpnFailure = false;
    desktopPage.off('dialog', dialogHandler);

    // 9. Converter Modal Keyboard Focus Trap, Dynamic Elements & Escape Handling
    console.log('Testing Converter Modal Focus Containment & Traversal...');
    const opener = desktopPage.locator('a[href="javascript:openConverterModal()"]');
    await opener.focus();
    await opener.click();
    assert(await desktopPage.locator('#converter-modal').isVisible(), 'Converter modal must open');

    // Verify initial focus is inside modal
    const initialFocusInside = await desktopPage.evaluate(() => {
      const modal = document.getElementById('converter-modal');
      return modal && modal.contains(document.activeElement);
    });
    assert(initialFocusInside, 'Focus must be placed inside modal upon opening');

    // Activate the real native file picker from the keyboard, without internal UI helpers.
    await desktopPage.keyboard.press('Tab');
    assert.equal(await desktopPage.locator('#convert-file-input').evaluate(el => el === document.activeElement), true);
    const fileFocusOutline = await desktopPage.locator('#convert-dropzone').evaluate(el => getComputedStyle(el).outlineStyle);
    assert.equal(fileFocusOutline, 'solid', 'File picker keyboard focus must remain visible');
    const [desktopChooser] = await Promise.all([
      desktopPage.waitForEvent('filechooser', { timeout: 3000 }),
      desktopPage.keyboard.press('Enter')
    ]);
    await desktopChooser.setFiles({ name: 'keyboard-proof.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic keyboard selection') });
    await desktopPage.locator('#convert-file-list-container:not(.hidden)').waitFor();
    assert((await desktopPage.textContent('#convert-file-list')).includes('keyboard-proof.txt'));

    // Traverse Tab forward
    for (let i = 0; i < 5; i++) {
      await desktopPage.keyboard.press('Tab');
      const inside = await desktopPage.evaluate(() => {
        const modal = document.getElementById('converter-modal');
        return modal && modal.contains(document.activeElement);
      });
      assert(inside, `Tab step ${i} escaped converter modal focus trap`);
    }

    // Traverse Shift+Tab backward
    for (let i = 0; i < 5; i++) {
      await desktopPage.keyboard.press('Shift+Tab');
      const inside = await desktopPage.evaluate(() => {
        const modal = document.getElementById('converter-modal');
        return modal && modal.contains(document.activeElement);
      });
      assert(inside, `Shift+Tab step ${i} escaped converter modal focus trap`);
    }

    // Verify touch targets on visible modal controls (>=48px)
    const modalTargetChecks = await desktopPage.evaluate(() => {
      const modal = document.getElementById('converter-modal');
      const elements = Array.from(modal.querySelectorAll('button, a[href], label'));
      const failures = [];
      for (const el of elements) {
        if (el.closest('.hidden')) continue;
        if (el.offsetParent === null && el.offsetWidth === 0 && el.offsetHeight === 0) continue;
        const rect = el.getBoundingClientRect();
        if (rect.height < 48 || rect.width < 48) {
          failures.push({
            tag: el.tagName,
            id: el.id,
            text: el.textContent.trim().slice(0, 20),
            w: Math.round(rect.width),
            h: Math.round(rect.height)
          });
        }
      }
      return failures;
    });
    assert(modalTargetChecks.length === 0, `All modal controls must meet >=48px touch boundary: ${JSON.stringify(modalTargetChecks)}`);

    // Capture Dialog Screenshot
    const dialogScreenshot = path.join(sddDir, 'm3-dialog-desktop.png');
    await desktopPage.screenshot({ path: dialogScreenshot });
    console.log('Saved screenshot:', dialogScreenshot, `(${fs.statSync(dialogScreenshot).size} bytes)`);

    // Press Escape to dismiss and verify focus restoration to opener
    await desktopPage.keyboard.press('Escape');
    await desktopPage.waitForTimeout(200);
    assert(await desktopPage.locator('#converter-modal').isHidden(), 'Converter modal must dismiss on Escape key');
    const restoredToOpener = await desktopPage.evaluate(() => {
      const op = document.querySelector('a[href="javascript:openConverterModal()"]');
      return document.activeElement === op;
    });
    assert(restoredToOpener, 'Focus must be restored to actual opener after Escape dismissal');

    // Capture screenshot: Desktop Admin
    const desktopAdminScreenshot = path.join(sddDir, 'm3-admin-desktop.png');
    await desktopPage.screenshot({ path: desktopAdminScreenshot, fullPage: true });
    console.log('Saved screenshot:', desktopAdminScreenshot, `(${fs.statSync(desktopAdminScreenshot).size} bytes)`);
    await desktopPage.close();

    // -------------------------------------------------------------
    // PASS 2: MOBILE ADMIN (412x915 & 390x844 & 360x800)
    // -------------------------------------------------------------
    console.log('--- TEST PASS 2: Mobile Admin (412x915, 390x844, 360x800) ---');
    const mobileAdminPage = await context.newPage();
    await mobileAdminPage.setViewportSize({ width: 412, height: 915 });
    await mobileAdminPage.goto(`${baseUrl}/admin`);

    await mobileAdminPage.waitForFunction(() => {
      const memEl = document.getElementById('svc-active-mem');
      return memEl && memEl.textContent.includes('N/A');
    });

    const overflow412 = await mobileAdminPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(overflow412, 'Mobile 412px view must not exhibit horizontal overflow');

    // Verify 390px mobile view: no horizontal overflow, touch targets >= 48px
    await mobileAdminPage.setViewportSize({ width: 390, height: 844 });
    const overflow390 = await mobileAdminPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(overflow390, 'Mobile 390px view must not exhibit horizontal overflow');

    const mobile390Targets = await mobileAdminPage.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('button, a[href], a#header-local-ip, label.switch, input.m3-text-field'));
      const failures = [];
      for (const el of elements) {
        if (el.closest('.hidden')) continue;
        if (el.offsetParent === null && el.offsetWidth === 0 && el.offsetHeight === 0) continue;
        const rect = el.getBoundingClientRect();
        if (rect.height < 48 || rect.width < 48) {
          failures.push({
            tag: el.tagName,
            id: el.id,
            text: el.textContent.trim().slice(0, 20),
            w: Math.round(rect.width),
            h: Math.round(rect.height)
          });
        }
      }
      return failures;
    });
    console.log('Mobile 390px touch target deviations count (>=48px):', mobile390Targets.length);
    assert(mobile390Targets.length === 0, `All interactive controls at 390px must meet >=48px hit area: ${JSON.stringify(mobile390Targets)}`);

    await mobileAdminPage.locator('a[href="javascript:openConverterModal()"]').click();
    const mobileDialogTargets = await mobileAdminPage.locator('#converter-modal').evaluate(modal => {
      return Array.from(modal.querySelectorAll('button, label[for]')).filter(el => {
        if (el.closest('.hidden') || el.getClientRects().length === 0) return false;
        const rect = el.getBoundingClientRect();
        return rect.width < 48 || rect.height < 48 || rect.left < 0 || rect.right > innerWidth;
      }).map(el => el.id || el.textContent.trim());
    });
    assert.deepEqual(mobileDialogTargets, [], '390px dialog controls must remain visible with 48px hit targets');
    await mobileAdminPage.keyboard.press('Escape');

    // Verify 360px viewport
    await mobileAdminPage.setViewportSize({ width: 360, height: 800 });
    const narrowAdminOverflow = await mobileAdminPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(narrowAdminOverflow, 'Mobile 360px view must not exhibit horizontal overflow');

    // Reset to 412px for official screenshot
    await mobileAdminPage.setViewportSize({ width: 412, height: 915 });
    const mobileAdminScreenshot = path.join(sddDir, 'm3-admin-mobile.png');
    await mobileAdminPage.screenshot({ path: mobileAdminScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileAdminScreenshot, `(${fs.statSync(mobileAdminScreenshot).size} bytes)`);
    await mobileAdminPage.close();

    // -------------------------------------------------------------
    // PASS 3: MOBILE FAMILY (412x915)
    // -------------------------------------------------------------
    console.log('--- TEST PASS 3: Mobile Family (412x915) ---');
    const familyPage = await context.newPage();
    await familyPage.setViewportSize({ width: 412, height: 915 });
    await familyPage.goto(`${baseUrl}/family`);

    const familyOverflow = await familyPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(familyOverflow, 'Family mobile view must not exhibit horizontal overflow');

    // Check 5 family service cards
    const familyCards = await familyPage.locator('.family-services-grid .m3-card').count();
    assert.equal(familyCards, 5, 'Family page must render exactly 5 service cards');
    const familyText = await familyPage.locator('.family-services-grid').textContent();
    assert(!familyText.includes('undefined'), 'Family grid must not contain literal undefined text');

    // Check Quick Notes toolbar buttons meeting >=48px touch target
    const bulletBtnRect = await familyPage.locator('button[onclick="insertBulletList()"]').boundingBox();
    assert(bulletBtnRect.height >= 48 && bulletBtnRect.width >= 48, `Bullet list button must be >= 48px, got ${bulletBtnRect.width}x${bulletBtnRect.height}`);

    // Test notes edit and autosave with corrected waitForFunction signature (fn, undefined, options)
    await familyPage.locator('#user-notes-input').fill('Updated notes in family M3 test');
    await familyPage.waitForFunction(() => {
      const st = document.getElementById('notes-status');
      return st && st.textContent.includes('Saved');
    }, undefined, { timeout: 3000 });
    assert.equal(currentNotes, 'Updated notes in family M3 test');

    // Verify converter modal keyboard focus trap & Escape on family page
    const familyConverterOpener = familyPage.locator('button[onclick="openConverterModal()"]');
    await familyConverterOpener.focus();
    await familyConverterOpener.click();
    assert(await familyPage.locator('#converter-modal').isVisible(), 'Family converter modal must open');

    await familyPage.keyboard.press('Tab');
    assert.equal(await familyPage.locator('#convert-file-input').evaluate(el => el === document.activeElement), true);
    assert.equal(await familyPage.locator('#convert-dropzone').evaluate(el => getComputedStyle(el).outlineStyle), 'solid');
    const [familyChooser] = await Promise.all([
      familyPage.waitForEvent('filechooser', { timeout: 3000 }),
      familyPage.keyboard.press('Enter')
    ]);
    await familyChooser.setFiles({ name: 'family-keyboard.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic family selection') });
    await familyPage.locator('#convert-file-list-container:not(.hidden)').waitFor();

    for (let i = 0; i < 4; i++) {
      await familyPage.keyboard.press('Tab');
      const inside = await familyPage.evaluate(() => {
        const m = document.getElementById('converter-modal');
        return m && m.contains(document.activeElement);
      });
      assert(inside, 'Tab in family converter modal must remain trapped');
    }
    for (let i = 0; i < 8; i++) {
      await familyPage.keyboard.press('Shift+Tab');
      assert.equal(await familyPage.locator('#converter-modal').evaluate(modal => modal.contains(document.activeElement)), true);
    }
    await familyPage.keyboard.press('Escape');
    await familyPage.waitForTimeout(200);
    assert(await familyPage.locator('#converter-modal').isHidden(), 'Family converter modal must dismiss on Escape');
    assert.equal(await familyConverterOpener.evaluate(el => el === document.activeElement), true, 'Family modal must return focus to its opener');

    const mobileFamilyScreenshot = path.join(sddDir, 'm3-family-mobile.png');
    await familyPage.screenshot({ path: mobileFamilyScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileFamilyScreenshot, `(${fs.statSync(mobileFamilyScreenshot).size} bytes)`);
    await familyPage.close();

    // -------------------------------------------------------------
    // PASS 4: MOBILE LOGIN (412x915) & ERROR LAYOUT
    // -------------------------------------------------------------
    console.log('--- TEST PASS 4: Mobile Login (412x915) ---');
    const loginPage = await context.newPage();
    await loginPage.setViewportSize({ width: 412, height: 915 });
    await loginPage.goto(`${baseUrl}/login`);

    const loginOverflow = await loginPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(loginOverflow, 'Login mobile view must not exhibit horizontal overflow');

    const userBox = await loginPage.locator('#username').boundingBox();
    const passBox = await loginPage.locator('#password').boundingBox();
    const submitBox = await loginPage.locator('#submit-btn').boundingBox();

    assert(userBox.height >= 48, `Username field height must be >= 48px, got ${userBox.height}`);
    assert(passBox.height >= 48, `Password field height must be >= 48px, got ${passBox.height}`);
    assert(submitBox.height >= 48, `Submit button height must be >= 48px, got ${submitBox.height}`);

    const mobileLoginScreenshot = path.join(sddDir, 'm3-login-mobile.png');
    await loginPage.screenshot({ path: mobileLoginScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileLoginScreenshot, `(${fs.statSync(mobileLoginScreenshot).size} bytes)`);

    // Trigger synthetic login error and check visible layout
    console.log('Triggering synthetic login error...');
    await loginPage.fill('#username', 'wrong');
    await loginPage.fill('#password', 'badpassword');
    await loginPage.click('#submit-btn');

    await loginPage.waitForSelector('#error-msg:not(.hidden)');
    const errText = await loginPage.textContent('#error-msg');
    assert.equal(errText, 'Invalid credentials provided');

    const errBox = await loginPage.locator('#error-msg').boundingBox();
    assert(errBox.width > 200 && errBox.height >= 20, 'Login error must have visible layout');

    // Verify error contrast ratio
    const errContrast = await loginPage.evaluate(() => {
      function luminance(r, g, b) {
        const a = [r, g, b].map(v => {
          v /= 255;
          return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        });
        return a[0] * 0.2126 + a[1] * 0.7152 + a[2] * 0.0722;
      }
      function parseRgb(str) {
        const m = str.match(/\d+(\.\d+)?/g);
        return m ? [parseFloat(m[0]), parseFloat(m[1]), parseFloat(m[2])] : [0, 0, 0];
      }
      const el = document.getElementById('error-msg');
      const s = window.getComputedStyle(el);
      const [r1, g1, b1] = parseRgb(s.color);
      const l1 = luminance(r1, g1, b1);
      // Measure the error banner's own opaque background, not its parent card.
      const [r2, g2, b2] = parseRgb(s.backgroundColor);
      const l2 = luminance(r2, g2, b2);
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    });
    console.log('Login error computed contrast ratio:', errContrast.toFixed(2));
    assert(errContrast >= 4.5, `Error message contrast ratio ${errContrast.toFixed(2)} must be >= 4.5`);

    const loginErrorScreenshot = path.join(sddDir, 'm3-login-error-mobile.png');
    await loginPage.screenshot({ path: loginErrorScreenshot, fullPage: true });
    console.log('Saved screenshot:', loginErrorScreenshot, `(${fs.statSync(loginErrorScreenshot).size} bytes)`);

    await loginPage.close();

    await context.close();
    assert.deepEqual(pageErrors, [], 'Actual role templates must produce no uncaught page errors');
    assert.deepEqual(externalRequests, [], 'Role templates must not depend on external page resources');

    console.log('====================================================');
    console.log('MATERIAL 3 ACCEPTANCE PASS: All visual, interaction, and responsive assertions succeeded.');
    console.log('====================================================');

  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(err => {
  console.error('Material 3 Browser Verification Failed:', err);
  process.exit(1);
});
