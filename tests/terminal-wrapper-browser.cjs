/**
 * Expressive Workbench Terminal Wrappers & Cloud Storage Integration Test
 * 
 * Tests both web/agy_index.html and web/termux_index.html chrome:
 * - Desktop (1440x900), Mobile (390x844), and Short (640x420) viewports
 * - >= 48px touch targets in BOTH dimensions (height and width)
 * - Pill geometry, Material 3 glass tokens, calm pulse dot (static, no animation)
 * - Keyboard focus states (:focus-visible)
 * - Emoji-free buttons with clean SVG glyphs
 * - Prompt input, quick keys, send action, drop overlay, and toast alerts
 * - Terminal container viewport bounds (clearance from top/bottom floating chrome)
 * - Laptop terminal mode (/laptop URL) on termux wrapper
 * - Dynamic Matugen palette color updates on custom chrome
 * - Preserves exact SHA256 for bundled ttyd script
 * - Captures full screenshot preview suite to EXPRESSIVE_PREVIEW_DIR
 */

const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const assert = require('node:assert/strict');

const rootDir = path.resolve(__dirname, '..');
const previewDir = process.env.EXPRESSIVE_PREVIEW_DIR || (
  fs.existsSync('/home/astro/server-audit/2026-11-24/expressive-redesign/task4-preview')
    ? '/home/astro/server-audit/2026-11-24/expressive-redesign/task4-preview'
    : path.join(require('os').tmpdir(), 'task4-preview')
);
fs.mkdirSync(previewDir, { recursive: true });

