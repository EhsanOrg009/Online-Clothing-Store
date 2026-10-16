/* Requires optional Playwright and isolated fixtures. Never points at the production DB. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.SHOP_QA_URL || 'http://127.0.0.1:8765';
const auth = JSON.parse(fs.readFileSync(process.env.SHOP_QA_AUTH_FILE || '/tmp/shop-qa-auth.json', 'utf8'));
const out = process.env.SHOP_QA_OUTPUT || '/tmp/shop-browser-results';
fs.mkdirSync(out, {recursive:true});
const widths = process.env.SHOP_QA_SKIP_LAYOUT === '1' ? [] : [360,640,768,1024,1280,1536];
const results = [], issues = [];
let browser;
(async () => {
  browser = await chromium.launch({executablePath:process.env.SHOP_QA_CHROME || '/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
  for (const [role, routes] of Object.entries({
    guest:['/','/store/','/categories/','/store/category/qa-clothing/','/search/?q=لباس','/search/?q=absent','/product/qa-product-0/','/accounts/login/','/cart/','/admin/login/','/__qa__/400/','/__qa__/403/','/__qa__/404/','/__qa__/500/'],
    guest_verify:['/accounts/verify/'],
    customer_complete:['/accounts/complete-profile/'],
    customer:['/accounts/profile/','/accounts/profile/edit/','/accounts/addresses/','/accounts/addresses/new/','/cart/','/checkout/','/checkout/review/','/orders/',`/orders/${auth.order_number}/`,...auth.customer_routes,...auth.payment_routes],
    admin:auth.admin_routes
  })) {
    const context = await browser.newContext();
    if (auth[role]) await context.addCookies([{name:'sessionid',value:auth[role],url:base}]);
    const page = await context.newPage();
    let expectedNetworkFailure = false;
    page.on('pageerror', error => issues.push({role,type:'pageerror',url:page.url(),message:error.message}));
    page.on('console', message => { if (message.type()==='error' && !page.url().includes('/__qa__/') && !expectedNetworkFailure) issues.push({role,type:'console',url:page.url(),message:message.text()}); });
    page.on('requestfailed', request => { if(!expectedNetworkFailure) issues.push({role,type:'requestfailed',url:request.url(),message:request.failure()?.errorText}); });
    page.on('response', response => { if(response.status()>=400 && ['script','stylesheet','image','font'].includes(response.request().resourceType())) issues.push({role,type:'asset',url:response.url(),status:response.status()}); });
    for (const width of widths) {
      await page.setViewportSize({width,height:900});
      for (const route of routes) {
        const response = await page.goto(base+route,{waitUntil:'networkidle'});
        const layout = await page.evaluate(() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,direction:getComputedStyle(document.documentElement).direction,overflow:[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right>innerWidth+2 || e.getBoundingClientRect().left<-2).slice(0,8).map(e=>({tag:e.tagName,id:e.id,class:e.className}))}));
        results.push({role,route,width,status:response.status(),...layout});
        if (layout.scrollWidth>width+2) issues.push({role,route,width,type:'overflow',...layout});
        const expected = route.startsWith('/__qa__/') ? Number(route.split('/')[2]) : 200;
        if (response.status()!==expected) issues.push({role,route,width,type:'http',status:response.status(),expected});
        if ((width===360||width===1536) && ['/','/product/qa-product-0/','/accounts/login/','/accounts/verify/','/accounts/complete-profile/','/checkout/review/','/admin/','/admin/products/product/','/admin/products/product/add/','/admin/auth/group/add/','/__qa__/404/'].includes(route)) {
          await page.evaluate(async () => { for(let y=0;y<document.body.scrollHeight;y+=600){scrollTo(0,y);await new Promise(r=>setTimeout(r,40));}scrollTo(0,0); });
          await page.waitForTimeout(200);
          await page.screenshot({path:path.join(out,`${role}-${route.replaceAll('/','_')||'home'}-${width}.png`),fullPage:true});
        }
      }
    }
    if (role==='guest') {
      await page.setViewportSize({width:390,height:900});
      await page.goto(base+'/');
      await page.locator('[data-rail-step="1"]').click();await page.waitForTimeout(400);assert((await page.locator('#new-products').evaluate(rail=>Math.abs(rail.scrollLeft)))>0);
      await page.goto(base+'/');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'#main');
      await page.locator('.mobile-menu summary').focus();await page.keyboard.press('Space');assert(await page.locator('.mobile-menu nav').isVisible());
      await page.keyboard.press('Escape');assert.equal(await page.locator('.mobile-menu').getAttribute('open'),null);
      await page.keyboard.press('Control+k');assert.equal(await page.evaluate(()=>document.activeElement.id),'header-search');
      await page.goto(base+'/store/?q=لباس&sort=price_asc');
      await page.locator('[data-catalog-filters] summary').click();assert(await page.locator('#filter-form').isVisible());
      await page.locator('#filter-form [name=min_price]').fill('1000000')
      await page.locator('#filter-form [name=max_price]').fill('1');
      await page.getByRole('button',{name:'اعمال فیلترها',exact:true}).click();
      assert(await page.locator('#filter-form [role=alert]').textContent().then(text=>text.includes('حداکثر قیمت نمی‌تواند کمتر از حداقل قیمت باشد')));
      assert(await page.locator('#filter-form').isVisible());
      await page.goto(base+'/store/?min_price=abc');assert.equal(await page.locator('#filter-form [name=min_price]').getAttribute('aria-invalid'),'true');
      assert(await page.locator('#filter-form').isVisible());
      await page.goto(base+'/store/?q=لباس');await page.locator('#id_sort').selectOption('price_desc');await page.getByRole('button',{name:'اعمال',exact:true}).click();
      assert.equal(new URL(page.url()).searchParams.get('q'),'لباس');assert.equal(new URL(page.url()).searchParams.get('sort'),'price_desc');
      await page.goto(base+'/product/qa-product-0/');
      await page.locator('[data-gallery-zoom]').click();assert(await page.locator('#gallery-dialog').isVisible());
      await page.keyboard.press('Escape');assert.equal(await page.locator('#gallery-dialog').isVisible(),false);assert.equal(await page.evaluate(()=>document.activeElement.hasAttribute('data-gallery-zoom')),true);
      await page.getByLabel('آبی آزمایش',{exact:true}).check();await page.getByLabel('M',{exact:true}).check();
      const before = await page.locator('[name=quantity]').inputValue();await page.getByRole('button',{name:'افزایش تعداد',exact:true}).click();assert.equal(await page.locator('[name=quantity]').inputValue(),String(Number(before)+1));
      assert.equal(await page.locator('[data-product-form] [type=submit]').isEnabled(),true);
      assert.equal((await page.locator('[data-cart-count]').textContent()).trim(),'0');
      await page.locator('[data-gallery-src]').nth(1).click();assert.equal(await page.locator('[data-gallery-src]').nth(1).getAttribute('aria-pressed'),'true');
      await page.locator('[data-product-form] [type=submit]').click();await page.waitForLoadState('networkidle');
      await page.goto(base+'/cart/');assert(await page.locator('#cart-content').textContent().then(s=>s.includes('لباس آزمایشی')));
      await page.locator('[data-qty-step="1"]').first().click();await page.waitForTimeout(600);assert.equal((await page.locator('[data-cart-count]').textContent()).trim(),'3');assert(await page.locator('[data-cart-feedback]').isVisible());
      expectedNetworkFailure=true;
      await page.route('**/cart/update/**', route => route.abort('internetdisconnected'));
      await page.getByRole('button',{name:'بروزرسانی',exact:true}).click();
      await page.locator('[data-cart-network-error]').waitFor({state:'visible'});
      await page.unroute('**/cart/update/**');expectedNetworkFailure=false;
      await page.getByRole('button',{name:'بروزرسانی',exact:true}).click();
      await page.locator('[data-cart-network-error]').waitFor({state:'hidden'});
      await page.getByRole('button',{name:'حذف لباس آزمایشی 1 از سبد خرید',exact:true}).click();await page.waitForTimeout(600);assert(await page.getByText('سبد خرید شما خالی است',{exact:true}).isVisible());assert.equal((await page.locator('[data-cart-count]').textContent()).trim(),'0');
      await page.goto(base+'/accounts/login/');await page.locator('[name=phone_number]').fill('invalid');await page.getByRole('button',{name:'ارسال کد تأیید',exact:true}).click();assert.equal(await page.locator('[name=phone_number]').getAttribute('aria-invalid'),'true');
    }
    if (role==='customer') {
      await page.goto(base+'/accounts/addresses/new/');
      for(const [name,value] of Object.entries({receiver_name:'گیرنده آزمایش مرورگر',receiver_phone:'09120000002',city:'تهران',address_line:'آدرس موقت ایجادشده فقط برای آزمون مرورگر',postal_code:'1234567890'}))await page.locator(`[name="${name}"]`).fill(value);
      await page.locator('[name=province]').selectOption('تهران');await page.getByRole('button',{name:'ذخیره آدرس',exact:true}).click();await page.waitForLoadState('networkidle');
      const addressCard=page.locator('article').filter({has:page.getByRole('heading',{name:'گیرنده آزمایش مرورگر',exact:true})});
      assert(await addressCard.isVisible());page.once('dialog',dialog=>dialog.accept());await addressCard.getByRole('button',{name:'حذف آدرس',exact:true}).click();await page.waitForLoadState('networkidle');assert.equal(await page.getByRole('heading',{name:'گیرنده آزمایش مرورگر',exact:true}).count(),0);
      await page.goto(base+'/checkout/');await page.getByRole('button',{name:'ادامه با آدرس انتخاب‌شده'}).click();await page.waitForLoadState('networkidle');assert(page.url().endsWith('/checkout/review/'));
      await page.getByRole('button',{name:/ثبت سفارش و پرداخت/}).click();await page.waitForURL(/\/payments\/mock\//);await page.getByRole('link',{name:'آزمایش نتیجه موفق'}).click();await page.waitForURL(/\/success\//);assert(await page.getByRole('heading',{name:'پرداخت با موفقیت انجام شد'}).isVisible());
    }
    if (role==='admin') {
      await page.goto(base+'/admin/products/product/');
      await page.locator('input.action-select').first().check();await page.locator('select[name=action]').first().selectOption('publish_products');await page.locator('button[name=index]').first().click();await page.waitForLoadState('networkidle');assert((await page.locator('.messagelist').textContent()).includes('منتشر'));
      await page.goto(base+'/admin/products/product/add/');
      const totalBefore = Number(await page.locator('#id_images-TOTAL_FORMS').inputValue());
      await page.locator('#images-group .add-row a').click();
      assert.equal(Number(await page.locator('#id_images-TOTAL_FORMS').inputValue()), totalBefore+1);
      await page.locator('input[type=file][name$="-image"]').first().setInputFiles({name:'qa-preview.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=','base64')});
      assert(await page.locator('.admin-upload-preview').isVisible());
      assert.equal(await page.locator('.admin-upload-preview img').evaluate(image=>image.src.startsWith('blob:')),true);
    }
    await context.close();
  }
  const reducedContext = await browser.newContext({reducedMotion:'reduce'});const reducedPage=await reducedContext.newPage();
  await reducedPage.goto(base+'/product/qa-product-0/');assert.equal(await reducedPage.locator('.btn-primary').first().evaluate(el=>getComputedStyle(el).transitionDuration),'0s');await reducedContext.close();
  const nojs = await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:900}});const p=await nojs.newPage();await p.goto(base+'/product/qa-product-0/');assert(await p.locator('#variant-select').isVisible());await p.locator('#variant-select').selectOption({label:'آبی آزمایش / M'});await p.locator('[data-product-form] [type=submit]').click();await p.goto(base+'/cart/');assert(await p.getByText('لباس آزمایشی 1',{exact:true}).isVisible());await nojs.close();
  const loginContext=await browser.newContext({viewport:{width:390,height:900}});const loginPage=await loginContext.newPage();await loginPage.goto(base+'/admin/login/');await loginPage.locator('[name=username]').fill('09120000001');await loginPage.locator('[name=password]').fill(auth.admin_password);await loginPage.locator('.submit-row input[type=submit]').click();await loginPage.waitForURL(base+'/admin/');assert(await loginPage.locator('.shop-stats').isVisible());await loginContext.close();
  await browser.close();
  fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({results,issues,interactions:'passed'},null,2));
  console.log(JSON.stringify({pages:results.length,issues:issues.length,interactions:'passed',report:path.join(out,'report.json')}));
  if(issues.length)process.exitCode=1;
})().catch(async error=>{await browser?.close();fs.writeFileSync(path.join(out,'report.json'),JSON.stringify({results,issues,error:error.message},null,2));console.error(error.message);process.exitCode=1});
