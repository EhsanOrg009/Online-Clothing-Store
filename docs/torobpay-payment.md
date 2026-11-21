# پرداخت اعتباری ترب‌پی در مینی‌کندو

قرارداد اصلی، فایل ارسالی `TorobPayCPG.pdf` نسخهٔ `1.3.31` است. تاریخ جدول تغییرات `2026-10-25`، ولی سربرگ صفحهٔ ۲ «۲۱ فروردین ۱۴۰۵» یعنی `2026-04-10` است. SHA256 فایل بررسی‌شده:

```text
ce3b44e95fd06a025a318d92274eb1e0247e2089922181834e7f34185b86c27d
```

در بررسی ۲۰ نوامبر ۲۰۲۶، سایت عمومی `https://torobpay.com` در دسترس بود؛ مسیرهای عمومی Swagger/OpenAPI روی `cpg.torobpay.com` پاسخ ۴۰۴ داشتند و `docs.torobpay.com` resolve نشد. هیچ منبع عمومی دیگری معنای واحد/خط، حساب سبد یا وضعیت دقیق را تأیید نکرد. فرض‌های زیر تأیید تازهٔ ارائه‌دهنده محسوب نمی‌شوند. هیچ درخواست خرید یا بازپرداخت واقعی در پیاده‌سازی/آزمون اجرا نشده است.

## تنظیمات و فعال‌سازی

چهار credential را از پنل رسمی یا مسئول فنی پذیرندگان ترب‌پی، مطابق قرارداد پذیرندگی دریافت کنید. این‌ها اطلاعات حساب فروشگاه یا مشتری نیستند؛ پذیرنده باید نزد ترب‌پی فعال باشد. مقدار واقعی را فقط مالک در محیط خصوصی قرار دهد؛ فایل‌های مثال، محل نگهداری secret نیستند.

| متغیر | کاربرد و پیش‌فرض |
| --- | --- |
| `PAYMENT_GATEWAY` | روش پیش‌فرض؛ رفتار قبلی `zibal` حفظ می‌شود |
| `PAYMENT_GATEWAYS` | خالی: فقط روش قبلی؛ `zibal,torobpay`: هر دو روش |
| `TOROBPAY_CLIENT_ID` | شناسهٔ client از پذیرندگان ترب‌پی |
| `TOROBPAY_CLIENT_SECRET` | secret همان client، فقط محیط خصوصی |
| `TOROBPAY_USERNAME` | نام کاربری پذیرندهٔ ترب‌پی |
| `TOROBPAY_PASSWORD` | رمز پذیرندهٔ ترب‌پی |
| `TOROBPAY_BASE_URL` | پیش‌فرض `https://cpg.torobpay.com`؛ HTTPS و همین میزبان الزامی |
| `TOROBPAY_TIMEOUT_SECONDS` | پیش‌فرض ۱۰، timeout جداگانهٔ connect و read؛ مثبت و حداکثر ۳۰ |
| `TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS` | پیش‌فرض ۳۰۰؛ JWT حداکثر ۳۳۰۰ ثانیه cache می‌شود و exp کوتاه‌تر نیز رعایت می‌شود |
| `TOROBPAY_ALLOWED_PAYMENT_HOSTS` | پیش‌فرض `cpg.torobpay.com,torobpay.com`؛ فقط میزبان‌های صریح زیر دامنهٔ ترب‌پی |
| `SITE_URL` | origin فروشگاه؛ HTTPS در production |
| `ZIBAL_MERCHANT` | هنگام فعال‌بودن زیبال، همان پذیرندهٔ واقعی قبلی |

روش پیش‌فرض باید در فهرست فعال باشد؛ انتخاب از POST فقط نام کوتاه روش فعال را می‌پذیرد. مسیر کلاس سازگار موجود زیبال و مسیر کلاس ترب‌پی در تنظیمات قابل استفاده‌اند. production چهار credential ترب‌پی را فقط وقتی فعال است الزامی می‌کند؛ development با credential ناقص آن را در checkout پنهان می‌کند. Compose پایه متغیرهای جدید را به `web` می‌دهد و override تولیدی همان environment را به ارث می‌برد؛ سرویس Docker تازه‌ای اضافه نشده است.

دامنهٔ HTTPS `SITE_URL` و مسیر POST زیر را طبق قواعد ثبت callback ترب‌پی ثبت/تأیید کنید. بخش UUID برای هر تلاش متفاوت است؛ پذیرنده باید دامنه/الگوی مسیر را مجاز کند، نه یک UUID ثابت:

