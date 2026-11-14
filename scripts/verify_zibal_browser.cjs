/* Optional Playwright checks against guarded /tmp fixtures; no financial network calls. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {mockZibalPage} = require('./qa/zibal-browser.cjs');
const base = process.env.SHOP_QA_URL || 'http://127.0.0.1:8765';
const auth = JSON.parse(fs.readFileSync(process.env.SHOP_QA_AUTH_FILE || '/tmp/shop-qa-auth.json', 'utf8'));
const pending = auth.payment_routes.find(route => route.startsWith('/payments/redirect/'));
assert(pending, 'Prepare the isolated payment fixtures first.');
const checks = [];
let browser;

async function contextForPayment(javaScriptEnabled, manual = false) {
  const context = await browser.newContext({javaScriptEnabled});
  await context.addCookies([{name:'sessionid',value:auth.customer,url:base}]);
  await mockZibalPage(context, base);
  await context.route('**/payments/redirect/*', async route => {
    const response = await route.fetch();
    let body = await response.text();
    if (manual) body = body.replace(/<meta http-equiv="refresh"[^>]*>/, '');
    // Simulate the additional same-origin policy sent by production Nginx.
    await route.fulfill({response,body,headers:{...response.headers(),'referrer-policy':'strict-origin-when-cross-origin, same-origin'}});
  });
  return context;
}

(async () => {
  browser = await chromium.launch({executablePath:process.env.SHOP_QA_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  for (const javaScriptEnabled of [true, false]) {
    for (const manual of [false, true]) {
      const context = await contextForPayment(javaScriptEnabled, manual);
      const page = await context.newPage();
      await page.goto(base + pending);
      if (manual) await page.getByRole('link',{name:'ورود به درگاه',exact:true}).click();
      await page.waitForURL('https://gateway.zibal.ir/start/*');
      assert(await page.getByRole('heading',{name:'آزمون پاسخ درگاه زیبال'}).isVisible());
      checks.push({test:'official-start-and-referer',javaScriptEnabled,manual,passed:true});
      await context.close();
    }
  }

  const nojs = await contextForPayment(false);
  const page = await nojs.newPage();
  await page.goto(base + pending);
  await page.waitForURL('https://gateway.zibal.ir/start/*');
  await page.getByRole('link',{name:'آزمایش نتیجه ناموفق',exact:true}).click();
  await page.waitForURL('**/failed/');
  assert(await page.getByRole('heading',{name:'پرداخت ناموفق بود',exact:true}).isVisible());
  checks.push({test:'server-confirmed-failure',passed:true});
  await page.getByRole('button',{name:'تلاش دوباره برای پرداخت',exact:true}).click();
  await page.waitForURL('https://gateway.zibal.ir/start/*');
  await page.getByRole('link',{name:'آزمایش انصراف',exact:true}).click();
  await page.waitForURL('**/cancelled/');
  assert(await page.getByRole('heading',{name:'از پرداخت انصراف دادید',exact:true}).isVisible());
  checks.push({test:'retry-and-server-confirmed-cancellation',passed:true});
  await page.getByRole('button',{name:'تلاش دوباره برای پرداخت',exact:true}).click();
  await page.waitForURL('https://gateway.zibal.ir/start/*');
  const trackId = new URL(page.url()).pathname.split('/').pop();
  await page.getByRole('link',{name:'آزمایش نتیجه موفق',exact:true}).click();
  await page.waitForURL('**/success/');
  assert(await page.getByRole('heading',{name:'پرداخت با موفقیت انجام شد',exact:true}).isVisible());
  checks.push({test:'retry-and-server-verified-success-without-javascript',passed:true});
  const orderId = new URL(page.url()).pathname.split('/')[3];
  const callback = new URL(base + '/payments/callback/');
  callback.search = new URLSearchParams({trackId,orderId,success:'1',status:'2'}).toString();
  await page.goto(callback.href);
  await page.waitForURL('**/success/');
  assert(await page.getByRole('heading',{name:'پرداخت با موفقیت انجام شد',exact:true}).isVisible());
  checks.push({test:'duplicate-callback',passed:true});
  await nojs.close();

  const checkout = await contextForPayment(true);
  const purchase = await checkout.newPage();
  await purchase.goto(base + '/cart/');
  assert(await purchase.getByText('لباس آزمایشی 1',{exact:true}).isVisible());
  await purchase.goto(base + '/checkout/');
  await purchase.getByRole('button',{name:'ادامه با آدرس انتخاب‌شده',exact:true}).click();
  await purchase.waitForURL('**/checkout/review/');
  await purchase.getByRole('button',{name:/ثبت سفارش و پرداخت/}).click();
  await purchase.waitForURL('https://gateway.zibal.ir/start/*');
  await purchase.getByRole('link',{name:'آزمایش نتیجه موفق',exact:true}).click();
  await purchase.waitForURL('**/success/');
  assert(await purchase.getByRole('heading',{name:'پرداخت با موفقیت انجام شد',exact:true}).isVisible());
  checks.push({test:'cart-checkout-zibal-callback-result',passed:true});
  await checkout.close();
  await browser.close();
  fs.writeFileSync('/tmp/minikandoo-zibal-browser-result.json',JSON.stringify({checks,passed:checks.length},null,2));
  console.log(JSON.stringify({passed:checks.length,financialNetworkRequests:0,report:'/tmp/minikandoo-zibal-browser-result.json'}));
})().catch(async error => {await browser?.close();console.error(error.message);process.exitCode=1;});
