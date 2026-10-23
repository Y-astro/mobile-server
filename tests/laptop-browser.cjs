const {chromium}=require('@playwright/test');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{}),headless:true});
 try{
  const context=await browser.newContext();
  const page=await context.newPage();
  const ident='b'.repeat(32);
  let online=true,failCreate=false,sessions=[],deleted=[];
  await context.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.pathname==='/static/laptop.js')return route.fulfill({body:fs.readFileSync('web/laptop.js','utf8'),contentType:'application/javascript'});
   if(url.pathname==='/api/laptop/terminals'){
    if(request.method()==='POST'){
     if(failCreate)return route.fulfill({status:503,json:{message:'Laptop went offline'}});
     sessions=[{id:ident}];return route.fulfill({status:201,json:{id:ident}});
    }
    return route.fulfill({json:{online,sessions,error:online?null:'Laptop offline',max_sessions:4}});
   }
   if(url.pathname==='/api/laptop/terminals/'+ident && request.method()==='DELETE'){
    deleted.push(ident);sessions=[];return route.fulfill({json:{closed:true}});
   }
   if(url.pathname==='/laptop/')return route.fulfill({body:'<html>Terminal fixture</html>',contentType:'text/html'});
   if(url.origin==='http://mobile.test' && url.pathname==='/')return route.fulfill({body:fs.readFileSync('.work/dashboard-test.html','utf8'),contentType:'text/html'});
   await route.abort();
  });
  await page.goto('http://mobile.test/');
  await page.waitForFunction(()=>!document.querySelector('#laptop-new').disabled);
  const popupPromise=page.waitForEvent('popup');await page.locator('#laptop-new').click();const popup=await popupPromise;
  await popup.waitForURL('http://mobile.test/laptop/?arg='+ident);
  await page.waitForFunction(()=>document.querySelector('#laptop-sessions a'));
  assert.equal(await page.locator('#laptop-sessions a').getAttribute('href'),'/laptop/?arg='+ident);
  await popup.close();
  page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'Close terminal '+ident.slice(0,8)}).click();
  await page.waitForFunction(()=>document.querySelector('#laptop-sessions').textContent.includes('No laptop terminals'));
  assert.deepEqual(deleted,[ident]);
  online=false;await page.reload();await page.waitForFunction(()=>document.querySelector('#laptop-status').textContent==='Laptop offline');
  assert(await page.locator('#laptop-new').isDisabled());
  online=true;await page.reload();await page.waitForFunction(()=>!document.querySelector('#laptop-new').disabled);
  failCreate=true;await page.locator('#laptop-new').click();await page.waitForFunction(()=>document.querySelector('#laptop-status').textContent==='Laptop went offline');
  assert.equal(await page.locator('#laptop-new').isDisabled(),false);
  assert.equal(page.context().pages().length,1,'Failed creation must close its blank tab');
  failCreate=false;await page.evaluate(()=>window.open=()=>null);await page.locator('#laptop-new').click();
  await page.waitForFunction(()=>document.querySelector('#laptop-sessions a'));
  console.log('Laptop controls PASS: create/reconnect/close, offline/recovery, failed creation, blocked-popup fallback.');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