```text
https://<store-host>/payments/torobpay/callback/<request-uuid>/
```

HTTP فقط برای callback توسعه با `DEBUG=True` مجاز است. Base URL API و URL میزبانی پرداخت همیشه HTTPS هستند. اگر ارائه‌دهنده میزبان دیگری برگرداند، تنها پس از تأیید رسمی همان زیر دامنه را صریحاً به allowlist اضافه کنید؛ مقصد عمومی یا scheme غیر HTTPS مجاز نیست.

## معماری و نمایش checkout

gateway جدید جایگزین زیبال نشده است. interface اصلی سه‌متدی حفظ شده؛ قابلیت‌های eligibility، مراحل callback، status، settle، revert، cancel و update فقط در adapter ترب‌پی و سرویس آن هستند. فایل سرویس عمومی در checkout فعلی `payments/services.py` است، نه یک پوشهٔ services. Mock در نسخهٔ بررسی‌شده حذف شده بود و هنوز روش قابل انتخاب نیست.

checkout از `Cart` و pricing سروری مبلغ فعلی را محاسبه می‌کند. مبلغ کامل پس از تخفیف/ارسال به ریال تبدیل و `GET /api/online/offer/v1/eligible` اجرا می‌شود. تنها `eligible=true` همراه اطلاعات لازم مشتری/نشانی گزینه را نمایش می‌دهد. بازهٔ شامل دو مرز: ۲۰۰٬۰۰۰ تا ۱٬۰۰۰٬۰۰۰٬۰۰۰ ریال، معادل ۲۰٬۰۰۰ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان مبلغ نهایی. false، timeout، خطای auth، JSON خراب یا اطلاعات ناقص، فقط ترب‌پی را پنهان می‌کند. متن title/description با escaping معمول template نمایش داده می‌شود.

نتیجهٔ eligibility cache مشتری ندارد؛ فقط JWT cache می‌شود. authorization داخلی کوتاه‌مدت به مشتری، مبلغ، نشانی و یک سفارش متصل می‌شود. اگر pricing هنگام ثبت عوض شود، eligibility برای snapshot واقعی سفارش دوباره اجرا می‌شود. GET سفارش/توکن نمی‌سازد. POST روش جعل‌شده یا غیرفعال را رد می‌کند. تلاش pending قبلی به روش دیگری تبدیل نمی‌شود و تا تعیین وضعیتش رسید تازه ساخته نمی‌شود.

## پول و سبد خرید

تمام DB و نمایش مشتری به تومان می‌مانند. `toman_to_rial` با Decimal ضرب در ۱۰ و تبدیل صحیحِ معتبر را انجام می‌دهد؛ float/bool، منفی، غیرمتناهی، ورودی خراب یا ریال اعشاری رد می‌شود. هیچ مبلغ مشتری از POST پذیرفته نمی‌شود.

فرض صریح برای ابهام PDF:

```text
cartItems.amount = original_unit_price × 10   (قیمت واحد قبل از تخفیف)
cartItems.count = OrderItem.quantity
cart.totalAmount = Σ(amount × count) + shippingAmount
amount = cart.totalAmount - discountAmount
discountAmount = Order.discount_total × 10
```

`isShipmentIncluded=true` و shipping از snapshot سفارش است. tax/فلگ مالیات و external funding ارسال نمی‌شوند چون فروشگاه چنین مؤلفه‌ای ذخیره نمی‌کند. دقیقاً یک cart با `cartId=order_number` و تمام ردیف‌های snapshot ارسال می‌شود. ID هر ردیف همان PK پایدار `OrderItem` است؛ نام/رنگ/سایز و دسته‌بندی snapshot استفاده می‌شوند. category جدید هنگام ساخت سفارش ثبت می‌شود؛ سفارش قدیمی بدون آن برای ترب‌پی قابل ارسال نیست و از محصول قابل تغییر امروز پر نمی‌شود.

نام گیرنده، نشانی، استان، شهر، کد پستی و تلفن گیرنده از Order هستند. شمارهٔ ثبت‌نام از حساب ذخیره‌شدهٔ مشتری می‌آید. هر دو تلفن با قرارداد فعلی فروشگاه `09XXXXXXXXX` نرمال و کد پستی با ۱۰ رقم ASCII معتبر می‌شوند. PDF شکل کشور/شماره را دقیق‌تر مشخص نکرده است. `ONLINE_CREDIT` ثابت است. `commissionType` حذف شده چون مثال string و جدول integer/0 متناقض‌اند؛ هیچ مقدار حدسی ارسال نمی‌شود.