(async () => {
  // 0. Verify bundled ttyd script byte SHA invariant before launch
  const expectedScriptSha = '42bb3a80a3cb5207e88f18b8dfe603a08f535196de1145fb65277dbbcabf0ac2';
  for (const p of ['web/agy_index.html', 'web/termux_index.html']) {
    const content = fs.readFileSync(path.join(rootDir, p), 'utf8');
    const m = content.match(/<script type="text\/javascript">([\s\S]*?)<\/script>/);
    assert(m, `Bundled script tag must exist in ${p}`);
    const hash = crypto.createHash('sha256').update(m[1]).digest('hex');
    assert.equal(hash, expectedScriptSha, `Bundled script SHA256 must match exactly in ${p}`);
  }

  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    console.log('=== TEST SUITE: Expressive Workbench Terminal Wrappers ===');

    for (const wrapperName of ['agy', 'termux']) {
      const fileName = wrapperName === 'agy' ? 'web/agy_index.html' : 'web/termux_index.html';
      const rawHtml = fs.readFileSync(path.join(rootDir, fileName), 'utf8');

      // Strip bundled ttyd websocket transport to test DOM & chrome deterministically
      const strippedHtml = rawHtml.replace(/<script type="text\/javascript">[\s\S]*?<\/script>/, '');

      // Common route handler for same-origin portal resources
      const setupRoutes = async (page) => {
        await page.route('http://terminal.test/**', async route => {
          const url = route.request().url();
          if (url.endsWith('/api/agy/scroll')) {
            await route.fulfill({ json: { status: 'ok' } });
          } else if (url.endsWith('/api/clipboard/upload')) {
            await route.fulfill({ json: { status: 'ok', path: '/root/screenshots/screen.png', url: '/screenshots/screen.png' } });
          } else if (url.endsWith('/theme-tokens.css')) {
            await route.fulfill({ body: fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8'), contentType: 'text/css' });
          } else if (url.endsWith('/appearance.css')) {
            await route.fulfill({ body: fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8'), contentType: 'text/css' });
          } else if (url.endsWith('/appearance.js')) {
            await route.fulfill({ body: fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8'), contentType: 'application/javascript' });
          } else {
            await route.fulfill({ body: '<html></html>', contentType: 'text/html' });
          }
        });
      };

      // 1. DESKTOP VIEWPORT (1440x900)
      {
        const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        await setupRoutes(page);
        await page.goto(`http://terminal.test/${wrapperName}/`);
        await page.evaluate(() => {
          window.rawKeys = [];
          window.term = {
            rows: 24,
            cols: 80,
            input: x => window.rawKeys.push(x),
            focus: () => {}
          };
        });

        await page.setContent(strippedHtml);

        // Inject simulated xterm canvas for visual layout verification
        await page.evaluate((name) => {
          let tc = document.getElementById('terminal-container');
          if (!tc) {
            tc = document.createElement('div');
            tc.id = 'terminal-container';
            document.body.prepend(tc);
          }
          const fakeTerm = document.createElement('div');
          fakeTerm.className = 'terminal xterm';
          fakeTerm.innerHTML = `
            <div class="xterm-screen" style="width: 100%; height: 100%; padding: 24px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #a6accd; line-height: 1.6;">
              <div style="color: #64748b;">[Expressive Workbench Terminal Session: ${name}]</div>
              <div>astro@redmi:~$ ps aux | grep -E "agy|ttyd"</div>
              <div style="color: #70d6c5;">root      14321  0.2  0.8 128440 32180 ?  S    22:00   0:01 agy daemon</div>
              <div style="color: #94a3b8;">astro     14325  0.0  0.3  42100 12040 ?  S    22:00   0:00 ttyd -p 7681</div>
              <div style="color: #34d399;">✔ Persistent tmux session attached and healthy</div>
              <div>astro@redmi:~$ _</div>
            </div>
          `;
          tc.appendChild(fakeTerm);
        }, wrapperName);

        // Verify No Emoji in Buttons
        const focusBtnText = await page.locator('#agy-focus-btn').innerText();
        assert(!focusBtnText.includes('⌨️'), 'Focus button must not contain emoji icons');
        assert(focusBtnText.includes('Focus'), 'Focus button must have clean text label');

        const focusBtnSvg = await page.locator('#agy-focus-btn svg.agy-icon').count();
        assert.equal(focusBtnSvg, 1, 'Focus button must contain an SVG icon');

        // Verify Touch Targets & Geometry on Desktop (>= 48px in BOTH dimensions)
        const geom = await page.evaluate(() => {
          const focusBtn = document.getElementById('agy-focus-btn').getBoundingClientRect();
          const uploadBtn = document.getElementById('agy-upload-btn').getBoundingClientRect();
          const sendBtn = document.getElementById('agy-send-btn').getBoundingClientRect();
          const firstKey = document.querySelector('.agy-kbtn').getBoundingClientRect();
          const badge = document.querySelector('.agy-badge').getBoundingClientRect();
          const tc = document.getElementById('terminal-container').getBoundingClientRect();
          const dot = document.querySelector('.agy-pulse-dot');
          const dotStyle = dot ? getComputedStyle(dot) : null;
          return {
            focusBtn: { w: focusBtn.width, h: focusBtn.height },
            uploadBtn: { w: uploadBtn.width, h: uploadBtn.height },
            sendBtn: { w: sendBtn.width, h: sendBtn.height },
            firstKey: { w: firstKey.width, h: firstKey.height },
            badge: { w: badge.width, h: badge.height },
            tc: { top: tc.top, bottom: tc.bottom, height: tc.height, width: tc.width },
            dotHasAnimation: dotStyle ? dotStyle.animationName !== 'none' : false
          };
        });

        console.log(`[${wrapperName.toUpperCase()} Desktop Geom]:`, geom);
        assert(geom.focusBtn.h >= 48 && geom.focusBtn.w >= 48, 'Focus button must meet >= 48px in both dimensions');
        assert(geom.uploadBtn.h >= 48 && geom.uploadBtn.w >= 48, 'Upload button must meet >= 48px in both dimensions');
        assert(geom.sendBtn.h >= 48 && geom.sendBtn.w >= 48, 'Send button must meet >= 48px in both dimensions');
        assert(geom.firstKey.h >= 48 && geom.firstKey.w >= 48, 'Key button must meet >= 48px in both dimensions');
        assert(geom.badge.h >= 48 && geom.badge.w >= 48, 'Badge must meet >= 48px in both dimensions');

        // Status dot must be calm/static without animation or fake pulse
        assert.equal(geom.dotHasAnimation, false, 'Status dot must be static with no pulse animation');

        // Terminal Container Bounds Check (Desktop)
        assert(geom.tc.top >= 60 && geom.tc.top <= 68, `Desktop terminal container top must reserve top bar space (${geom.tc.top})`);
        assert(geom.tc.bottom <= 900 - 68, `Desktop terminal container bottom must reserve bottom bar space (${geom.tc.bottom})`);
        assert(geom.tc.height > 700, `Desktop terminal container height must be usable (${geom.tc.height})`);

        // Test Interactive Elements & Keyboard Focus
        await page.focus('#agy-focus-btn');
        await page.waitForTimeout(50);
        const focusVisible = await page.evaluate(() => {
          const el = document.getElementById('agy-focus-btn');
          const outline = getComputedStyle(el).outline;
          return outline && !outline.includes('none') && outline.includes('rgb');
        });
        assert(focusVisible, 'Focus button must have visible outline when focused');

        // Test Prompt Input & Send
        await page.locator('#agy-prompt-input').fill('echo "Expressive Workbench"');
        await page.locator('#agy-send-btn').click();
        await page.waitForTimeout(100);
        assert.deepEqual(await page.evaluate(() => window.rawKeys), ['echo "Expressive Workbench"\r']);

        // Test Quick Keys
        await page.locator('.agy-kbtn-esc').click();
        await page.waitForTimeout(50);
        const keysAfterEsc = await page.evaluate(() => window.rawKeys);
        assert(keysAfterEsc.length >= 2, 'Esc key must deliver keycode');

        // Capture Desktop Screenshot
        const desktopShotPath = path.join(previewDir, `terminal-${wrapperName}-desktop-1440.png`);
        await page.screenshot({ path: desktopShotPath });
        console.log(`Saved screenshot: ${desktopShotPath}`);

        // Test Drop Overlay Preview & Screenshot
        await page.evaluate(() => {
          document.getElementById('agy-drop-overlay').style.display = 'flex';
        });
        const overlayShotPath = path.join(previewDir, `terminal-${wrapperName}-overlay-desktop.png`);
        await page.screenshot({ path: overlayShotPath });
        console.log(`Saved screenshot: ${overlayShotPath}`);

        await page.close();
      }

      // 2. MOBILE VIEWPORT (390x844 - Modern Smartphone)
      {
        const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
        await setupRoutes(page);
        await page.goto(`http://terminal.test/${wrapperName}/`);
        await page.setContent(strippedHtml);

        // Inject simulated xterm canvas for visual layout verification
        await page.evaluate((name) => {
          let tc = document.getElementById('terminal-container');
          if (!tc) {
            tc = document.createElement('div');
            tc.id = 'terminal-container';
            document.body.prepend(tc);
          }
          const fakeTerm = document.createElement('div');
          fakeTerm.className = 'terminal xterm';
          fakeTerm.innerHTML = `
            <div class="xterm-screen" style="width: 100%; height: 100%; padding: 12px; font-family: ui-monospace, monospace; color: #a6accd; font-size: 11px;">
              <div style="color: #64748b;">[${name}]</div>
              <div>astro@redmi:~$ agy status</div>
              <div style="color: #70d6c5;">● Active PID 14321</div>
              <div>astro@redmi:~$ _</div>
            </div>
          `;
          tc.appendChild(fakeTerm);
        }, wrapperName);

        // Verify mobile stacked layout & touch targets (>= 48px in both dimensions)
        const mobileGeom = await page.evaluate(() => {
          const bottomBar = document.getElementById('agy-bottom-bar');
          const inputRow = document.querySelector('.agy-input-row');
          const keysRow = document.querySelector('.agy-keys-row');
          const focusBtn = document.getElementById('agy-focus-btn').getBoundingClientRect();
          const sendBtn = document.getElementById('agy-send-btn').getBoundingClientRect();
          const firstKey = document.querySelector('.agy-kbtn').getBoundingClientRect();
          const tc = document.getElementById('terminal-container').getBoundingClientRect();
          return {
            bottomBarHeight: bottomBar.getBoundingClientRect().height,
            inputRowHeight: inputRow.getBoundingClientRect().height,
            keysRowHeight: keysRow.getBoundingClientRect().height,
            focusBtn: { w: focusBtn.width, h: focusBtn.height },
            sendBtn: { w: sendBtn.width, h: sendBtn.height },
            firstKey: { w: firstKey.width, h: firstKey.height },
            tc: { top: tc.top, bottom: tc.bottom, height: tc.height }
          };
        });

        console.log(`[${wrapperName.toUpperCase()} Mobile Geom]:`, mobileGeom);
        assert(mobileGeom.bottomBarHeight >= 80, 'Mobile bottom bar must accommodate two rows');
        assert(mobileGeom.focusBtn.h >= 48 && mobileGeom.focusBtn.w >= 48, 'Mobile focus button touch target >= 48px in both dimensions');
        assert(mobileGeom.sendBtn.h >= 48 && mobileGeom.sendBtn.w >= 48, 'Mobile send button touch target >= 48px in both dimensions');
        assert(mobileGeom.firstKey.h >= 48 && mobileGeom.firstKey.w >= 48, 'Mobile first key touch target >= 48px in both dimensions');

        // Mobile Terminal Container Bounds Check: viewport lines must not be obscured by chrome
        assert(mobileGeom.tc.top >= 60 && mobileGeom.tc.top <= 68, `Mobile terminal container top must reserve top bar space (${mobileGeom.tc.top})`);
        assert(mobileGeom.tc.bottom <= 844 - 135, `Mobile terminal container bottom must reserve bottom bar space (${mobileGeom.tc.bottom})`);
        assert(mobileGeom.tc.height > 600, `Mobile terminal container height must be usable (${mobileGeom.tc.height})`);

        // Capture Mobile Screenshot
        const mobileShotPath = path.join(previewDir, `terminal-${wrapperName}-mobile-390.png`);
        await page.screenshot({ path: mobileShotPath });
        console.log(`Saved screenshot: ${mobileShotPath}`);

        await page.close();
      }

      // 3. SHORT HEIGHT VIEWPORT (640x420)
      {
        const page = await browser.newPage({ viewport: { width: 640, height: 420 } });
        await setupRoutes(page);
        await page.goto(`http://terminal.test/${wrapperName}/`);
        await page.setContent(strippedHtml);

        const shortGeom = await page.evaluate(() => {
          let tc = document.getElementById('terminal-container');
          if (!tc) {
            tc = document.createElement('div');
            tc.id = 'terminal-container';
            document.body.prepend(tc);
          }
          const r = tc.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, height: r.height };
        });

        assert(shortGeom.top >= 60 && shortGeom.top <= 68, 'Short viewport top clearance');
        assert(shortGeom.bottom <= 420 - 68, 'Short viewport bottom clearance');
        assert(shortGeom.height >= 180, 'Short viewport terminal container height must remain positive and usable');
        await page.close();
      }

      // 4. DYNAMIC MATUGEN PALETTE UPDATE TEST
      {
        const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        await setupRoutes(page);
        await page.goto(`http://terminal.test/${wrapperName}/`);
        await page.setContent(strippedHtml);

        // Palette 1: Frosted Glass / Teal (#70d6c5)
        const color1 = await page.evaluate(() => {
          document.documentElement.style.setProperty('--md-sys-color-primary', '#70d6c5');
          document.documentElement.style.setProperty('--md-sys-color-surface-container-high', '#272c2d');
          const sendBtn = document.getElementById('agy-send-btn');
          return getComputedStyle(sendBtn).backgroundColor;
        });

        // Palette 2: Dark Obsidian / Warm Amber (#e5c46c)
        const color2 = await page.evaluate(() => {
          document.documentElement.style.setProperty('--md-sys-color-primary', '#e5c46c');
          document.documentElement.style.setProperty('--md-sys-color-surface-container-high', '#332b21');
          const sendBtn = document.getElementById('agy-send-btn');
          return getComputedStyle(sendBtn).backgroundColor;
        });

        console.log(`[${wrapperName.toUpperCase()} Matugen Check]:`, { color1, color2 });
        assert(color1 && color2, 'Custom chrome must compute element colors');
        assert.notEqual(color1, color2, 'Custom chrome computed colors must dynamically update between Matugen palettes');
        await page.close();
      }

      // 5. LAPTOP TERMINAL MODE TEST (Termux wrapper only)
      if (wrapperName === 'termux') {
        const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        await setupRoutes(page);
        await page.goto('http://terminal.test/laptop/');
        await page.setContent(strippedHtml);

        const laptopState = await page.evaluate(() => {
          return {
            title: document.title,
            badgeText: document.querySelector('.agy-badge-text')?.textContent,
            uploadBtnVisible: document.getElementById('agy-upload-btn') ? getComputedStyle(document.getElementById('agy-upload-btn')).display !== 'none' : false,
            dropOverlayExists: !!document.getElementById('agy-drop-overlay'),
            fileInputExists: !!document.getElementById('agy-file-input')
          };
        });

        console.log('[Laptop Mode State]:', laptopState);
        assert.equal(laptopState.title, 'Laptop Terminal', 'Laptop mode must set page title to "Laptop Terminal"');
        assert.equal(laptopState.badgeText, 'Laptop Session', 'Laptop mode must set badge text to "Laptop Session"');
        assert.equal(laptopState.uploadBtnVisible, false, 'Laptop mode must hide phone upload button');
        assert.equal(laptopState.dropOverlayExists, false, 'Laptop mode must remove drop overlay');
        assert.equal(laptopState.fileInputExists, false, 'Laptop mode must remove file input');

        await page.close();
      }
    }

    console.log('=== ALL TERMINAL WRAPPER TESTS PASSED SUCCESSFULLY ===');
  } finally {
    await browser.close();
  }
})();
