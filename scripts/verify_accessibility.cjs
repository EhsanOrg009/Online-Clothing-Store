/* Optional browser audit. Install Playwright and axe-core outside the application. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const base = process.env.SHOP_QA_URL || 'http://127.0.0.1:8765';
const auth = JSON.parse(fs.readFileSync(process.env.SHOP_QA_AUTH_FILE || '/tmp/shop-qa-auth.json', 'utf8'));
const output = process.env.SHOP_QA_A11Y_OUTPUT || '/tmp/shop-plum-accessibility.json';
const axe = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
let browser;
(async () => {
  browser = await chromium.launch({executablePath:process.env.SHOP_QA_CHROME || '/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
  const results = [];
  const routes = {
    guest:['/','/store/','/search/?q=absent','/product/qa-product-0/','/cart/','/accounts/login/','/admin/login/','/__qa__/404/','/__qa__/500/'],
    guest_verify:['/accounts/verify/'],
    customer_complete:['/accounts/complete-profile/'],
    customer:['/accounts/profile/','/accounts/profile/edit/','/accounts/addresses/','/accounts/addresses/new/','/cart/','/checkout/','/checkout/review/','/orders/',`/orders/${auth.order_number}/`,...auth.payment_routes],
    admin:auth.admin_routes,
  };
  for (const [role, paths] of Object.entries(routes)) {
    const context = await browser.newContext();
    if (auth[role]) await context.addCookies([{name:'sessionid',value:auth[role],url:base}]);
    const page = await context.newPage();
    for (const width of [360,1280]) {
      await page.setViewportSize({width,height:900});
      for (const route of paths) {
        const response = await page.goto(base+route,{waitUntil:'networkidle'});
        assert.equal(page.url(),new URL(base+route).href,`Unexpected redirect for ${role} ${route}`);
        const expectedStatus = route.startsWith('/__qa__/') ? Number(route.split('/')[2]) : 200;
        assert.equal(response.status(),expectedStatus,`Unexpected response for ${role} ${route}`);
        const csp = response.headers()['content-security-policy'] || '';
        const nonce = csp.match(/'nonce-([^']+)'/)?.[1];
        if (nonce) {
          await page.evaluate(({source,nonce})=>{const script=document.createElement('script');script.nonce=nonce;script.textContent=source;document.head.appendChild(script);},{source:axe,nonce});
        } else {
          // Standalone error responses have no nonce; use their permitted eval policy.
          await page.evaluate(source=>window.eval(source),axe);
        }
        if (!await page.evaluate(()=>Boolean(window.axe))) throw new Error(`Audit injection failed: ${role} ${route}`);
        const audit = await page.evaluate(async () => {
          const result = await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});
          return {violations:result.violations.map(v=>({id:v.id,impact:v.impact,help:v.help,nodes:v.nodes.map(n=>({target:n.target,html:n.html,summary:n.failureSummary}))})),incomplete:result.incomplete.map(v=>v.id)};
        });
        results.push({role,route,width,...audit});
        fs.writeFileSync(output,JSON.stringify(results,null,2));
      }
    }
    await context.close();
  }
  await browser.close();
  fs.writeFileSync(output,JSON.stringify(results,null,2));
  const violations = results.reduce((n,r)=>n+r.violations.length,0);
  console.log(JSON.stringify({pages:results.length,violations,report:output}));
  if (violations) process.exitCode = 1;
})().catch(async error=>{await browser?.close();console.error(error.message);process.exitCode=1;});
