/**
 * =========================================================================
 * CLOUD STORAGE & GLASS APPEARANCE VERIFICATION SUITE
 * Exercises real FileBrowser app mount, 29 named exports, lazy chunks,
 * shared appearance controller, gallery, sliders, reset, reload persistence,
 * portal cross-page appearance, 48px touch targets, error fallbacks.
 * =========================================================================
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');

const rootDir = path.resolve(__dirname, '..');
const sddDir = path.resolve(rootDir, '.superpowers/sdd');
[sddDir].forEach(d => {
  if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
});

// Load maintained files
const entryJs = fs.readFileSync(path.join(rootDir, 'web/filebrowser-entry.js'), 'utf8');
const filebrowserJs = fs.readFileSync(path.join(rootDir, 'web/filebrowser.js'), 'utf8');
const filebrowserCss = fs.readFileSync(path.join(rootDir, 'web/filebrowser.css'), 'utf8');
const themeTokensCss = fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8');
const appearanceCss = fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8');
const appearanceJs = fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8');
const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
const adminHtml = fs.readFileSync(path.join(rootDir, 'web/admin.html'), 'utf8');
const familyHtml = fs.readFileSync(path.join(rootDir, 'web/family.html'), 'utf8');
const loginHtml = fs.readFileSync(path.join(rootDir, 'web/login.html'), 'utf8');

// Cached public static assets
const staticDir = path.join(rootDir, '.work/filebrowser-static');
const nativeIndexHtml = fs.existsSync(path.join(staticDir, 'index.html'))
  ? fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8')
  : '';
// shortcut: target acceptance uses captured public assets; automate capture when enabling this suite in CI.
assert(nativeIndexHtml.includes('index-BqkXVoMb.js'), 'Capture the pinned public FileBrowser fixture into .work/filebrowser-static before running test:cloud');

function makeSyntheticJwt(user = {}) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    user: {
      id: 1,
      username: 'astro',
      perm: {
        admin: true,
        execute: false,
        create: true,
        rename: true,
        modify: true,
        delete: true,
        share: true,
        download: true
      },
      viewMode: 'mosaic',
      locale: 'en',
      aceEditorTheme: 'ace/theme/twilight',
      ...user
    },
    exp: Math.floor(Date.now() / 1000) + 86400
  })).toString('base64url');
  return `${header}.${payload}.synthetic-signature`;
}

const wallpapersData = {
  version: 1,
  default_id: 'db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc',
  wallpapers: [
    {
      id: 'db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc',
      name: '06. Frosted Glass.png',
      width: 3840,
      height: 2160,
      thumbnail_url: '/api/wallpapers/db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc/thumbnail.webp',
      image_url: '/api/wallpapers/db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc/display.webp'
    },
    {
      id: 'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210',
      name: '12. Dark Obsidian.png',
      width: 3840,
      height: 2160,
      thumbnail_url: '/api/wallpapers/fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210/thumbnail.webp',
      image_url: '/api/wallpapers/fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210/display.webp'
    }
  ]
};

// 1x1 transparent PNG for mock images
const mockPixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

// Default display wallpaper read locally from sibling if available
const defaultWallpaperPath = path.resolve(rootDir, "../cloud-glass-data/.work/wallpapers-library/display/db06f8659ae47cecaf6444034e8ffbc5f4c24c3b666199e0929ae17baf697bbc.webp");
const defaultWallpaperBytes = fs.existsSync(defaultWallpaperPath)
  ? fs.readFileSync(defaultWallpaperPath)
  : mockPixel;

(async () => {
  let wallpaperApiAuthRequired = false;
  let wallpaperApiHang = false;
  let authCheckAdminRole = null;
  let authCheckAdminStatus = null;
  let authCheckAdminHang = false;

  const server = http.createServer((req, res) => {
    const parsed = new URL(req.url, 'http://127.0.0.1');
    const pathname = parsed.pathname;

    // Portal routes
    if (pathname === '/' || pathname === '/admin') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(adminHtml);
    }
    if (pathname === '/family') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(familyHtml);
    }
    if (pathname === '/login') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(loginHtml);
    }
    if (pathname === '/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(portalCss);
    }
    if (pathname === '/static/portal-polling.js') {
      const p = path.join(rootDir, 'web/portal-polling.js');
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : '');
    }
    if (pathname === '/static/laptop.js') {
      const p = path.join(rootDir, 'web/laptop.js');
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : '');
    }
    if (pathname === '/api/agy/sessions') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ sessions: [] }));
    }

    // Shared appearance assets
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
    if (pathname === '/api/wallpapers') {
      if (wallpaperApiHang) return;
      if (wallpaperApiAuthRequired) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: 'unauthorized' }));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify(wallpapersData));
    }
    if (pathname.startsWith('/api/wallpapers/') && pathname.endsWith('.webp')) {
      if (pathname.includes('/missing/')) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: 'not found' }));
      }
      res.writeHead(200, { 'Content-Type': 'image/webp', 'Cache-Control': 'private, no-store' });
      return res.end(defaultWallpaperBytes);
    }

    if (pathname === '/api/auth-check-admin') {
      if (authCheckAdminHang) return;
      if (authCheckAdminStatus === 302) {
        res.writeHead(302, {Location: '/login'});
        return res.end();
      }
      if (authCheckAdminStatus === 500) {
        res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
        return res.end('Internal Server Error');
      }
      let role = authCheckAdminRole;
      if (!role) {
        const fwdUser = req.headers['x-forwarded-user'] || '';
        const cookie = req.headers['cookie'] || '';
        const referer = req.headers['referer'] || '';
        if (fwdUser === 'admin' || fwdUser === 'astro') role = 'admin';
        else if (fwdUser === 'family') role = 'family';
        else if (cookie.includes('session_user=admin') || cookie.includes('session_user=astro')) role = 'admin';
        else if (cookie.includes('session_user=family')) role = 'family';
        else if (referer.includes('/family')) role = 'family';
        else if (referer.includes('/login')) role = 'anonymous';
        else if (referer.includes('/admin') || referer.includes('/drive/')) role = 'admin';
        else role = 'admin';
      }

      if (role === 'admin') {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8', 'X-Forwarded-User': 'astro' });
        return res.end('OK');
      }
      if (role === 'family') {
        res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
        return res.end('Forbidden: Administrator privileges required.');
      }
      res.writeHead(401, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ status: 'error', message: 'Unauthorized' }));
    }

    // Portal APIs for admin/family/stats
    if (pathname === '/api/stats') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ tailscale_ip: '100.75.159.53', local_ip: '192.168.1.8', battery: { percentage: 88 } }));
    }
    if (pathname === '/api/services') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ services: [] }));
    }
    if (pathname === '/api/vpn/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ enabled: true, verified_via_tor: true, masked_ip: '185.220.101.5' }));
    }
    if (pathname === '/api/notes') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ notes: 'Test note' }));
    }

    // Public FileBrowser HTML & Static Assets
    if (pathname === '/drive/' || pathname.startsWith('/drive/files') || pathname.startsWith('/drive/settings')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(nativeIndexHtml);
    }

    // Native custom CSS override
    if (pathname === '/drive/static/custom.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(filebrowserCss);
    }

    // Native entry wrapper: imports /appearance.js & reexports from ./mobile-filebrowser.js
    if (pathname === '/drive/static/assets/index-BqkXVoMb.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(entryJs);
    }

    // Same-directory module alias: serves maintained web/filebrowser.js
    if (pathname === '/drive/static/assets/mobile-filebrowser.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(filebrowserJs);
    }

    // Cached static chunks
    if (pathname.startsWith('/drive/static/assets/')) {
      const assetName = path.basename(pathname);
      const filePath = path.join(staticDir, 'assets', assetName);
      if (fs.existsSync(filePath)) {
        const ext = path.extname(assetName);
        const contentType = ext === '.js' ? 'application/javascript; charset=utf-8' : (ext === '.css' ? 'text/css; charset=utf-8' : 'application/octet-stream');
        res.writeHead(200, { 'Content-Type': contentType });
        return res.end(fs.readFileSync(filePath));
      }
    }
    if (pathname.startsWith('/drive/static/img/')) {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
      return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"></svg>');
    }

    // FileBrowser synthetic APIs
    if (pathname === '/drive/api/login') {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
      return res.end(makeSyntheticJwt());
    }
    if (pathname === '/drive/api/renew') {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
      return res.end(makeSyntheticJwt());
    }
    if (pathname.startsWith('/drive/api/resources')) {
      if (req.method === 'DELETE' || req.method === 'PUT' || req.method === 'POST') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'ok' }));
      }
      if (pathname.endsWith('/notes.txt')) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({path:'/notes.txt',name:'notes.txt',size:24,extension:'.txt',modified:'2026-11-05T12:00:00Z',mode:420,isDir:false,type:'text',content:'Sample downloaded content'}));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        path: '/',
        name: '',
        size: 0,
        extension: '',
        modified: '2026-11-05T12:00:00Z',
        mode: 2147484141,
        isDir: true,
        isSymlink: false,
        type: '',
        items: [
          {
            path: '/Documents',
            name: 'Documents',
            size: 0,
            extension: '',
            modified: '2026-11-05T12:00:00Z',
            mode: 2147484141,
            isDir: true,
            isSymlink: false,
            type: ''
          },
          {
            path: '/notes.txt',
            name: 'notes.txt',
            size: 1024,
            extension: '.txt',
            modified: '2026-11-05T12:00:00Z',
            mode: 420,
            isDir: false,
            isSymlink: false,
            type: 'text'
          }
        ],
        numDirs: 1,
        numFiles: 1,
        sorting: { by: 'name', asc: true }
      }));
    }
    if (pathname.startsWith('/drive/api/search')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify([
        {
          path: '/notes.txt',
          name: 'notes.txt',
          size: 1024,
          extension: '.txt',
          modified: '2026-11-05T12:00:00Z',
          mode: 420,
          isDir: false,
          isSymlink: false,
          type: 'text'
        }
      ]));
    }
    if (pathname.startsWith('/drive/api/usage')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ used: 1048576, total: 104857600 }));
    }
    if (pathname.startsWith('/drive/api/users/')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ id: 1, username: 'astro', perm: { admin: true } }));
    }
    if (pathname.startsWith('/drive/api/raw')) {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      return res.end('Sample downloaded content');
    }
    if (pathname.startsWith('/drive/api/tus')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ status: 'ok' }));
    }

    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not Found');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;
  console.log(`Test server running at ${baseUrl}`);

  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    const context = await browser.newContext();
    const pageErrors = [];
    context.on('page', p => p.on('pageerror', err => pageErrors.push(err.message)));

    // =========================================================================
    // TEST 1: NATIVE FILEBROWSER MOUNT & 29 EXPORTS VERIFICATION
    // =========================================================================
    console.log('--- TEST 1: Native FileBrowser Mount & Module Exports ---');
    const cloudPage = await context.newPage();
    const imageRequests = [];
    cloudPage.on('request', r => { if (/\/api\/wallpapers\/.+\/display\.webp/.test(r.url())) imageRequests.push(r.url()); });
    cloudPage.on('console', msg => console.log('BROWSER CONSOLE:', msg.type(), msg.text()));
    await cloudPage.setViewportSize({ width: 1280, height: 900 });

    // Seed JWT in localStorage before load for proxy auth
    await cloudPage.addInitScript((jwt) => {
      localStorage.setItem('jwt', jwt);
    }, makeSyntheticJwt());

    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.waitForSelector('#app', { state: 'attached', timeout: 5000 });
    await cloudPage.locator('.item:not(.header)').first().waitFor({ state: 'visible', timeout: 5000 });

    await cloudPage.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--app-wallpaper-image').includes('url('));
    const nativeStyles = await cloudPage.evaluate(() => {
      const crumbs = document.querySelector('#breadcrumbs, .breadcrumbs');
      const item = document.querySelector('.item:not(.header)');
      return { breadcrumbs: crumbs && getComputedStyle(crumbs).backgroundColor, rowBlur: item && getComputedStyle(item).backdropFilter, isolated: getComputedStyle(document.body).isolation };
    });
    assert(nativeStyles.breadcrumbs && !nativeStyles.breadcrumbs.includes('255, 255, 255'), 'Native breadcrumbs must use the dark theme');
    assert.equal(nativeStyles.rowBlur, 'none', 'File rows must not create repeated backdrop filters');
    assert.equal(nativeStyles.isolated, 'isolate', 'Wallpaper layers must remain above the body background');

    // Verify no module errors occurred
    assert.equal(pageErrors.length, 0, `Page errors detected during mount: ${pageErrors.join('; ')}`);

    // Verify all 29 named exports from filebrowser-entry.js
    const exportCheck = await cloudPage.evaluate(async (base) => {
      const entryMod = await import(`${base}/drive/static/assets/index-BqkXVoMb.js`);
      const fbMod = await import(`${base}/drive/static/assets/mobile-filebrowser.js`);

      const entryKeys = Object.keys(entryMod).sort();
      const fbKeys = Object.keys(fbMod).sort();
      const expected29 = ['C','D','E','O','S','T','_','a','b','c','d','f','g','h','i','k','l','m','n','o','p','r','s','t','u','v','w','x','y'].sort();
      const entryNamed29 = entryKeys.filter(k => k !== '__vite_legacy_guard');

      return {
        entryCount: entryNamed29.length,
        fbCount: fbKeys.length,
        entryMatchesFb: JSON.stringify(entryKeys) === JSON.stringify(fbKeys),
        hasAll29: JSON.stringify(entryNamed29) === JSON.stringify(expected29),
        appearanceInitialized: window.__MOBILE_APPEARANCE_INITIALIZED__ === true
      };
    }, baseUrl);

    console.log('Module Exports Check:', exportCheck);
    assert.equal(exportCheck.entryCount, 29, 'Entry wrapper must have exactly 29 named exports');
    assert.equal(exportCheck.hasAll29, true, 'Entry wrapper must export all 29 expected names');
    assert.equal(exportCheck.entryMatchesFb, true, 'Entry wrapper exports must match mobile-filebrowser.js');
    assert.equal(exportCheck.appearanceInitialized, true, 'Appearance controller must initialize once');

    // Test dynamic chunk loading for lazy Editor & Preview modules
    const lazyCheck = await cloudPage.evaluate(async (base) => {
      const editor = await import(`${base}/drive/static/assets/Editor-DU8e4lG3.js`);
      const preview = await import(`${base}/drive/static/assets/Preview-CaXSHOvx.js`);
      return {
        editorLoaded: typeof editor === 'object',
        previewLoaded: typeof preview === 'object'
      };
    }, baseUrl);
    console.log('Lazy Chunks Loading Check:', lazyCheck);
    assert.equal(lazyCheck.editorLoaded, true, 'Lazy Editor chunk must load cleanly');
    assert.equal(lazyCheck.previewLoaded, true, 'Lazy Preview chunk must load cleanly');

    // =========================================================================
    // TEST 2: CLOUD APPEARANCE CONTROLS, DIALOG & PERSISTENCE
    // =========================================================================
    console.log('--- TEST 2: Cloud Appearance Controls & Interactivity ---');

    // Verify Appearance button is mounted into native header
    const cloudBtn = cloudPage.locator('#cloud-appearance-btn');
    await cloudBtn.waitFor({ state: 'visible', timeout: 3000 });
    assert.equal(await cloudBtn.isVisible(), true, 'Cloud appearance button must mount in chrome');

    // Click Appearance button to open dialog
    await cloudBtn.click();
    const dialog = cloudPage.locator('#appearance-dialog');
    await dialog.waitFor({ state: 'visible', timeout: 3000 });

    const requestsBeforeSliders = imageRequests.length;
    // Test Blur slider (set to 80%)
    const blurInput = cloudPage.locator('#appearance-blur-input');
    await blurInput.fill('80');
    await blurInput.dispatchEvent('input');
    const computedBlur = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-blur').trim());
    assert.equal(computedBlur, '24.0px', 'Blur 80% must evaluate linearly to 24.0px');

    // Test Translucency slider (set to 60%)
    const transInput = cloudPage.locator('#appearance-translucency-input');
    await transInput.fill('60');
    await transInput.dispatchEvent('input');
    const computedAlpha = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim());
    assert.equal(computedAlpha, '0.400', 'Translucency 60% must evaluate to panel alpha 0.400');

    await cloudPage.waitForTimeout(100);
    assert.equal(imageRequests.length, requestsBeforeSliders, 'Slider changes must not reload wallpaper images');

    await transInput.fill('100');
    await transInput.dispatchEvent('input');
    assert.equal(await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim()), '0.000');
    await transInput.fill('0');
    await transInput.dispatchEvent('input');
    assert.equal(await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim()), '1.000');

    // Test Wallpaper selection in gallery
    const thumbs = cloudPage.locator('.appearance-thumb-btn');
    await thumbs.first().waitFor({ state: 'visible', timeout: 3000 });
    const thumbCount = await thumbs.count();
    assert.equal(thumbCount >= 2, true, 'Gallery must display available wallpapers');

    await thumbs.nth(1).click();
    const isSelected = await thumbs.nth(1).getAttribute('aria-pressed');
    assert.equal(isSelected, 'true', 'Clicked wallpaper must be marked aria-pressed=true');

    // Test Reset Button
    const resetBtn = cloudPage.locator('#appearance-reset-btn');
    await resetBtn.click();
    const resetBlur = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-blur').trim());
    const resetAlpha = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim());
    assert.equal(resetBlur, '15.0px', 'Reset must restore default blur 50% (15px)');
    assert.equal(resetAlpha, '0.650', 'Reset must restore default translucency 35% (0.65 alpha)');

    // Test Keyboard Escape & Focus Restoration
    await cloudPage.keyboard.press('Escape');
    await cloudPage.waitForTimeout(200);
    const dialogOpen = await dialog.evaluate(el => el.hasAttribute('open'));
    assert.equal(dialogOpen, false, 'Escape key must close appearance dialog');
    const activeIsTrigger = await cloudPage.evaluate(() => document.activeElement === document.getElementById('cloud-appearance-btn'));
    assert.equal(activeIsTrigger, true, 'Closing dialog must restore focus to trigger button');

    // Test 48px touch targets
    await cloudBtn.click();
    const touchTargetChecks = await cloudPage.evaluate(() => {
      const ids = ['cloud-appearance-btn', 'appearance-close', 'appearance-reset-btn', 'appearance-done-btn'];
      const results = {};
      ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) {
          const rect = el.getBoundingClientRect();
          results[id] = { w: rect.width, h: rect.height, ok: rect.width >= 48 && rect.height >= 48 };
        }
      });
      return results;
    });
    console.log('Touch Target Checks (>=48px):', touchTargetChecks);
    assert.equal(touchTargetChecks['appearance-close'].ok, true);
    assert.equal(touchTargetChecks['appearance-reset-btn'].ok, true);
    assert.equal(touchTargetChecks['appearance-done-btn'].ok, true);

    // Set custom values and test reload persistence
    await blurInput.fill('70');
    await blurInput.dispatchEvent('input');
    await transInput.fill('40');
    await transInput.dispatchEvent('input');
    await cloudPage.locator('#appearance-done-btn').click();

    // Reload page & verify persistence from localStorage
    await cloudPage.reload();
    await cloudPage.waitForSelector('#app', { state: 'attached' });
    const reloadedBlur = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-blur').trim());
    const reloadedAlpha = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim());
    assert.equal(reloadedBlur, '21.0px', 'Blur 70% must persist across reloads (21.0px)');
    assert.equal(reloadedAlpha, '0.600', 'Translucency 40% must persist across reloads (0.600 alpha)');

    await cloudPage.locator('.spinner').waitFor({state:'hidden'});
    await cloudPage.locator('.item:not(.header)').first().waitFor({state:'visible'});
    // Capture screenshot 1: Desktop Cloud
    const desktopScreenshotPath = path.join(sddDir, 'desktopcloud.png');
    await cloudPage.screenshot({ path: desktopScreenshotPath, fullPage: false });
        console.log('Saved screenshot: desktopcloud.png');

    // =========================================================================
    // TEST 3: MOBILE CLOUD VIEWPORT (390x844)
    // =========================================================================
    console.log('--- TEST 3: Mobile Viewport FileBrowser Layout ---');
    await cloudPage.setViewportSize({ width: 390, height: 844 });
    await cloudPage.waitForTimeout(300);

    const mobileBtnVisible = await cloudPage.locator('#cloud-appearance-btn').isVisible();
    assert.equal(mobileBtnVisible, true, 'Appearance button must remain visible and accessible on mobile');

    await cloudPage.locator('.spinner').waitFor({state:'hidden'});
    // Capture screenshot 2: Mobile Cloud
    const mobileScreenshotPath = path.join(sddDir, 'mobilecloud.png');
    await cloudPage.screenshot({ path: mobileScreenshotPath, fullPage: false });
        console.log('Saved screenshot: mobilecloud.png');

    // =========================================================================
    // TEST 4: PORTAL APPEARANCE & CROSS-PAGE PERSISTENCE
    // =========================================================================
    console.log('--- TEST 4: Portal Appearance & Cross-Page Persistence ---');
    await cloudPage.goto(`${baseUrl}/admin`);
    await cloudPage.waitForSelector('#btn-appearance', { state: 'visible' });

    // Verify stored appearance carried over from cloud to portal
    const portalBlur = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-blur').trim());
    const portalAlpha = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim());
    assert.equal(portalBlur, '21.0px', 'Appearance blur must persist across portal and cloud');
    assert.equal(portalAlpha, '0.600', 'Appearance translucency must persist across portal and cloud');

    // Open Appearance dialog on portal page
    await cloudPage.locator('#btn-appearance').click();
    await cloudPage.locator('#appearance-dialog').waitFor({ state: 'visible' });

    // Capture screenshot 3: Portal Appearance
    const portalScreenshotPath = path.join(sddDir, 'portalappearance.png');
    await cloudPage.screenshot({ path: portalScreenshotPath, fullPage: false });
        console.log('Saved screenshot: portalappearance.png');
    await cloudPage.locator('#appearance-close').click();

    // =========================================================================
    // TEST 5: ROLE ISOLATION - LOGIN & FAMILY GATING & BACKGROUND PRESERVATION
    // =========================================================================
    console.log('--- TEST 5: Login & Family Role Restrictions & Styling Preservation ---');

    // 5A: Login Page - trigger absent, openDialog fails closed, no private wallpaper leak
    authCheckAdminRole = 'anonymous';
    wallpaperApiAuthRequired = true; // /api/wallpapers returns 401
    await cloudPage.goto(`${baseUrl}/login`);
    await cloudPage.waitForSelector('#login-form', { state: 'visible' });

    const loginTriggerCount = await cloudPage.locator('#btn-appearance').count();
    assert.equal(loginTriggerCount, 0, 'Appearance button must be completely absent from login template');

    // Direct invocation on login must fail closed
    await cloudPage.evaluate(() => window.MobileAppearance.openDialog());
    await cloudPage.waitForTimeout(100);
    const loginDialogCount = await cloudPage.locator('#appearance-dialog').count();
    assert.equal(loginDialogCount, 0, 'Direct openDialog on login page must fail closed and never create dialog');

    // 5B: Family Portal - trigger absent, direct openDialog denied, background & styling intact
    authCheckAdminRole = 'family';
    wallpaperApiAuthRequired = false; // /api/wallpapers 200 authenticated
    await cloudPage.goto(`${baseUrl}/family`);
    await cloudPage.waitForSelector('.family-services-grid', { state: 'visible' });

    const familyTriggerCount = await cloudPage.locator('#btn-appearance').count();
    assert.equal(familyTriggerCount, 0, 'Appearance button must be completely absent from family template');

    // Direct invocation on family portal must fail closed
    await cloudPage.evaluate(() => window.MobileAppearance.openDialog());
    await cloudPage.waitForTimeout(100);
    const familyDialogCount = await cloudPage.locator('#appearance-dialog').count();
    assert.equal(familyDialogCount, 0, 'Direct openDialog on family page must fail closed');

    // Styling & background wallpaper must still load cleanly for family
    await cloudPage.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--app-wallpaper-image').includes('url('));
    const familyWallpaper = await cloudPage.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-wallpaper-image'));
    assert(familyWallpaper.includes('url('), 'Family portal must preserve wallpaper background rendering');
    const familyBackdrop = await cloudPage.evaluate(() => {
      const b = document.getElementById('app-wallpaper-backdrop');
      const t = document.getElementById('app-wallpaper-tint');
      return !!b && !!t;
    });
    assert.equal(familyBackdrop, true, 'Family portal must preserve backdrop and tint layers');

    // 5C: Family Native Cloud (FileBrowser) - cloud button absent, direct openDialog denied, styling loaded
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.waitForSelector('#app', { state: 'attached' });
    await cloudPage.locator('.item:not(.header)').first().waitFor({ state: 'visible', timeout: 5000 });
    await cloudPage.waitForTimeout(300);

    const familyCloudBtnCount = await cloudPage.locator('#cloud-appearance-btn').count();
    assert.equal(familyCloudBtnCount, 0, 'Cloud appearance button must never mount for non-admin/family role');

    await cloudPage.evaluate(() => window.MobileAppearance.openDialog());
    await cloudPage.waitForTimeout(100);
    const familyCloudDialogCount = await cloudPage.locator('#appearance-dialog').count();
    assert.equal(familyCloudDialogCount, 0, 'Direct openDialog in cloud for family must fail closed');

    // Restore admin role for subsequent tests
    authCheckAdminRole = 'admin';

    // =========================================================================
    // TEST 6: ERROR HANDLING & EDGE CASES
    // =========================================================================
    console.log('--- TEST 6: Resilience, Error Handling & Fallbacks ---');

    // A. Corrupt localStorage fallback
    await cloudPage.evaluate(() => {
      localStorage.setItem('mobile.appearance.v1', '{invalid-json');
    });
    await cloudPage.reload();
    const corruptBlur = await cloudPage.evaluate(() => window.MobileAppearance.getState().blur);
    assert.equal(corruptBlur, 50, 'Corrupt storage must gracefully revert to default blur 50%');

    // B. Missing image fallback
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.route('**/api/wallpapers/**/display.webp', route => route.fulfill({status:404, body:'Missing image'}));
    await cloudPage.reload();
    await cloudPage.locator('#cloud-appearance-btn').click();
    await cloudPage.locator('.appearance-thumb-btn').last().click();
    await cloudPage.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--app-wallpaper-image').trim() === 'none');
    await cloudPage.unroute('**/api/wallpapers/**/display.webp');

    // C. prefers-reduced-transparency CSS evaluation
    const session = await context.newCDPSession(cloudPage);
    await session.send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-transparency',value:'reduce'}]});
    const reducedTransSolid = await cloudPage.evaluate(() => ({matches:matchMedia('(prefers-reduced-transparency: reduce)').matches, alpha:getComputedStyle(document.documentElement).getPropertyValue('--app-panel-alpha').trim(), backdrop:getComputedStyle(document.querySelector('#app-wallpaper-backdrop')).display}));
    assert.equal(reducedTransSolid.matches, true);
    assert.equal(reducedTransSolid.alpha, '1');
    assert.equal(reducedTransSolid.backdrop, 'none');
    await session.send('Emulation.setEmulatedMedia', {features:[]});
    await session.detach();

    wallpaperApiHang = true;
    await cloudPage.reload();
    await cloudPage.locator('#cloud-appearance-btn').click();
    await cloudPage.locator('.appearance-error').waitFor({state:'visible',timeout:12000});
    wallpaperApiHang = false;
    await cloudPage.locator('#appearance-retry-btn').click();
    await cloudPage.locator('.appearance-thumb-btn').first().waitFor({state:'visible'});

    // E. Rejected / Network-failed auth-check-admin (500 status) fails closed
    authCheckAdminRole = null;
    authCheckAdminStatus = 500;
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.waitForSelector('#app', { state: 'attached' });
    await cloudPage.locator('.item:not(.header)').first().waitFor({ state: 'visible' });
    await cloudPage.waitForTimeout(300);
    assert.equal(await cloudPage.locator('#cloud-appearance-btn').count(), 0, 'Cloud appearance button must not mount when auth-check fails');
    await cloudPage.evaluate(() => window.MobileAppearance.openDialog());
    await cloudPage.waitForTimeout(100);
    assert.equal(await cloudPage.locator('#appearance-dialog').count(), 0, 'openDialog must fail closed on 500 auth failure');

    // A login page returning 200 after an auth redirect must never grant admin.
    authCheckAdminStatus = 302;
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.locator('.item:not(.header)').first().waitFor({state:'visible'});
    await cloudPage.evaluate(() => window.MobileAppearance.openDialog());
    assert.equal(await cloudPage.locator('#cloud-appearance-btn').count(), 0, 'Auth redirect must not mount admin controls');
    assert.equal(await cloudPage.locator('#appearance-dialog').count(), 0, 'Auth redirect must not create settings dialog');

    // F. Network hang/timeout on auth-check-admin fails closed
    authCheckAdminStatus = null;
    authCheckAdminHang = true;
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.waitForSelector('#app', { state: 'attached' });
    await cloudPage.locator('.item:not(.header)').first().waitFor({ state: 'visible' });
    await cloudPage.waitForTimeout(300);
    assert.equal(await cloudPage.locator('#cloud-appearance-btn').count(), 0, 'Cloud appearance button must not mount when auth-check hangs');
    await cloudPage.evaluate(() => { window.MobileAppearance.openDialog(); });
    await cloudPage.waitForTimeout(100);
    assert.equal(await cloudPage.locator('#appearance-dialog').count(), 0, 'openDialog must fail closed when auth-check hangs');
    authCheckAdminHang = false;
    authCheckAdminRole = 'admin';

    // =========================================================================
    // TEST 7: FILEBROWSER NATIVE ACTIONS & DIALOGS
    // =========================================================================
    console.log('--- TEST 7: FileBrowser Native File Actions & Search ---');
    await cloudPage.setViewportSize({width:1280,height:900});
    await cloudPage.goto(`${baseUrl}/drive/files/`);
    await cloudPage.locator('.item:not(.header)').first().waitFor({ state: 'visible', timeout: 5000 });
    await cloudPage.locator('.spinner').waitFor({state:'hidden'});
    await cloudPage.locator('nav .action[aria-label="New folder"]').click();
    await cloudPage.getByRole('button', {name:'Cancel',exact:true}).waitFor({state:'visible'});
    await cloudPage.getByRole('button', {name:'Cancel',exact:true}).click();
    await cloudPage.locator('.item').filter({hasText:'notes.txt'}).dblclick();
    await cloudPage.locator('.ace_editor').waitFor({state:'visible',timeout:5000});
    assert((await cloudPage.locator('.ace_editor').textContent()).includes('Sample downloaded content'));
    await cloudPage.goto(`${baseUrl}/drive/files/`);

    // Item selection
    await cloudPage.locator('.spinner').waitFor({state:'hidden'});
    const nativeActions = await cloudPage.locator('nav .action, header .action').evaluateAll(nodes => nodes.map(n => ({id:n.id,label:n.getAttribute('aria-label'),text:n.textContent.trim().slice(0,50)})));
    console.log('Native action hooks:', JSON.stringify(nativeActions));
    const firstItem = cloudPage.locator('.item:not(.header)').first();
    await firstItem.click();
    await cloudPage.waitForTimeout(200);

    // Search bar input
    const searchBtn = cloudPage.locator('.search-button, [aria-label*="search"], [aria-label*="Search"]').first();
    if (await searchBtn.isVisible()) {
      await searchBtn.click();
      const searchInput = cloudPage.locator('input[type="search"], input[type="text"]').first();
      if (await searchInput.isVisible()) {
        await searchInput.fill('notes');
        await cloudPage.waitForTimeout(200);
      }
    }

    console.log('====================================================');
    console.log('CLOUD APPEARANCE ACCEPTANCE PASS: All tests passed successfully.');
    console.log('====================================================');

    await cloudPage.close();
  } finally {
    await browser.close();
    server.close();
  }
})();
