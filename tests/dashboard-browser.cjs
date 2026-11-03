const { chromium } = require('@playwright/test');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    headless: true
  });

  try {
    const context = await browser.newContext({ acceptDownloads: true });

    // 1. MAINTAINED LOGIN PAGE TEST
    {
      const page = await context.newPage();
      let loginPayload = null;

      await page.route('**/*', async route => {
        const req = route.request();
        const url = req.url();
        if (url === 'http://mobile.test/login') {
          return route.fulfill({
            body: fs.readFileSync('.work/login-test.html', 'utf8'),
            contentType: 'text/html'
          });
        }
        if (url.endsWith('/portal.css')) {
          return route.fulfill({ body: fs.readFileSync('web/portal.css', 'utf8'), contentType: 'text/css' });
        }
        if (url.endsWith('/static/laptop.js')) {
          return route.fulfill({ body: fs.readFileSync('web/laptop.js', 'utf8'), contentType: 'application/javascript' });
        }
        if (url.endsWith('/api/login') && req.method() === 'POST') {
          loginPayload = req.postDataJSON();
          return route.fulfill({
            json: { status: 'ok', user: 'testadmin', role: 'admin', jwt: 'mock-jwt-val', redirect: '/' }
          });
        }
        if (url.endsWith('/favicon.ico') || url.endsWith('.webp') || url.endsWith('.png')) {
          return route.fulfill({ body: '', contentType: 'image/png' });
        }
        await route.abort();
      });

      await page.goto('http://mobile.test/login');
      assert(await page.locator('#username').isVisible(), 'Username input should be visible');
      assert(await page.locator('#password').isVisible(), 'Password input should be visible');
      assert(await page.locator('#submit-btn').isVisible(), 'Submit button should be visible');

      await page.locator('#username').fill('testadmin');
      await page.locator('#password').fill('secret-pass');
      await page.locator('#submit-btn').click();

      await page.waitForResponse(resp => resp.url().endsWith('/api/login'));
      assert.deepEqual(loginPayload, { username: 'testadmin', password: 'secret-pass' });
      console.log('Login page PASS: loads from maintained template, submits credentials.');
      await page.close();
    }

    // 2. MAINTAINED ADMIN DASHBOARD (CONVERSION & NOTES CONTRACTS)
    {
      const page = await context.newPage();
      let capturedNotesPost = null;
      let capturedConvertReq = null;

      await page.route('**/*', async route => {
        const req = route.request();
        const url = req.url();
        if (url === 'http://mobile.test/' || url === 'http://mobile.test/admin') {
          return route.fulfill({
            body: fs.readFileSync('.work/dashboard-test.html', 'utf8'),
            contentType: 'text/html'
          });
        }
        if (url.endsWith('/portal.css')) {
          return route.fulfill({ body: fs.readFileSync('web/portal.css', 'utf8'), contentType: 'text/css' });
        }
        if (url.endsWith('/static/laptop.js')) {
          return route.fulfill({ body: fs.readFileSync('web/laptop.js', 'utf8'), contentType: 'application/javascript' });
        }
        if (url.endsWith('/api/stats')) {
          return route.fulfill({
            json: { tailscale_ip: '100.75.159.53', local_ip: '192.168.1.8', stale: false, battery: { percentage: 90, temperature: 30 }, ram: {}, storage: {}, network: {}, load_avg: [null] }
          });
        }
        if (url.endsWith('/api/vpn/status')) {
          return route.fulfill({ json: { enabled: true, verified_via_tor: false, stale: false, masked_ip: '203.0.113.99' } });
        }
        if (url.endsWith('/api/services')) {
          return route.fulfill({ json: { services: [], total_active_mem_mb: null, sleeping_count: 0 } });
        }
        if (url.endsWith('/api/agy/sessions')) {
          return route.fulfill({ json: { sessions: [] } });
        }
        if (url.endsWith('/api/notes')) {
          if (req.method() === 'GET') {
            return route.fulfill({ json: { notes: 'Existing admin notes on server' } });
          }
          if (req.method() === 'POST') {
            capturedNotesPost = req.postDataJSON();
            return route.fulfill({ json: { status: 'saved' } });
          }
        }
        if (url.includes('/api/convert/pdf') && req.method() === 'POST') {
          capturedConvertReq = {
            url: req.url(),
            headers: req.headers(),
            postData: req.postData()
          };
          const disposition = capturedConvertReq.postData && capturedConvertReq.postData.includes('second.docx')
            ? 'attachment; filename="converted_documents.zip"'
            : 'attachment; filename="admin_doc.pdf"';
          const ctype = disposition.includes('.zip') ? 'application/zip' : 'application/pdf';
          const payload = disposition.includes('.zip') ? Buffer.from('PK mock zip') : Buffer.from('%PDF-1.4 mock pdf');
          return route.fulfill({
            status: 200,
            headers: {
              'Content-Type': ctype,
              'Content-Disposition': disposition,
              'Content-Length': String(payload.length)
            },
            body: payload
          });
        }
        if (url.endsWith('/favicon.ico') || url.endsWith('.webp') || url.endsWith('.png')) {
          return route.fulfill({ body: '', contentType: 'image/png' });
        }
        await route.abort();
      });

      await page.goto('http://mobile.test/');

      // Check notes load
      await page.waitForFunction(() => document.getElementById('user-notes-input') && document.getElementById('user-notes-input').value === 'Existing admin notes on server');
      assert.equal(await page.locator('#user-notes-input').inputValue(), 'Existing admin notes on server');

      // Check notes edit and auto-save
      await page.locator('#user-notes-input').fill('Updated admin notes content');
      await page.waitForFunction(() => {
        const el = document.getElementById('notes-status');
        return el && el.textContent.includes('Saved');
      }, { timeout: 3000 });
      assert.deepEqual(capturedNotesPost, { notes: 'Updated admin notes content' });

      // Check conversion: open modal and single file
      await page.evaluate(() => openConverterModal());
      assert(await page.locator('#convert-btn').isDisabled(), 'Convert button must start disabled');
      await page.locator('#convert-file-input').setInputFiles([
        { name: 'admin_doc.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('docx content') }
      ]);
      await page.waitForFunction(() => !document.getElementById('convert-btn').disabled);
      assert((await page.locator('#convert-file-summary').textContent()).includes('1 file selected'));

      const [singleDownload] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('#convert-btn').click()
      ]);
      assert.equal(singleDownload.suggestedFilename(), 'admin_doc.pdf');
      assert(capturedConvertReq.url.endsWith('/api/convert/pdf'));
      assert(capturedConvertReq.headers['content-type'].includes('multipart/form-data'));
      assert(capturedConvertReq.postData.includes('name="files"'));
      assert(capturedConvertReq.postData.includes('admin_doc.docx'));

      // Check conversion: multi-file zip download
      await page.waitForTimeout(1600);
      await page.evaluate(() => openConverterModal());
      await page.locator('#convert-file-input').setInputFiles([
        { name: 'first.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('docx 1') },
        { name: 'second.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('docx 2') }
      ]);
      await page.waitForFunction(() => !document.getElementById('convert-btn').disabled);
      assert((await page.locator('#convert-file-summary').textContent()).includes('2 files selected'));

      const [multiDownload] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('#convert-btn').click()
      ]);
      assert.equal(multiDownload.suggestedFilename(), 'converted_documents.zip');

      console.log('Admin dashboard PASS: notes load/edit/save, single & batch conversion download contracts.');
      await page.close();
    }

    // 3. MAINTAINED FAMILY DASHBOARD (CONVERSION & NOTES CONTRACTS)
    {
      const page = await context.newPage();
      let capturedNotesPost = null;
      let capturedConvertReq = null;

      await page.route('**/*', async route => {
        const req = route.request();
        const url = req.url();
        if (url === 'http://mobile.test/' || url === 'http://mobile.test/family') {
          return route.fulfill({
            body: fs.readFileSync('.work/family-test.html', 'utf8'),
            contentType: 'text/html'
          });
        }
        if (url.endsWith('/portal.css')) {
          return route.fulfill({ body: fs.readFileSync('web/portal.css', 'utf8'), contentType: 'text/css' });
        }
        if (url.endsWith('/static/laptop.js')) {
          return route.fulfill({ body: fs.readFileSync('web/laptop.js', 'utf8'), contentType: 'application/javascript' });
        }
        if (url.endsWith('/api/stats')) {
          return route.fulfill({
            json: { tailscale_ip: '100.75.159.53', local_ip: '192.168.1.8', stale: false, battery: { percentage: 95, temperature: 28 }, ram: {}, storage: {}, network: {}, load_avg: [null] }
          });
        }
        if (url.endsWith('/api/notes')) {
          if (req.method() === 'GET') {
            return route.fulfill({ json: { notes: 'Family shared note' } });
          }
          if (req.method() === 'POST') {
            capturedNotesPost = req.postDataJSON();
            return route.fulfill({ json: { status: 'saved' } });
          }
        }
        if (url.includes('/api/convert/pdf') && req.method() === 'POST') {
          capturedConvertReq = {
            url: req.url(),
            headers: req.headers(),
            postData: req.postData()
          };
          const disposition = 'attachment; filename="family_homework.pdf"';
          const payload = Buffer.from('%PDF-1.4 family pdf output');
          return route.fulfill({
            status: 200,
            headers: {
              'Content-Type': 'application/pdf',
              'Content-Disposition': disposition,
              'Content-Length': String(payload.length)
            },
            body: payload
          });
        }
        if (url.endsWith('/favicon.ico') || url.endsWith('.webp') || url.endsWith('.png')) {
          return route.fulfill({ body: '', contentType: 'image/png' });
        }
        await route.abort();
      });

      await page.goto('http://mobile.test/');

      // Check notes load
      await page.waitForFunction(() => document.getElementById('user-notes-input') && document.getElementById('user-notes-input').value === 'Family shared note');
      assert.equal(await page.locator('#user-notes-input').inputValue(), 'Family shared note');

      // Check notes edit and auto-save
      await page.locator('#user-notes-input').fill('Updated grocery list for weekend');
      await page.waitForFunction(() => {
        const el = document.getElementById('notes-status');
        return el && el.textContent.includes('Saved');
      }, { timeout: 3000 });
      assert.deepEqual(capturedNotesPost, { notes: 'Updated grocery list for weekend' });

      // Check conversion
      await page.evaluate(() => openConverterModal());
      assert(await page.locator('#convert-btn').isDisabled(), 'Family convert button must start disabled');
      await page.locator('#convert-file-input').setInputFiles([
        { name: 'family_homework.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('homework') }
      ]);
      await page.waitForFunction(() => !document.getElementById('convert-btn').disabled);
      assert((await page.locator('#convert-file-summary').textContent()).includes('1 file selected'));

      const [familyDownload] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('#convert-btn').click()
      ]);
      assert.equal(familyDownload.suggestedFilename(), 'family_homework.pdf');
      assert(capturedConvertReq.url.endsWith('/api/convert/pdf'));
      assert(capturedConvertReq.headers['content-type'].includes('multipart/form-data'));
      assert(capturedConvertReq.postData.includes('name="files"'));
      assert(capturedConvertReq.postData.includes('family_homework.docx'));

      console.log('Family dashboard PASS: notes load/edit/save, conversion download contract.');
      await page.close();
    }

  } finally {
    await browser.close();
  }
})().catch(err => {
  console.error(err);
  process.exit(1);
});
