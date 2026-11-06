/**
 * Shared test asset serving helper for appearance and theme tokens.
 * Used by existing browser test suites and cloud-appearance-browser.cjs.
 */
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '../..');
const themeTokensPath = path.join(rootDir, 'web/theme-tokens.css');
const appearanceCssPath = path.join(rootDir, 'web/appearance.css');
const appearanceJsPath = path.join(rootDir, 'web/appearance.js');
const portalCssPath = path.join(rootDir, 'web/portal.css');

function getThemeTokensCss() {
  return fs.existsSync(themeTokensPath) ? fs.readFileSync(themeTokensPath, 'utf8') : '';
}

function getAppearanceCss() {
  return fs.existsSync(appearanceCssPath) ? fs.readFileSync(appearanceCssPath, 'utf8') : '';
}

function getAppearanceJs() {
  return fs.existsSync(appearanceJsPath) ? fs.readFileSync(appearanceJsPath, 'utf8') : '';
}

function getPortalCss() {
  return fs.existsSync(portalCssPath) ? fs.readFileSync(portalCssPath, 'utf8') : '';
}

/**
 * Handles incoming HTTP requests for shared assets in node http servers.
 * Returns true if the request was handled, false otherwise.
 */
function handleSharedAssetHttp(req, res, wallpapersJson = null) {
  const parsed = new URL(req.url, 'http://127.0.0.1');
  const pathname = parsed.pathname;

  if (pathname === '/theme-tokens.css') {
    res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
    res.end(getThemeTokensCss());
    return true;
  }
  if (pathname === '/appearance.css') {
    res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
    res.end(getAppearanceCss());
    return true;
  }
  if (pathname === '/appearance.js') {
    res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
    res.end(getAppearanceJs());
    return true;
  }
  if (pathname === '/api/wallpapers') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(wallpapersJson || { version: 1, default_id: null, wallpapers: [] }));
    return true;
  }
  if (pathname === '/api/auth-check-admin') {
    const fwdUser = req.headers['x-forwarded-user'] || '';
    const cookie = req.headers['cookie'] || '';
    const referer = req.headers['referer'] || '';
    let role = 'anonymous';

    if (fwdUser === 'admin' || fwdUser === 'astro') {
      role = 'admin';
    } else if (fwdUser === 'family') {
      role = 'family';
    } else if (cookie.includes('session_user=admin') || cookie.includes('session_user=astro')) {
      role = 'admin';
    } else if (cookie.includes('session_user=family')) {
      role = 'family';
    } else if (referer.includes('/family')) {
      role = 'family';
    } else if (referer.includes('/login')) {
      role = 'anonymous';
    } else if (referer.includes('/admin') || referer.includes('/drive/')) {
      role = 'admin';
    }

    if (role === 'admin') {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8', 'X-Forwarded-User': 'astro' });
      res.end('OK');
      return true;
    }
    if (role === 'family') {
      res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Forbidden: Administrator privileges required.');
      return true;
    }
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'error', message: 'Unauthorized' }));
    return true;
  }
  return false;
}

/**
 * Attaches Playwright route interception for shared assets.
 */
async function attachSharedAssetRoutes(target, wallpapersJson = null) {
  await target.route('**/theme-tokens.css', route => {
    return route.fulfill({
      status: 200,
      contentType: 'text/css; charset=utf-8',
      body: getThemeTokensCss()
    });
  });
  await target.route('**/appearance.css', route => {
    return route.fulfill({
      status: 200,
      contentType: 'text/css; charset=utf-8',
      body: getAppearanceCss()
    });
  });
  await target.route('**/appearance.js', route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/javascript; charset=utf-8',
      body: getAppearanceJs()
    });
  });
  await target.route('**/api/wallpapers', route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(wallpapersJson || { version: 1, default_id: null, wallpapers: [] })
    });
  });
  await target.route('**/api/auth-check-admin', route => {
    const req = route.request();
    const headers = req.headers();
    const fwdUser = headers['x-forwarded-user'] || '';
    const cookie = headers['cookie'] || '';
    const referer = headers['referer'] || '';
    let role = 'anonymous';

    if (fwdUser === 'admin' || fwdUser === 'astro') {
      role = 'admin';
    } else if (fwdUser === 'family') {
      role = 'family';
    } else if (cookie.includes('session_user=admin') || cookie.includes('session_user=astro')) {
      role = 'admin';
    } else if (cookie.includes('session_user=family')) {
      role = 'family';
    } else if (referer.includes('/family')) {
      role = 'family';
    } else if (referer.includes('/login')) {
      role = 'anonymous';
    } else if (referer.includes('/admin') || referer.includes('/drive/')) {
      role = 'admin';
    }

    if (role === 'admin') {
      return route.fulfill({
        status: 200,
        contentType: 'text/plain; charset=utf-8',
        headers: { 'X-Forwarded-User': 'astro' },
        body: 'OK'
      });
    }
    if (role === 'family') {
      return route.fulfill({
        status: 403,
        contentType: 'text/plain; charset=utf-8',
        body: 'Forbidden: Administrator privileges required.'
      });
    }
    return route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'error', message: 'Unauthorized' })
    });
  });
}

module.exports = {
  getThemeTokensCss,
  getAppearanceCss,
  getAppearanceJs,
  getPortalCss,
  handleSharedAssetHttp,
  attachSharedAssetRoutes
};