## چرخهٔ پرداخت و زمان‌ها

۱. قبل از token یک Payment با request UUID، مبلغ تومان و snapshot digest زیر قفل سفارش ثبت می‌شود. HTTP بعد از commit اجرا می‌شود. timeout بعد از ارسال token به معنی ساخته‌نشدن سفارش نیست؛ همان تلاش در `uncertain` باقی می‌ماند و دوباره ارسال نمی‌شود. خطای احراز هویت پیش از ارسال درخواست token، رد محلیِ بدون ایجاد است. رد صریحِ مستند نیز با سابقهٔ قبلی محفوظ، تلاش بعدی را مجاز می‌کند.

۲. token محرمانه در `provider_token` ذخیره می‌شود. authority ترب‌پی SHA256 توکن است تا محدودیت ۶۴ کاراکتری و uniqueness موجود حفظ شود؛ authority زیبال همان track ID می‌ماند. توکن در metadata قرار نمی‌گیرد؛ فقط URL رسمیِ لازم برای redirect آنجا نگه‌داری می‌شود. URL خراب، حتی با token معتبر، redirect نمی‌شود و token برای بازیابی حفظ می‌گردد. حد طول/کاراکتر token در client یک محدودیت دفاعی محلی است، نه ادعای محدودیت PDF.

۳. صفحهٔ میانی فعلی با `no-referrer` به URL مجاز می‌رود. callback ترب‌پی فقط POST form-urlencoded محدود، دقیقاً سه پارامتر غیرتکراری و UUID/مبلغ منطبق را می‌پذیرد. CSRF exemption فقط همین endpoint است. callback نه cookie ورود می‌سازد و نه message session جدید؛ پاسخ ۳۰۳ کاربر را به GET نتیجه می‌برد. callback GET زیبال حفظ شده است.

۴. status سروری با مبلغ ریال و transaction ID ذخیره‌شده تطبیق می‌شود. callback `OK/FAILED` اثبات پرداخت نیست. برای مسیر معمول، `verify → status(VERIFY) → بررسی وضعیت سفارش/رزرو → settle → status(SETTLE)` اجرا می‌شود. verify تنها receipt میانی است. `mark_order_paid` فقط پس از تأیید نهایی و دقیقاً یک بار فراخوانی می‌شود.

۵. PDF زمان واقعی پرداخت را نمی‌دهد. با سیاست انتخابی مالک، مشاهدهٔ سروری SETTLE داخل مهلت رزرو، کران بالای معتبر زمان پرداخت است؛ `gateway_paid_at=NULL` باقی می‌ماند و زمان callback/ساخت سفارش جای آن گذاشته نمی‌شود. کران و شواهد مقدار/شناسه به‌صورت پایدار ثبت می‌شوند. تأیید دیرهنگام، هویت/مبلغ متعارض، موجودی از دست‌رفته یا receipt دستی دیگر، fulfillment خودکار نمی‌دهد. سفارش لغوشده/رزرو آزادشده احیا نمی‌شود.

| API status | معنای قابل اتکا و رفتار |
| --- | --- |
| `PENDING` | NEW/IPG/W_FOR_VERIFY؛ pending و حفظ رزرو، نه اثبات عدم دریافت وجه |
| `VERIFY` | W_FOR_SETTLE؛ میانی، بررسی fulfillment و settle |
| `SETTLE` | ONGOING/PAID/UPDATED؛ پذیرش فقط با تطبیق و سیاست مهلت |
| `REVERT` | FAILED/CANCELED؛ با receipt و زمینهٔ عملیات تطبیق می‌شود |

۱۵ دقیقهٔ ورود، ۱۰ دقیقهٔ IPG، تا ۱۰ دقیقهٔ بازگشت به W_FOR_VERIFY، ۱۰ دقیقهٔ verify و ۱۰ دقیقهٔ حرکت خودکار پس از verify، مهلت‌های متفاوت‌اند. رزرو محلی ۳۰ دقیقه تغییر نکرده است. پس از موعد، PENDING/UNKNOWN ترب‌پی مجوز آزادسازی stock نیست؛ `reservation_hold` آن را تا status نهایی/بررسی نگه می‌دارد. سفارشِ از قبل غیرقابل تحویل به مسیر verify/تسویهٔ خودکار برده نمی‌شود. انقضا قبل از قفل، reconciliation می‌کند و زیر قفل hold را دوباره می‌خواند.

