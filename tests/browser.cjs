const { chromium } = require('@playwright/test');
const fs = require('fs');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {}), headless:true});
 try {
  const page = await browser.newPage();
  const calls = [];
  await page.route('http://mobile.test/**', async route => {
   if (route.request().url().endsWith('/api/agy/scroll')) { calls.push(route.request().postDataJSON()); await route.fulfill({json:{status:'ok'}}); }
   else if (route.request().url().endsWith('/api/clipboard/upload')) await route.fulfill({json:{status:'ok',path:'/root/screenshots/test.png',url:'/screenshots/test.png'}});
   else await route.fulfill({body:'<html></html>',contentType:'text/html'});
  });
  await page.goto('http://mobile.test/agy/');
  let html=fs.readFileSync('web/agy_index.html','utf8');
  // Omit the bundled ttyd client; exercise the actual custom controls with an
  // instrumented terminal so accidental arrow-key input is observable.
  html=html.replace(/<script type="text\/javascript">[\s\S]*?<\/script>/,'');
  await page.evaluate(()=> {window.rawKeys=[];window.term={rows:24,input:x=>window.rawKeys.push(x),focus(){}};});
  await page.setContent(html);
  await page.evaluate(()=> {
   const terminal=document.createElement('div');terminal.className='xterm';terminal.style='width:400px;height:300px';document.body.append(terminal);
   terminal.dispatchEvent(new WheelEvent('wheel',{deltaY:-120,bubbles:true,cancelable:true}));
  });
  await page.waitForTimeout(200);
  assert.equal(calls[0].action,'up');
  assert.deepEqual(await page.evaluate(()=>window.rawKeys),[]);
  await page.locator('[data-key="pgdown"]').first().click();
  await page.waitForTimeout(100);assert.equal(calls.at(-1).action,'down');
  await page.locator('#agy-prompt-input').fill('new prompt');
  await page.locator('#agy-send-btn').click();await page.waitForTimeout(100);
  assert.equal(calls.at(-1).action,'bottom');
  assert.deepEqual(await page.evaluate(()=>window.rawKeys),['new prompt\r']);
  await page.locator('#agy-file-input').setInputFiles({name:'<img src=x onerror="window.pwned=1">.png',mimeType:'image/png',buffer:Buffer.from('test')});
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>window.pwned),undefined);
  assert.equal(await page.locator('.agy-toast img[src="x"]').count(),0);
  assert.equal(await page.locator('#agy-prompt-input').inputValue(),'/root/screenshots/test.png');
  assert.deepEqual(await page.evaluate(()=>window.rawKeys),['new prompt\r']);
  await page.route('http://mobile.test/api/agy/scroll',async route=> {
    calls.push(route.request().postDataJSON());await new Promise(r=>setTimeout(r,100));await route.fulfill({json:{status:'ok'}});
  });
  await page.evaluate(()=>{
    document.querySelector('.xterm').dispatchEvent(new WheelEvent('wheel',{deltaY:-120,bubbles:true,cancelable:true}));
    document.getElementById('agy-prompt-input').value='after scrolling';document.getElementById('agy-send-btn').click();
  });
  await page.waitForTimeout(500);
  assert.equal(calls.at(-1).action,'bottom');
  assert.deepEqual(await page.evaluate(()=>window.rawKeys),['new prompt\r','after scrolling\r']);
  console.log('AGY browser controls: wheel/page controls scroll history without prompt keys; send returns live first.');
  const dashboard = await browser.newPage();
  await dashboard.route('**/*',async route=> {
    const url=route.request().url();
    if(url.endsWith('/api/stats')) await route.fulfill({json:{tailscale_ip:'100.75.159.53',stale:false,battery:{percentage:0,temperature:0},ram:{},storage:{},network:{},load_avg:[null]}});
    else if(url.endsWith('/api/vpn/status')) await route.fulfill({json:{enabled:true,verified_via_tor:false,stale:false,masked_ip:'203.0.113.99'}});
    else if(url.endsWith('/api/services')) await route.fulfill({json:{services:[],total_active_mem_mb:null,sleeping_count:0}});
    else if(url==='http://mobile.test/') await route.fulfill({body:fs.readFileSync('.work/dashboard-test.html','utf8'),contentType:'text/html'});
    else await route.abort();
  });
  await dashboard.goto('http://mobile.test/');await dashboard.waitForTimeout(200);
  assert.equal(await dashboard.locator('#battery-pct').textContent(),'0%');
  assert.equal(await dashboard.locator('#net-wifi-speed').textContent(),'N/A');
  assert.equal(await dashboard.locator('#cpu-temp').textContent(),'N/A');
  assert.equal(await dashboard.locator('#dest-exit-ip').textContent(),'Proxy exit: unavailable');
  await dashboard.evaluate(()=>addConvertFiles([new File(['test'],'<img src=x onerror="window.pwned=1">.docx')]));
  assert.equal(await dashboard.evaluate(()=>window.pwned),undefined);
  assert.equal(await dashboard.locator('#convert-file-list img').count(),0);
  console.log('Dashboard browser checks: zero readings retained, missing values N/A, Tor unverified exit hidden, filenames rendered as text.');
 } finally { await browser.close(); }
})();
