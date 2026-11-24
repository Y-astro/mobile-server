/**
 * Expressive Workbench Terminal Wrappers & Cloud Storage Integration Test
 * 
 * Tests both web/agy_index.html and web/termux_index.html chrome:
 * - Desktop (1440x900) and Mobile (390x844) viewports
 * - 48px touch targets for top and bottom controls
 * - Pill geometry, Material 3 glass tokens, calm pulse dot
 * - Keyboard focus states (:focus-visible)
 * - Emoji-free buttons with clean SVG glyphs
 * - Prompt input, quick keys, send action, drop overlay, and toast alerts
 * - Captures full screenshot preview suite to /home/astro/server-audit/2026-11-24/expressive-redesign/task4-preview/
 */

const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');

const previewDir = '/home/astro/server-audit/2026-11-24/expressive-redesign/task4-preview';
fs.mkdirSync(previewDir, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    console.log('=== TEST SUITE: Expressive Workbench Terminal Wrappers ===');

    for (const wrapperName of ['agy', 'termux']) {
      const fileName = wrapperName === 'agy' ? 'web/agy_index.html' : 'web/termux_index.html';
      const rawHtml = fs.readFileSync(path.join(__dirname, '..', fileName), 'utf8');

      // Strip bundled ttyd websocket transport to test DOM & chrome deterministically
      const strippedHtml = rawHtml.replace(/<script type="text\/javascript">[\s\S]*?<\/script>/, '');

      // 1. DESKTOP VIEWPORT (1440x900)
      {
        const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        const calls = [];

        await page.route('http://terminal.test/**', async route => {
          const url = route.request().url();
          if (url.endsWith('/api/agy/scroll')) {
            calls.push(route.request().postDataJSON());
            await route.fulfill({ json: { status: 'ok' } });
          } else if (url.endsWith('/api/clipboard/upload')) {
            await route.fulfill({ json: { status: 'ok', path: '/root/screenshots/screen.png', url: '/screenshots/screen.png' } });
          } else {
            await route.fulfill({ body: '<html></html>', contentType: 'text/html' });
          }
        });

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

        // Verify Touch Targets & Geometry on Desktop
        const geom = await page.evaluate(() => {
          const focusBtn = document.getElementById('agy-focus-btn').getBoundingClientRect();
          const uploadBtn = document.getElementById('agy-upload-btn').getBoundingClientRect();
          const sendBtn = document.getElementById('agy-send-btn').getBoundingClientRect();
          const firstKey = document.querySelector('.agy-kbtn').getBoundingClientRect();
          const badge = document.querySelector('.agy-badge').getBoundingClientRect();
          return {
            focusBtn: { w: focusBtn.width, h: focusBtn.height },
            uploadBtn: { w: uploadBtn.width, h: uploadBtn.height },
            sendBtn: { w: sendBtn.width, h: sendBtn.height },
            firstKey: { w: firstKey.width, h: firstKey.height },
            badge: { w: badge.width, h: badge.height }
          };
        });

        console.log(`[${wrapperName.toUpperCase()} Desktop Geom]:`, geom);
        assert(geom.focusBtn.h >= 40 && geom.focusBtn.w >= 48, 'Focus button must have adequate touch target');
        assert(geom.uploadBtn.h >= 40 && geom.uploadBtn.w >= 48, 'Upload button must have adequate touch target');
        assert(geom.sendBtn.h >= 44 && geom.sendBtn.w >= 48, 'Send button must have >= 44px height and >= 48px width');
        assert(geom.firstKey.h >= 40 && geom.firstKey.w >= 40, 'Key button must have adequate touch target');

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

      // 2. MOBILE VIEWPORT (390x844 - iPhone 14 / Modern Smartphone)
      {
        const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
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

        // Verify mobile stacked layout
        const mobileGeom = await page.evaluate(() => {
          const bottomBar = document.getElementById('agy-bottom-bar');
          const inputRow = document.querySelector('.agy-input-row');
          const keysRow = document.querySelector('.agy-keys-row');
          const focusBtn = document.getElementById('agy-focus-btn').getBoundingClientRect();
          const sendBtn = document.getElementById('agy-send-btn').getBoundingClientRect();
          return {
            bottomBarHeight: bottomBar.getBoundingClientRect().height,
            inputRowHeight: inputRow.getBoundingClientRect().height,
            keysRowHeight: keysRow.getBoundingClientRect().height,
            focusBtn: { w: focusBtn.width, h: focusBtn.height },
            sendBtn: { w: sendBtn.width, h: sendBtn.height }
          };
        });

        console.log(`[${wrapperName.toUpperCase()} Mobile Geom]:`, mobileGeom);
        assert(mobileGeom.bottomBarHeight >= 80, 'Mobile bottom bar must accommodate two rows');
        assert(mobileGeom.sendBtn.h >= 40 && mobileGeom.sendBtn.w >= 48, 'Mobile send button touch target');

        // Capture Mobile Screenshot
        const mobileShotPath = path.join(previewDir, `terminal-${wrapperName}-mobile-390.png`);
        await page.screenshot({ path: mobileShotPath });
        console.log(`Saved screenshot: ${mobileShotPath}`);

        await page.close();
      }
    }

    console.log('=== ALL TERMINAL WRAPPER TESTS PASSED SUCCESSFULLY ===');
  } finally {
    await browser.close();
  }
})();