PDF هم انتقال خودکار به ONGOING بعد از ۱۰ دقیقه و هم الزام settle تا پایان روز را می‌گوید. مسیر عادی settle صریح دارد؛ پس از پاسخ نامطمئن یا انتقال خودکار، status مقدم است و settle کورکورانه تکرار نمی‌شود. SETTLE خودکار داخل مهلت با شواهد منطبق قابل پذیرش است؛ فلگ `explicit_settle` برای بررسی تعهد پایان روز حفظ می‌شود. امکان settle مجدد ONGOING مستند نیست و باید با ارائه‌دهنده روشن شود.

## بازیابی و عملیات دوره‌ای

هر عملیات remote یک lease و timestamp پایدار دارد؛ زمان پیش‌فرض lease با timeout ده‌ثانیه‌ای ۱۸۰ ثانیه است. duplicate callback فقط شمارنده را زیاد می‌کند. HTTP هیچ قفل DB را نگه نمی‌دارد؛ نتایج با ترتیب Order، Payment، واریانت‌ها به PK صعودی اعمال می‌شوند. پاسخ worker قدیمی شواهدش را ثبت می‌کند و تصمیم worker جدید را بازنویسی نمی‌کند.

بازیابی زیبال نیز پرداخت دارای `reservation_hold` را مانع انتخاب رسید اصلی می‌داند؛ حتی اگر token ثبت شده ولی هیچ رسید میانی هنوز دیده نشده باشد. تا تعیین وضعیت ترب‌پی، ابزار بازیابی زیبال مجوز پذیرش رسید دیگری برای همان سفارش نمی‌دهد. این hold به fingerprint برنامهٔ بازیابی نیز متصل است تا تغییر هم‌زمانش مجوز قبلی را بی‌اعتبار کند.

استعلام پیش‌فرض فقط وضعیت می‌خواند و Payment/Order را تغییر نمی‌دهد:

```sh
uv run python manage.py reconcile_torobpay_payments --payment-id <PAYMENT_ID> --dry-run
```

اعمال reconciliation عادی، همان قواعد callback را دارد؛ نه force-primary، نه درخواست پرداخت تازه و نه refund تازه:

```sh
uv run python manage.py reconcile_torobpay_payments --apply --limit 100
```

در runner عملیاتی موجود، هر یک دقیقه این command را اجرا و `expire_pending_orders` موجود را هر پنج دقیقه حفظ کنید. در تولید از همان wrapper و web استفاده می‌شود؛ این راهنما هیچ Cron/systemd یا سرویس جدید نصب نکرده است:

```sh
scripts/compose-production exec -T web python manage.py reconcile_torobpay_payments --apply --limit 100
scripts/compose-production exec -T web python manage.py expire_pending_orders
```

اگر token create گم شده، status با transaction UUID مستند نیست. token را مستقل از پنل/پشتیبانی ترب‌پی پیدا کنید، در فایل خارج از Git با دسترسی ۶۰۰ و mount خواندنی بگذارید و با بکاپ تازهٔ SQL و SHA256 معتبر به همان Payment متصل کنید:

```sh
uv run python manage.py reconcile_torobpay_payments --payment-id <PAYMENT_ID> --apply \
  --actor-id <AUTHORIZED_STAFF_ID> --token-file /private/torobpay-token \
  --backup /readonly/fresh.sql.gz
```

اتصال token به‌تنهایی verify/fulfillment نمی‌کند و URL پرداخت از status ساخته نمی‌شود. بدون token معتبر، attempts پاک/بازنشانی یا درخواست create تکرار نشوند.

## لغو و بازپرداخت

`cancel_order_with_payment` قابلیت اختیاری درگاه را اجرا می‌کند؛ `cancel_order` و تغییر مستقیم وضعیت ادمین برای receipt ترب‌پی پیش از تأیید remote، stock را آزاد نمی‌کنند. عملیات remote باید بیرون atomic فرم ادمین فراخوانی شود. سفارش shipped/delivered و گذارهای غیرمجاز قابل لغو نیستند.

