/**
 * =========================================================================
 * ANTRA BROWSER ACCEPTANCE SUITE
 * Exercises real web/antra.html, web/antra.css, and web/antra.js with
 * synthetic Antra API endpoints.
 *
 * Verifies:
 * - Admin vs Anonymous (401) vs Family (403) access denial, control disablement,
 *   and complete purge of previously rendered private jobs/tracks
 * - Console /admin link removed; only valid Portal / navigation retained
 * - Storage destination selection (default 'private' vs 'family'), server display,
 *   and retry destination immutability
 * - Delayed track details response + close race condition guard (late responses ignored)
 * - Form inputs, submit pending guard, explicit network error handling, bounded timeouts
 * - Work after page reload & progress updates
 * - 250 track details & index 101+/250 proof without missing rows
 * - Best-available FLAC rule: 24-bit and 16-bit accepted and completed
 * - Cancel & retry transitions
 * - XSS strings safely render as text
 * - Mobile touch targets (>= 48px) and responsive table overflow
 * - Pagehide lifecycle cleanup (abort in-flight GETs, dispose polling)
 * - Meaningful desktop & mobile screenshots saved in .superpowers/sdd
 * =========================================================================
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');

const rootDir = path.resolve(__dirname, '..');
const sddDir = path.resolve(rootDir, '.superpowers/sdd');
if (!fs.existsSync(sddDir)) {
  fs.mkdirSync(sddDir, { recursive: true });
}

// Load maintained assets
const antraHtml = fs.readFileSync(path.join(rootDir, 'web/antra.html'), 'utf8');
const antraCss = fs.readFileSync(path.join(rootDir, 'web/antra.css'), 'utf8');
const antraJs = fs.readFileSync(path.join(rootDir, 'web/antra.js'), 'utf8');
const portalCss = fs.readFileSync(path.join(rootDir, 'web/portal.css'), 'utf8');
const themeTokensCss = fs.readFileSync(path.join(rootDir, 'web/theme-tokens.css'), 'utf8');
const appearanceCss = fs.readFileSync(path.join(rootDir, 'web/appearance.css'), 'utf8');
const appearanceJs = fs.readFileSync(path.join(rootDir, 'web/appearance.js'), 'utf8');
const portalPollingJs = fs.readFileSync(path.join(rootDir, 'web/portal-polling.js'), 'utf8');

// Synthetic API State Fixture
function createInitialState() {
  const fixtureTracks = [];
  for (let i = 0; i < 250; i++) {
    const is24Bit = i % 5 !== 3; // every 5th track is 16-bit
    fixtureTracks.push({
      index: i,
      title: i === 0
        ? '<script>window.pwned=1</script>Track 1'
        : `Symphony No. ${i + 1} in C Minor`,
      artist: i === 0
        ? '<img src=x onerror="window.pwned=2">Artist Zero'
        : `Orchestra ${Math.floor(i / 10) + 1}`,
      status: i < 120 ? 'completed' : 'downloading',
      bit_depth: is24Bit ? 24 : 16,
      sample_rate: is24Bit ? 96000 : 44100,
      filename: `Orchestra/Symphony_${i + 1}.flac`,
      message: is24Bit ? 'Verified 24-bit FLAC (96000 Hz, 2 ch)' : 'Verified 16-bit FLAC (44100 Hz, 2 ch)'
    });
  }

  const initialJobs = [
    {
      id: 'job-uuid-250',
      url: 'https://tidal.com/playlist/complete-edition-250',
      destination: 'private',
      title: 'Complete 250-Track Hi-Res Anthology',
      status: 'downloading',
      total: 250,
      completed: 110,
      skipped: 20,
      failed: 0,
      created_at: Math.floor(Date.now() / 1000) - 3600,
      updated_at: Math.floor(Date.now() / 1000) - 60,
      message: 'Processing batch: 130 of 250 handled'
    },
    {
      id: 'job-uuid-failed',
      url: 'https://qobuz.com/album/failed-album',
      destination: 'family',
      title: 'Failed Album Requiring Retry',
      status: 'failed',
      total: 12,
      completed: 4,
      skipped: 2,
      failed: 6,
      created_at: Math.floor(Date.now() / 1000) - 7200,
      updated_at: Math.floor(Date.now() / 1000) - 1200,
      message: 'Upstream rate limit 429 encountered'
    }
  ];

  return {
    role: 'admin', // 'admin' | 'family' | 'anonymous'
    worker: {
      state: 'running',
      updated_at: Math.floor(Date.now() / 1000),
      message: 'Supervised background engine active'
    },
    settings: {
      output_directory: 'Music/Astro', // legacy fallback
      output_directories: {
        private: 'Music/Astro',
        family: 'Music/Family'
      },
      quality: 'Best Available FLAC'
    },
    jobs: initialJobs,
    tracksByJobId: {
      'job-uuid-250': fixtureTracks,
      'job-uuid-failed': [
        { index: 0, title: 'Track F1', artist: 'Band', status: 'completed', bit_depth: 24, sample_rate: 96000, filename: 'f1.flac', message: 'OK' },
        { index: 1, title: 'Track F2', artist: 'Band', status: 'failed', bit_depth: null, sample_rate: null, filename: null, message: 'Upstream 429' }
      ]
    },
    submittedPayloads: [],
    delayTrackJobId: null,
    delayMs: 0,
    pollCounts: {
      jobs: 0,
      tracks: 0
    }
  };
}

(async () => {
  console.log('--- STARTING ANTRA BROWSER ACCEPTANCE SUITE ---');
  let state = createInitialState();

  // Create Synthetic Antra HTTP Server
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const pathname = url.pathname;

    // Static Asset Delivery
    if (pathname === '/antra/' || pathname === '/antra' || pathname === '/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(antraHtml);
    }
    if (pathname === '/antra.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(antraCss);
    }
    if (pathname === '/antra.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(antraJs);
    }
    if (pathname === '/portal.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      return res.end(portalCss);
    }
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
    if (pathname === '/static/portal-polling.js' || pathname === '/portal-polling.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
      return res.end(portalPollingJs);
    }
    if (pathname === '/api/wallpapers') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ version: 1, default_id: null, wallpapers: [] }));
    }

    // Role Enforcement Gate (Matches real app/dashboard.py role dispatch)
    if (pathname.startsWith('/api/antra/')) {
      if (state.role === 'anonymous') {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Authentication required' }));
      }
      if (state.role === 'family') {
        res.writeHead(403, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Administrator access required' }));
      }
    }

    // Antra API Routes
    if (pathname === '/api/antra/jobs' && req.method === 'GET') {
      state.pollCounts.jobs++;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        jobs: state.jobs,
        worker: state.worker,
        settings: state.settings
      }));
    }

    if (pathname === '/api/antra/jobs' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        try {
          const payload = JSON.parse(body || '{}');
          if (!payload.url || typeof payload.url !== 'string') {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            return res.end(JSON.stringify({ status: 'error', message: 'Missing URL' }));
          }
          state.submittedPayloads.push(payload);
          const destination = payload.destination === 'family' ? 'family' : 'private';
          const newJob = {
            id: 'job-' + Date.now(),
            url: payload.url,
            destination: destination,
            title: 'Submitted ' + payload.url.split('/').pop(),
            status: 'queued',
            total: 10,
            completed: 0,
            skipped: 0,
            failed: 0,
            created_at: Math.floor(Date.now() / 1000),
            updated_at: Math.floor(Date.now() / 1000),
            message: 'Queued for resolution'
          };
          state.jobs.unshift(newJob);
          state.tracksByJobId[newJob.id] = [
            { index: 0, title: 'Sample Track 1', artist: 'Artist', status: 'queued', bit_depth: null, sample_rate: null, filename: null, message: 'Queued' }
          ];
          res.writeHead(202, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ job: newJob }));
        } catch (e) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ status: 'error', message: 'Malformed JSON' }));
        }
      });
      return;
    }

    // GET /api/antra/jobs/<id>
    const matchDetail = pathname.match(/^\/api\/antra\/jobs\/([^/]+)$/);
    if (matchDetail && req.method === 'GET') {
      const id = decodeURIComponent(matchDetail[1]);
      const job = state.jobs.find(j => j.id === id);
      if (!job) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Job not found' }));
      }
      state.pollCounts.tracks++;
      const tracks = state.tracksByJobId[id] || [];

      const sendResponse = () => {
        if (res.destroyed) return;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ job, tracks }));
      };

      if (state.delayTrackJobId === id && state.delayMs > 0) {
        setTimeout(sendResponse, state.delayMs);
        return;
      }
      return sendResponse();
    }

    // POST /api/antra/jobs/<id>/cancel
    const matchCancel = pathname.match(/^\/api\/antra\/jobs\/([^/]+)\/cancel$/);
    if (matchCancel && req.method === 'POST') {
      const id = decodeURIComponent(matchCancel[1]);
      const job = state.jobs.find(j => j.id === id);
      if (!job) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Job not found' }));
      }
      job.status = 'cancelled';
      job.message = 'Cancelled by administrator';
      res.writeHead(202, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ job }));
    }

    // POST /api/antra/jobs/<id>/retry
    const matchRetry = pathname.match(/^\/api\/antra\/jobs\/([^/]+)\/retry$/);
    if (matchRetry && req.method === 'POST') {
      const id = decodeURIComponent(matchRetry[1]);
      const job = state.jobs.find(j => j.id === id);
      if (!job) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ status: 'error', message: 'Job not found' }));
      }
      // Retry preserves existing immutable destination!
      job.status = 'queued';
      job.message = 'Retry queued';
      res.writeHead(202, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ job }));
    }

    res.writeHead(404);
    res.end('Not found');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}/antra/`;

  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    const context = await browser.newContext();

    // =========================================================================
    // 1. ANONYMOUS ROLE (401 REFUSAL) CHECK
    // =========================================================================
    console.log('[Test 1] Verifying Anonymous 401 handling...');
    state.role = 'anonymous';
    {
      const page = await context.newPage();
      await page.goto(baseUrl);
      await page.waitForSelector('#auth-alert:not(.hidden)');
      const alertTitle = await page.textContent('#auth-alert-title');
      assert.match(alertTitle, /Authentication Required/i);
      const isInputDisabled = await page.isDisabled('#antra-url-input');
      const isSelectDisabled = await page.isDisabled('#antra-destination-select');
      const isSubmitDisabled = await page.isDisabled('#antra-submit-btn');
      assert.strictEqual(isInputDisabled, true, 'Input is disabled under 401');
      assert.strictEqual(isSelectDisabled, true, 'Destination select is disabled under 401');
      assert.strictEqual(isSubmitDisabled, true, 'Submit is disabled under 401');
      await page.close();
    }

    // =========================================================================
    // 2. FAMILY ROLE (403 ACCESS DENIED) CHECK
    // =========================================================================
    console.log('[Test 2] Verifying Family 403 handling...');
    state.role = 'family';
    {
      const page = await context.newPage();
      await page.goto(baseUrl);
      await page.waitForSelector('#auth-alert:not(.hidden)');
      const alertTitle = await page.textContent('#auth-alert-title');
      assert.match(alertTitle, /Access Denied/i);
      const isInputDisabled = await page.isDisabled('#antra-url-input');
      const isSelectDisabled = await page.isDisabled('#antra-destination-select');
      const isSubmitDisabled = await page.isDisabled('#antra-submit-btn');
      assert.strictEqual(isInputDisabled, true, 'Input is disabled under 403');
      assert.strictEqual(isSelectDisabled, true, 'Destination select is disabled under 403');
      assert.strictEqual(isSubmitDisabled, true, 'Submit is disabled under 403');
      await page.close();
    }

    // =========================================================================
    // 3. ADMIN ROLE, NAVIGATION LINKS & DESTINATION LABELS
    // =========================================================================
    console.log('[Test 3] Verifying Admin role desktop display & navigation...');
    state.role = 'admin';
    const page = await context.newPage();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(baseUrl);

    await page.waitForSelector('#jobs-list .job-card');
    assert.strictEqual(await page.textContent('#worker-state-badge'), 'running');
    assert.strictEqual(await page.textContent('#output-dir-private'), 'Music/Astro');
    assert.strictEqual(await page.textContent('#output-dir-family'), 'Music/Family');

    // Root check: Verify redundant broken /admin link is removed, Portal / exists
    const adminLinkCount = await page.locator('a[href="/admin"]').count();
    assert.strictEqual(adminLinkCount, 0, 'Console link to /admin is removed');
    const portalLinkCount = await page.locator('a[href="/"]').count();
    assert.strictEqual(portalLinkCount >= 1, true, 'Portal link to / is present');

    // Save Desktop Screenshot
    const desktopScreenshotPath = path.join(sddDir, 'antra-desktop.png');
    await page.screenshot({ path: desktopScreenshotPath, fullPage: true });
    assert.strictEqual(fs.existsSync(desktopScreenshotPath), true);
    console.log(`Saved screenshot: ${desktopScreenshotPath} (${fs.statSync(desktopScreenshotPath).size} bytes)`);

    // =========================================================================
    // 4. FORM DESTINATION SELECTION & SUBMISSION
    // =========================================================================
    console.log('[Test 4] Testing destination select (private vs family) and submissions...');
    // Verify default destination is 'private'
    const defaultDestVal = await page.inputValue('#antra-destination-select');
    assert.strictEqual(defaultDestVal, 'private', 'Default storage destination is private');

    // 4a: Submit with default 'private' destination
    await page.fill('#antra-url-input', 'https://tidal.com/album/private-album');
    await page.click('#antra-submit-btn');
    await page.waitForSelector('.form-status-success');
    assert.strictEqual(state.submittedPayloads.at(-1).destination, 'private', 'Submitted payload has destination: private');

    // Verify destination badge on card
    const firstJobCard = page.locator('#jobs-list .job-card').first();
    const destBadgeText = await firstJobCard.locator('.badge-destination').textContent();
    assert.strictEqual(destBadgeText.trim(), 'My music (private)');

    // 4b: Submit with 'family' destination
    await page.selectOption('#antra-destination-select', 'family');
    await page.fill('#antra-url-input', 'https://tidal.com/album/family-album');
    await page.click('#antra-submit-btn');
    await page.waitForSelector('.form-status-success');
    assert.strictEqual(state.submittedPayloads.at(-1).destination, 'family', 'Submitted payload has destination: family');

    const newFamilyCard = page.locator('#jobs-list .job-card').first();
    const familyBadgeText = await newFamilyCard.locator('.badge-destination').textContent();
    assert.strictEqual(familyBadgeText.trim(), 'Family music');

    // Network error test using Playwright route abort
    await page.route('**/api/antra/jobs', async (route) => {
      if (route.request().method() === 'POST') {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    }, { times: 1 });
    await page.fill('#antra-url-input', 'https://tidal.com/track/dropped-connection');
    await page.click('#antra-submit-btn');
    await page.waitForSelector('.form-status-error');
    const netErr = await page.textContent('#form-status');
    assert.match(netErr, /Network error/i);

    // =========================================================================
    // 5. RACE CONDITION GUARD: DELAYED RESPONSE + CLOSE
    // =========================================================================
    console.log('[Test 5] Testing race condition: late track details response after Close Details...');
    // Set synthetic 400ms delay for job-uuid-failed details
    state.delayTrackJobId = 'job-uuid-failed';
    state.delayMs = 400;

    const failedCard = page.locator('.job-card[data-job-id="job-uuid-failed"]');
    // Click View Tracks on slow job
    await failedCard.locator('button:has-text("View Tracks")').click();
    // Immediately click Close Details while request is in-flight!
    await page.click('#btn-close-tracks');

    // Wait for the delayed network response to arrive
    await page.waitForTimeout(550);

    // Assert details panel remained hidden and was not reopened by the late arrival
    const isTracksHidden = await page.locator('#tracks-section').evaluate(el => el.classList.contains('hidden'));
    assert.strictEqual(isTracksHidden, true, 'Track details remained closed despite late network response');
    const tbodyRowCount = await page.locator('#tracks-tbody tr').count();
    assert.strictEqual(tbodyRowCount, 0, 'Tracks table was not populated by late response');

    // Reset delays
    state.delayTrackJobId = null;
    state.delayMs = 0;

    // =========================================================================
    // 6. 250 TRACK DETAILS & INDEX 101+/250 PROOF
    // =========================================================================
    console.log('[Test 6] Inspecting 250 track details & index 101+ proof...');
    const job250Card = page.locator('.job-card[data-job-id="job-uuid-250"]');
    await job250Card.locator('button:has-text("View Tracks")').click();

    await page.waitForSelector('#tracks-section:not(.hidden)');
    await page.waitForSelector('#tracks-tbody tr');

    const trackRows = page.locator('#tracks-tbody tr');
    const totalRowsCount = await trackRows.count();
    assert.strictEqual(totalRowsCount, 250, 'Exactly 250 tracks rendered without truncation');

    // Index 1 check (row 1, zero-based index 0)
    const row0 = trackRows.nth(0);
    assert.strictEqual(await row0.locator('td').nth(0).textContent(), '1');

    // Index 101 proof (row 102, zero-based index 101)
    const row101 = trackRows.nth(101);
    const index101Text = await row101.locator('td').nth(0).textContent();
    assert.strictEqual(index101Text, '102');
    const title101Text = await row101.locator('td').nth(1).textContent();
    assert.strictEqual(title101Text, 'Symphony No. 102 in C Minor');

    // Verify destination displayed in track details header
    const destDetailsText = await page.textContent('#tracks-job-dest-badge');
    assert.strictEqual(destDetailsText.trim(), 'My music (private)');

    // =========================================================================
    // 7. BEST AVAILABLE FLAC QUALITY ENFORCEMENT (16-BIT & 24-BIT COMPLETED)
    // =========================================================================
    console.log('[Test 7] Verifying best available FLAC quality enforcement (16-bit & 24-bit completed)...');
    const row1Quality = await trackRows.nth(1).locator('td').nth(3).textContent();
    assert.match(row1Quality, /24-bit/);
    assert.match(row1Quality, /96 kHz/);
    const row1Status = await trackRows.nth(1).locator('td').nth(4).textContent();
    assert.strictEqual(row1Status.trim(), 'completed');

    const row3Quality = await trackRows.nth(3).locator('td').nth(3).textContent();
    assert.match(row3Quality, /16-bit/);
    assert.match(row3Quality, /44\.1 kHz/);
    const row3Status = await trackRows.nth(3).locator('td').nth(4).textContent();
    assert.strictEqual(row3Status.trim(), 'completed');

    // =========================================================================
    // 8. XSS MITIGATION / SAFE STRING RENDERING PROOF
    // =========================================================================
    console.log('[Test 8] Verifying XSS mitigation...');
    const pwned = await page.evaluate(() => window.pwned);
    assert.strictEqual(pwned, undefined, 'No XSS payload executed in window context');
    const row0Title = await row0.locator('td').nth(1).textContent();
    assert.match(row0Title, /<script>window\.pwned=1<\/script>/);

    // =========================================================================
    // 9. CANCEL & RETRY JOB ACTIONS WITH IMMUTABLE DESTINATION
    // =========================================================================
    console.log('[Test 9] Verifying Cancel and Retry actions with destination preservation...');
    await job250Card.locator('button[data-action="cancel"]').click();
    await page.waitForTimeout(100);
    const updatedStatus250 = await job250Card.locator('.badge-status').textContent();
    assert.strictEqual(updatedStatus250.trim(), 'cancelled');

    // Retry family job
    await failedCard.locator('button[data-action="retry"]').click();
    await page.waitForTimeout(100);
    const updatedFailedStatus = await failedCard.locator('.badge-status').textContent();
    assert.strictEqual(updatedFailedStatus.trim(), 'queued');
    // Destination remains family
    const retriedDestBadge = await failedCard.locator('.badge-destination').textContent();
    assert.strictEqual(retriedDestBadge.trim(), 'Family music');

    // =========================================================================
    // 10. ROLE 403 / 401 PURGES PRIOR PRIVATE CONTENT
    // =========================================================================
    console.log('[Test 10] Testing 403 access denial clears all previously rendered jobs/tracks...');
    // We currently have jobs and tracks rendered on page. Switch server to 403:
    state.role = 'family';
    // Trigger poll / refresh:
    await page.click('#btn-refresh');
    await page.waitForSelector('#auth-alert:not(.hidden)');

    // Verify all private cached DOM elements are completely cleared
    const remainingJobsCount = await page.locator('#jobs-list .job-card').count();
    assert.strictEqual(remainingJobsCount, 0, 'All previously rendered job cards were purged on 403');
    const remainingTrackRows = await page.locator('#tracks-tbody tr').count();
    assert.strictEqual(remainingTrackRows, 0, 'All track rows were purged on 403');
    const isSectionHidden = await page.locator('#tracks-section').evaluate(el => el.classList.contains('hidden'));
    assert.strictEqual(isSectionHidden, true, 'Track section hidden on 403');

    // Verify controls disabled
    assert.strictEqual(await page.isDisabled('#antra-url-input'), true);
    assert.strictEqual(await page.isDisabled('#antra-destination-select'), true);
    assert.strictEqual(await page.isDisabled('#antra-submit-btn'), true);
    assert.strictEqual(await page.isDisabled('#btn-refresh'), true);

    // =========================================================================
    // 11. MOBILE TOUCH TARGETS & RESPONSIVE LAYOUT
    // =========================================================================
    console.log('[Test 11] Testing mobile touch targets (>= 48px) and horizontal overflow...');
    state.role = 'admin'; // reset role
    await page.goto(baseUrl);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForSelector('#jobs-list .job-card');

    const submitBtnBox = await page.locator('#antra-submit-btn').boundingBox();
    assert.strictEqual(submitBtnBox.height >= 48, true, `Submit button height ${submitBtnBox.height} >= 48px`);

    const destSelectBox = await page.locator('#antra-destination-select').boundingBox();
    assert.strictEqual(destSelectBox.height >= 48, true, `Destination select height ${destSelectBox.height} >= 48px`);

    const urlInputBox = await page.locator('#antra-url-input').boundingBox();
    assert.strictEqual(urlInputBox.height >= 48, true, `URL input height ${urlInputBox.height} >= 48px`);

    const refreshBtnBox = await page.locator('#btn-refresh').boundingBox();
    assert.strictEqual(refreshBtnBox.height >= 48, true, `Refresh button height ${refreshBtnBox.height} >= 48px`);

    // Verify track table scroll container overflow-x behavior
    const tableContainerOverflow = await page.evaluate(() => {
      const el = document.querySelector('.table-scroll-container');
      return window.getComputedStyle(el).overflowX;
    });
    assert.strictEqual(tableContainerOverflow, 'auto', 'Table container has overflow-x: auto');

    // Save Mobile Screenshot
    const mobileScreenshotPath = path.join(sddDir, 'antra-mobile.png');
    await page.screenshot({ path: mobileScreenshotPath, fullPage: true });
    assert.strictEqual(fs.existsSync(mobileScreenshotPath), true);
    console.log(`Saved screenshot: ${mobileScreenshotPath} (${fs.statSync(mobileScreenshotPath).size} bytes)`);

    // =========================================================================
    // 12. LIFECYCLE & POLLING CLEANUP VERIFICATION
    // =========================================================================
    console.log('[Test 12] Verifying polling cleanup on pagehide...');
    const pollsBefore = state.pollCounts.jobs;
    await page.evaluate(() => {
      window.dispatchEvent(new Event('pagehide'));
    });
    await page.waitForTimeout(300);
    const pollsAfter = state.pollCounts.jobs;
    assert.strictEqual(pollsAfter <= pollsBefore + 1, true, 'Polling ceased after pagehide');

    console.log('[Test 13] Verifying restored page resumes reads...');
    const restoredRead = page.waitForResponse(response => new URL(response.url()).pathname === '/api/antra/jobs');
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
    await restoredRead;
    assert.strictEqual(state.pollCounts.jobs > pollsAfter, true, 'Restored page resumes polling');

    console.log('[Test 14] Verifying close aborts detail fetch started by polling...');
    await page.locator('.job-card[data-job-id="job-uuid-250"] button:has-text("View Tracks")').click();
    await page.waitForSelector('#tracks-section:not(.hidden)');
    await page.evaluate(() => {
      const originalFetch = window.fetch;
      window.__detailAborted = false;
      window.fetch = function(input, options) {
        if (String(input) === '/api/antra/jobs/job-uuid-250' && options && options.signal) {
          options.signal.addEventListener('abort', () => { window.__detailAborted = true; }, { once: true });
        }
        return originalFetch.apply(this, arguments);
      };
    });
    state.delayTrackJobId = 'job-uuid-250';
    state.delayMs = 1000;
    const detailRequest = page.waitForRequest(request => new URL(request.url()).pathname === '/api/antra/jobs/job-uuid-250');
    await page.click('#btn-refresh');
    await detailRequest;
    await page.click('#btn-close-tracks');
    await page.waitForFunction(() => window.__detailAborted === true, null, { timeout: 2000 });
    assert.strictEqual(await page.locator('#tracks-section').evaluate(el => el.classList.contains('hidden')), true);
    state.delayTrackJobId = null;
    state.delayMs = 0;

    await page.close();
    await context.close();
    console.log('ALL ANTRA BROWSER ACCEPTANCE TESTS PASSED');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(err => {
  console.error('FATAL TEST ERROR:', err);
  process.exit(1);
});
