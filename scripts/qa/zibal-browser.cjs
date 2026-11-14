/* Browser-only HTTP interception: no requests reach the financial gateway. */
async function mockZibalPage(context, base) {
  const origin = new URL(base);
  if (!['127.0.0.1', 'localhost'].includes(origin.hostname)) throw new Error('Zibal browser QA must use a local server.');
  await context.route('https://gateway.zibal.ir/**', async route => {
    const match = new URL(route.request().url()).pathname.match(/^\/start\/([1-9][0-9]*)$/);
    if (!match) throw new Error('Unexpected external Zibal request.');
    const referer = (await route.request().allHeaders()).referer;
    if (referer !== origin.origin + '/') throw new Error('Zibal Start did not receive the storefront Referer.');
    const callback = origin.origin + '/__qa__/zibal/return/' + match[1];
    const body = '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><title>آزمون زیبال</title></head><body><main><h1>آزمون پاسخ درگاه زیبال</h1><p>پاسخ شبکه شبیه‌سازی شده است و هیچ وجهی جابه‌جا نمی‌شود.</p><a href="' + callback + '/successful/">آزمایش نتیجه موفق</a><br><a href="' + callback + '/failed/">آزمایش نتیجه ناموفق</a><br><a href="' + callback + '/cancelled/">آزمایش انصراف</a></main></body></html>';
    await route.fulfill({status:200,contentType:'text/html',body});
  });
}
module.exports = {mockZibalPage};