`revert` فقط با شاهد مستقل staff از W_FOR_VERIFY و زمان واقعی کمتر از ۳۰ دقیقه انتخاب می‌شود. PENDING این وضعیت را ثابت نمی‌کند. در مرز دقیق ۳۰ دقیقه، cancel انتخاب می‌شود؛ انتظار کانال SEP تا و شامل ۳۰ دقیقه و PAYA پس از آن از مراحل توضیحی PDF گرفته شده، با وجود متن دیگرِ «کمتر از ۳۰ دقیقه». کانال واقعی را ترب‌پی تعیین می‌کند؛ این کد تأیید وصول بانکی یا مبلغ اقساط پرداخت‌شده را اختراع نمی‌کند. 1065 ابتدا status تازه می‌گیرد و فقط اگر حالت مجاز روشن باشد به cancel می‌رود.

پس از timeout cancel/revert، همان intent حفظ و status قبل از هر تصمیم اجرا می‌شود؛ stock/وضعیت لغو فقط پس از تأیید REVERT منطبق با زمینهٔ عملیات اعمال می‌شوند. رد قطعی اثر refund ندارد و receipt قبلی حفظ می‌شود. هیچ refund خودکار جدید از job انقضا/reconciliation فرستاده نمی‌شود.

command لغو پیش‌فرض read-only است. apply صریح به staff مجاز و بکاپ یک‌ساعتهٔ معتبر به همراه SHA256 موجود نیاز دارد:

```sh
uv run python manage.py cancel_torobpay_payment --payment-id <PAYMENT_ID> --actor-id <STAFF_ID> --dry-run
uv run python manage.py cancel_torobpay_payment --payment-id <PAYMENT_ID> --actor-id <STAFF_ID> \
  --apply --backup /readonly/fresh.sql.gz
```

برای حالت دقیق نامعلوم، فقط پس از شاهد مستقل `--remote-state` و `--support-reference` اضافه کنید؛ `--gateway-paid-at` باید زمان واقعیِ دارای offset باشد، نه `paid_at` محلی، creation یا callback. SETTLE تنها مجموعهٔ حالت‌هاست؛ اگر فقط انتقال خودکار یا UPDATED دیده شده، ONGOING حدس زده نمی‌شود. بعد از settle صریح، آخرین حالت شناخته‌شده و status تازه بررسی می‌شوند و endpoint نیز حالت دقیق فعلی را الزام می‌کند.

## کاهش مبلغ سفارش

فروشگاه UI ویرایش سفارش پرداخت‌شده ندارد؛ UI جدید اضافه نشده است. قابلیت staff-only `update_payment(payment, quantities={saved_item_id: new_count}, actor=staff)` فقط کاهش/حذف تعدادِ اقلام ثبت‌شده را پشتیبانی می‌کند. قیمت، دسته و shipping snapshot حفظ می‌شوند. تنها سفارش محلی paid/preparing با حالت remote ONGOING، مبلغ کمتر و بدون هر تخفیف قابل اجراست. مقدار برابر/بیشتر یا حذف همهٔ اقلام رد می‌شود؛ لغو تأییدشده و سفارش جدید تصمیم جداگانهٔ مجاز است.

پیشنهاد قبل از HTTP ذخیره، status قبل از update و بعد از آن بررسی می‌شود. فقط پس از تأیید، totals فعلی سفارش/quantity و stock اختلاف تغییر می‌کنند. مقدار و شناسه/زمان receipt اولیهٔ Payment ثابت و snapshot کامل قبل از ویرایش در history باقی می‌ماند؛ مقدار و transaction ID نسخهٔ جدید جدا در metadata هستند.

timeout با مبلغ جدید و ID قبلی قابل reconciliation است؛ تغییر ID بدون پاسخ، manual review می‌خواهد. با ID مستقلِ تأییدشده و بکاپ معتبر می‌توان همان پیشنهاد محفوظ را بازیابی کرد، بدون POST update دوم:

```sh
uv run python manage.py reconcile_torobpay_payments --payment-id <PAYMENT_ID> --apply \
  --actor-id <STAFF_ID> --updated-transaction-id <CONFIRMED_UPDATED_ID> \
  --support-reference <INDEPENDENT_SUPPORT_REFERENCE> --backup /readonly/fresh.sql.gz
```

## migration، امنیت و آمادگی تولید

