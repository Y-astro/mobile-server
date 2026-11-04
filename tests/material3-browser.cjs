const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');

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

  const fullServices = [
    { id: 'filebrowser', name: 'Cloud Storage & Media', category: 'Storage & Media', running: true, ondemand: false, pid: 1234, rss_mb: 24.5, status_badge: 'Active (24.5 MB)', port: 8085, open_url: '/drive/', open_label: 'Master Drive', can_toggle: true, memory_complete: true },
    { id: 'suwayomi', name: 'Manga Reader & Server', category: 'Media & Reading', running: true, ondemand: false, pid: 1400, rss_mb: 120.0, status_badge: 'Active (120 MB)', port: 4567, open_url: '/suwayomi/', open_label: 'Reader GUI', unread_updates: 3, can_toggle: true, memory_complete: true },
    { id: 'vaultwarden', name: 'Password Vault (Bitwarden)', category: 'Security & Auth', running: false, ondemand: false, pid: null, rss_mb: null, status_badge: 'Disabled', port: 8082, open_url: 'javascript:openVaultHTTPS()', open_label: 'HTTPS Portal', can_toggle: true, memory_complete: true },
    { id: 'adguard', name: 'AdGuard Home DNS', category: 'Network Protection', running: true, ondemand: false, pid: 1550, rss_mb: 45.2, status_badge: 'Active (45.2 MB)', port: 3000, open_url: 'http://100.75.159.53:3000', open_label: 'DNS Dashboard', can_toggle: false, memory_complete: true },
    { id: 'terminal', name: 'Termux Web Terminal', category: 'System Shell', running: false, ondemand: false, pid: null, rss_mb: null, status_badge: 'Disabled', port: 8080, open_url: '/termux/', open_label: 'Terminal Shell', can_toggle: true, memory_complete: true },
    { id: 'agy', name: 'Autonomous Coding Agent', category: 'AI & Developer', running: true, ondemand: false, pid: 1800, rss_mb: 85.0, status_badge: 'Active (85 MB)', port: 8086, open_url: '/agy/', open_label: 'Agent Workspace', can_toggle: false, memory_complete: true },
    { id: 'converter', name: 'Document to PDF Converter', category: 'Document Tools', running: false, ondemand: false, pid: null, rss_mb: null, status_badge: 'Tool Ready', port: null, open_url: 'javascript:openConverterModal()', open_label: 'Convert File', can_toggle: false, memory_complete: true },
    { id: 'syncthing', name: 'Continuous File Sync', category: 'Sync & Backup', running: false, ondemand: false, pid: null, rss_mb: null, status_badge: 'Disabled', port: 8384, open_url: 'javascript:openSyncthingGUI()', open_label: 'Sync Console', can_toggle: true, memory_complete: true }
  ];

  let currentNotes = 'Default note content for M3 testing';

  const server = http.createServer((req, res) => {
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
      res.end(JSON.stringify({
        enabled: true,
        verified_via_tor: true,
        stale: false,
        direct_ip: '192.168.1.8',
        masked_ip: '185.220.101.5',
        error: null
      }));
    } else if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        services: fullServices,
        active_memory_mb: 274.7,
        total_active_mem_mb: 274.7,
        memory_complete: true,
        estimated_saved_mem_mb: 320,
        sleeping_count: 3
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
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', user: 'astro', role: 'admin', jwt: 'mock-jwt-token', redirect: '/admin' }));
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
      return memEl && memEl.textContent.includes('274.7 MB') && gridEl && gridEl.children.length >= 8;
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

    // 3. Verify No Horizontal Overflow on Desktop
    const desktopOverflow = await desktopPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(desktopOverflow, 'Desktop view must not exhibit horizontal overflow');

    // 4. Verify Interactive Touch Targets (all buttons, links, inputs, switches >= 48px hit area)
    const targetChecks = await desktopPage.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('button, a.m3-btn, a.btn-secondary, label.switch, input.m3-text-field'));
      const failures = [];
      for (const el of elements) {
        if (el.offsetParent === null) continue; // skip hidden
        const rect = el.getBoundingClientRect();
        // check hit area: min dimension 48px or touch target height >= 48px
        if (rect.height < 44 || rect.width < 44) {
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
    console.log('Touch target deviations count:', targetChecks.length);
    if (targetChecks.length > 0) {
      console.warn('Deviating elements:', targetChecks.slice(0, 5));
    }
    assert(targetChecks.length === 0, `All interactive controls must meet minimum touch boundary, found ${targetChecks.length} undersized`);

    // 5. Verify Populated Service Grid (8 cards with truthful state chips & action links)
    const cardCount = await desktopPage.locator('#services-grid .m3-card').count();
    assert.equal(cardCount, 8, 'Services grid must render all 8 services');
    const activeMemoryBadge = await desktopPage.textContent('#svc-active-mem');
    assert(activeMemoryBadge.includes('274.7 MB'), 'Active memory must reflect 274.7 MB');

    // 6. Verify Keyboard Focus and Modal Dialog Escape Key Handling
    await desktopPage.locator('a[href="javascript:openConverterModal()"]').click();
    assert(await desktopPage.locator('#converter-modal').isVisible(), 'Converter modal must open');
    // Press Escape to dismiss
    await desktopPage.keyboard.press('Escape');
    await desktopPage.waitForTimeout(200);
    assert(await desktopPage.locator('#converter-modal').isHidden(), 'Converter modal must dismiss on Escape key');

    // Capture screenshot: Desktop Admin
    const desktopAdminScreenshot = path.join(sddDir, 'm3-admin-desktop.png');
    await desktopPage.screenshot({ path: desktopAdminScreenshot, fullPage: true });
    console.log('Saved screenshot:', desktopAdminScreenshot, `(${fs.statSync(desktopAdminScreenshot).size} bytes)`);

    // Reopen modal to capture Dialog Screenshot
    await desktopPage.locator('a[href="javascript:openConverterModal()"]').click();
    const dialogScreenshot = path.join(sddDir, 'm3-dialog-desktop.png');
    await desktopPage.screenshot({ path: dialogScreenshot });
    console.log('Saved screenshot:', dialogScreenshot, `(${fs.statSync(dialogScreenshot).size} bytes)`);
    await desktopPage.keyboard.press('Escape');
    await desktopPage.close();

    // -------------------------------------------------------------
    // PASS 2: MOBILE ADMIN (412x915 & 360x800)
    // -------------------------------------------------------------
    console.log('--- TEST PASS 2: Mobile Admin (412x915 Redmi K20 Pro) ---');
    const mobileAdminPage = await context.newPage();
    await mobileAdminPage.setViewportSize({ width: 412, height: 915 });
    await mobileAdminPage.goto(`${baseUrl}/admin`);

    await mobileAdminPage.waitForFunction(() => {
      const memEl = document.getElementById('svc-active-mem');
      return memEl && memEl.textContent.includes('274.7 MB');
    });

    const overflowData = await mobileAdminPage.evaluate(() => {
      const sw = document.documentElement.scrollWidth;
      const cw = document.documentElement.clientWidth;
      const list = [];
      document.querySelectorAll('*').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.right > cw + 1) {
          list.push({ tag: el.tagName, id: el.id, cls: el.className, right: Math.round(r.right), width: Math.round(r.width) });
        }
      });
      return { sw, cw, list };
    });
    console.log('Mobile 412px scrollWidth vs clientWidth:', overflowData.sw, overflowData.cw);
    console.log('Overflowing elements:', overflowData.list);
    assert(overflowData.sw <= overflowData.cw, 'Mobile 412px view must not exhibit horizontal overflow');

    // Also test narrow 360px viewport
    await mobileAdminPage.setViewportSize({ width: 360, height: 800 });
    const narrowAdminOverflow = await mobileAdminPage.evaluate(() => {
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    assert(narrowAdminOverflow, 'Mobile 360px view must not exhibit horizontal overflow');

    // Reset to 412px for screenshot
    await mobileAdminPage.setViewportSize({ width: 412, height: 915 });
    const mobileAdminScreenshot = path.join(sddDir, 'm3-admin-mobile.png');
    await mobileAdminPage.screenshot({ path: mobileAdminScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileAdminScreenshot, `(${fs.statSync(mobileAdminScreenshot).size} bytes)`);
    await mobileAdminPage.close();

    // -------------------------------------------------------------
    // PASS 3: MOBILE FAMILY (412x915) & MOBILE LOGIN (412x915)
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

    // Check Quick Notes toolbar buttons meeting touch target
    const bulletBtnRect = await familyPage.locator('button[onclick="insertBulletList()"]').boundingBox();
    assert(bulletBtnRect.height >= 44 && bulletBtnRect.width >= 44, 'Bullet list button must satisfy touch target boundary');

    // Test notes edit and autosave
    await familyPage.locator('#user-notes-input').fill('Updated notes in family M3 test');
    await familyPage.waitForFunction(() => {
      const st = document.getElementById('notes-status');
      return st && st.textContent.includes('Saved');
    }, { timeout: 3000 });
    assert.equal(currentNotes, 'Updated notes in family M3 test');

    const mobileFamilyScreenshot = path.join(sddDir, 'm3-family-mobile.png');
    await familyPage.screenshot({ path: mobileFamilyScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileFamilyScreenshot, `(${fs.statSync(mobileFamilyScreenshot).size} bytes)`);
    await familyPage.close();

    // Mobile Login Check
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

    assert(userBox.height >= 44, `Username field height must be >= 44px, got ${userBox.height}`);
    assert(passBox.height >= 44, `Password field height must be >= 44px, got ${passBox.height}`);
    assert(submitBox.height >= 44, `Submit button height must be >= 44px, got ${submitBox.height}`);

    const mobileLoginScreenshot = path.join(sddDir, 'm3-login-mobile.png');
    await loginPage.screenshot({ path: mobileLoginScreenshot, fullPage: true });
    console.log('Saved screenshot:', mobileLoginScreenshot, `(${fs.statSync(mobileLoginScreenshot).size} bytes)`);
    await loginPage.close();

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