`orders/0003` فقط category snapshot و `payments/0005` فقط token محرمانه و hold را اضافه می‌کنند. مبلغ، authority، UUID، references، metadata و timestamps قدیمی rewrite نمی‌شوند. rollback payment/0005 با شواهد ترب‌پی رد می‌شود تا اطلاعات مالی حذف نشود؛ بازگشت نسخه پس از استفاده نیازمند طرح بکاپ/بازیابی بازبینی‌شده است. fixture تست migration قدیمی schema نهایی را کامل restore می‌کند.

token در DB با دسترسی محدود ذخیره می‌شود؛ رمزنگاری جدیدِ فیلد اضافه نشده است. حفاظت storage و بکاپ‌های خصوصی طبق سیاست استقرار مالک باقی می‌ماند. JWT فقط cache خصوصی است. لاگ اپلیکیشن شامل operation، شناسه‌های محلی، correlation و کدهای پالایش‌شده است؛ پیام خام API، credentials، body callback و URL محرمانه چاپ نمی‌شوند. ادمین token و URL را نمایش نمی‌دهد و صفحات مشتری فقط URL رسمی لازم در صفحهٔ انتقال را دارند.

پیش از rollout: چهار credential پذیرندهٔ فعال را امن تنظیم، domain/callback POST را تأیید، فرض‌های سبد/زمان/میزبان را با ترب‌پی بررسی، بکاپ تازه و restore آزمایشی تهیه، migration plan را مرور و در پنجرهٔ مجاز یک بار اعمال کنید. سپس readiness، هر دو روش، runner یک‌دقیقه‌ای، هشدارهای review و تعهد settle روزانه را بررسی کنید. در این وظیفه production deploy، production migration، تغییر `.env.production` یا خرید/refund زنده انجام نشده است.

## آزمون محلی

HTTP همهٔ آزمون‌های ترب‌پی/زیبال mock است. QA از دیتابیس تولیدی استفاده نمی‌کند:

```sh
PYTHONPATH=scripts/qa uv run python manage.py test --settings=shop_test_settings --noinput
PYTHONPATH=scripts/qa uv run python manage.py check --settings=shop_test_settings
PYTHONPATH=scripts/qa uv run python manage.py makemigrations --check --dry-run --settings=shop_test_settings
npm run build
```

SQLite قفل واقعی را ثابت نمی‌کند؛ suite و tests concurrency باید روی MariaDB جداگانه نیز اجرا شوند. محیط QA موقت با `--no-defaults`، datadir تازه، `--skip-networking` و Unix socket خصوصی در `/tmp` ساخته می‌شود؛ timezone سیستم فقط به همین DB وارد می‌شود. تعداد/نتیجهٔ اجرای واقعی در گزارش تحویل ثبت می‌شود؛ هیچ آزمون اجرا نشده‌ای یا عملیات زنده، موفق فرض نمی‌شود.

نتیجهٔ بررسی تحویل در ۲۱ نوامبر ۲۰۲۶، پس از آخرین اصلاح حفاظت بازیابی:

| بررسی | نتیجهٔ واقعی |
| --- | --- |
| مجموعهٔ کامل با `shop_test_settings` و SQLite | ۶۷۹ آزمون، موفق؛ ۲۷ آزمون قفل به‌علت محدودیت SQLite اجرا نشد |
| همان مجموعه روی MariaDB 11.4.13 ایزوله | ۶۷۹ آزمون، موفق؛ بدون آزمون کنارگذاشته‌شده |
| `manage.py check` با تنظیمات QA | بدون ایراد |
| `makemigrations --check --dry-run` با تنظیمات QA | بدون تغییر اعلام‌نشدهٔ مدل |
| `npm run build` | موفق؛ CSS و فایل‌های vendor ساخته شدند |
| `git diff --check` | موفق |
| اعتبارسنجی فقط‌خواندنی Compose با فایل مثال و مقادیر کاملاً ساختگی | موفق؛ هیچ کانتینری ساخته یا اجرا نشد |

برای این بررسی از `.venv/bin/python` استفاده شد؛ فرمان MariaDB، `manage.py test --settings=settings_torobpay_mariadb --noinput --verbosity 2` با ماژول QA موقت و سوکت خصوصی بود. این تنظیمات/داده‌های موقت جایگزین تنظیمات محیط تولید نیستند. هیچ وابستگی تازه‌ای اضافه نشد و هیچ خرید یا بازپرداخت زنده آزمایش نشد.
