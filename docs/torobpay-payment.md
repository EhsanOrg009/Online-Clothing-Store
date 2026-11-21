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

۵. PDF زمان واقعی پرداخت را نمی‌دهد. `Payment.settle_confirmed_at` نخستین زمان دریافت و تطبیق پاسخ معتبر سروری `SETTLE` است؛ این ستون **زمان واقعی پرداخت مشتری نیست**. زمان، بلافاصله پس از اعتبارسنجی پاسخ و پیش از انتظار برای قفل DB گرفته می‌شود. زیر قفل، ارتباط توکن/پرداخت/سفارش دوباره بررسی و زمان به همراه مبلغ ریال و شناسهٔ تراکنش فقط یک بار ثبت می‌شود. callback، ایجاد Payment و زمان نوشتن DB جای آن گذاشته نمی‌شوند؛ `gateway_paid_at=NULL` می‌ماند.

قاعدهٔ ترب‌پی دقیقاً `settle_confirmed_at < Order.expires_at` است. برابری با مهلت نیز دیرهنگام محسوب می‌شود؛ رفتار زمانی مستقل زیبال تغییر نکرده است. مهلت از ردیف واقعی سفارش خوانده و پیش از پاک‌شدن آن توسط سرویس پرداخت، در شواهد ثبت می‌شود. پاسخِ درون مهلت با نوشتن کمی دیرتر، در صورت سالم‌بودن رزرو و سایر شروط پذیرفته می‌شود. تأیید دیرهنگام، مبلغ/شناسهٔ متعارض، رزرو آزادشده و سفارش لغوشده به بررسی دستی می‌روند؛ سفارش یا موجودی احیا نمی‌شود. ادمین زمان مشاهده، مبلغ/شناسهٔ تأییدشده و دلیل بررسی را جداگانه نشان می‌دهد؛ این ستون نباید در صورتحساب یا گزارش مالی «زمان پرداخت مشتری» نامیده شود.

نسخهٔ قدیمی زمان اعمال DB را در `settlement_evidence.observed_at` نگه می‌داشت. زمان دقیق دریافت پاسخ تاریخی قابل بازسازی نیست؛ migration مقدار حدسی نمی‌سازد و ستون جدید برای آن سوابق NULL می‌ماند. رسید قدیمیِ قبلاً پذیرفته‌شده دوباره آماده‌سازی یا رد نمی‌شود. رسید ناقصِ منتظر پذیرش، بدون زمان مشاهدهٔ قابل اتکا، در بررسی دستی باقی می‌ماند.

| API status | معنای قابل اتکا و رفتار |
| --- | --- |
| `PENDING` | NEW/IPG/W_FOR_VERIFY؛ pending و حفظ رزرو، نه اثبات عدم دریافت وجه |
| `VERIFY` | W_FOR_SETTLE؛ میانی، بررسی fulfillment و settle |
| `SETTLE` | ONGOING/PAID/UPDATED؛ پذیرش فقط با تطبیق و سیاست مهلت |
| `REVERT` | FAILED/CANCELED؛ با receipt و زمینهٔ عملیات تطبیق می‌شود |

۱۵ دقیقهٔ ورود، ۱۰ دقیقهٔ IPG، تا ۱۰ دقیقهٔ بازگشت به W_FOR_VERIFY، ۱۰ دقیقهٔ verify و ۱۰ دقیقهٔ حرکت خودکار پس از verify، مهلت‌های متفاوت‌اند. رزرو محلی ۳۰ دقیقه تغییر نکرده است. پس از موعد، PENDING/UNKNOWN ترب‌پی مجوز آزادسازی stock نیست؛ `reservation_hold` آن را تا status نهایی/بررسی نگه می‌دارد. سفارشِ از قبل غیرقابل تحویل به مسیر verify/تسویهٔ خودکار برده نمی‌شود. انقضا قبل از قفل، reconciliation می‌کند و زیر قفل hold را دوباره می‌خواند.

PDF هم انتقال خودکار به ONGOING بعد از ۱۰ دقیقه و هم الزام settle تا پایان روز را می‌گوید. مسیر عادی settle صریح دارد؛ پس از پاسخ نامطمئن یا انتقال خودکار، status مقدم است و settle کورکورانه تکرار نمی‌شود. SETTLE خودکار داخل مهلت با شواهد منطبق قابل پذیرش است؛ فلگ `explicit_settle` برای بررسی تعهد پایان روز حفظ می‌شود. امکان settle مجدد ONGOING مستند نیست و باید با ارائه‌دهنده روشن شود.

## بازیابی و عملیات دوره‌ای

هر عملیات درگاه پیش از HTTP، با ترتیب قفل `Order → Payment` مالک پردازش و `reconciliation_lease_expires_at` را ثبت می‌کند. مهلت با timeout پیش‌فرض، ۱۸۰ ثانیه است و پیش/پس از هر مرحله تمدید می‌شود. مالک جدید فقط پس از پایان lease می‌تواند پردازش را با **status تازه** بازیابی کند؛ درخواست مالی نامعلوم تکرار نمی‌شود. lease قدیمیِ JSON با زمان معتبر به ستون محدود منتقل می‌شود؛ زمان خراب دیگر موجب انتظار بی‌پایان نمی‌شود. پاسخ worker قبلی در audit باقی می‌ماند و نتیجهٔ مالک جدید را بازنویسی نمی‌کند.

انقضا زیر قفل سفارش هم lease فعال و هم `reservation_hold` را بررسی می‌کند؛ حتی false بودن اشتباهِ فلگ، اجازهٔ آزادسازی هنگام lease فعال نمی‌دهد. HTTP بیرون تراکنش است. پایان lease فقط مالکیت worker را آزاد می‌کند؛ نامعلوم‌بودن وجه همچنان hold مالی یا بررسی دستی دارد. scheduler بعدی lease کرش‌شده را بازیابی می‌کند و فقط وضعیت قطعیِ پرداخت‌نشده، با قواعد موجود سفارش، اجازهٔ آزادسازی یک‌بارهٔ stock می‌دهد. انقضا در غیاب scheduler نیز این کنترل‌ها و استعلام لازم را دارد.

بازیابی زیبال نیز پرداخت دارای `reservation_hold` را مانع انتخاب رسید اصلی می‌داند؛ حتی اگر token ثبت شده ولی هیچ رسید میانی هنوز دیده نشده باشد. تا تعیین وضعیت ترب‌پی، ابزار بازیابی زیبال مجوز پذیرش رسید دیگری برای همان سفارش نمی‌دهد. این hold به fingerprint برنامهٔ بازیابی نیز متصل است تا تغییر هم‌زمانش مجوز قبلی را بی‌اعتبار کند.

استعلام پیش‌فرض فقط وضعیت می‌خواند و Payment/Order را تغییر نمی‌دهد:

```sh
uv run python manage.py reconcile_torobpay_payments --payment-id <PAYMENT_ID> --dry-run
```

اعمال reconciliation عادی، همان قواعد callback را دارد؛ نه force-primary، نه درخواست پرداخت تازه و نه refund تازه:

```sh
uv run python manage.py reconcile_torobpay_payments --apply --limit 100
```

پردازش دسته‌ای از پرداخت‌های بررسی‌نشده/کمتر بررسی‌شده شروع می‌شود؛ `reconciliation_attempted_at` مانع گرسنگی رسیدهای قدیمی در حضور checkout تازه یا صف بررسی دستی می‌شود. می‌توان بودجهٔ نرم را نیز محدود کرد:

```sh
scripts/compose-production exec -T web python manage.py reconcile_torobpay_payments --apply --limit 100 --max-runtime-seconds 45
scripts/compose-production exec -T web python manage.py expire_pending_orders --limit 100 --max-runtime-seconds 45
```

### زمان‌بندی اصلی تولید: timer سیستم‌دی

راهکار اصلی، `scripts/systemd/minikandoo-payments.timer` و service همراه آن است. timer در هر دقیقه فعال می‌شود؛ service از `scripts/run-payment-maintenance` و wrapper تولید موجود استفاده می‌کند و سرویس Docker تازه‌ای نمی‌سازد. کانتینر بکاپ، نقش و دسترسی متفاوتی دارد و برای اجرای قواعد پرداخت استفاده نمی‌شود. سه worker وب نیز scheduler پس‌زمینهٔ جداگانه اجرا نمی‌کنند.

اسکریپت در دایرکتوری واقعی پروژه، با تنظیمات صریح `config.settings.production` در `web` موجود، فرمان زیر را اجرا می‌کند:

```sh
python manage.py run_payment_maintenance --apply --limit 100 --max-runtime-seconds 45
```

این فرمان ابتدا `reconcile_torobpay_payments --apply` و سپس، در مرزهای پنج‌دقیقه‌ای UTC، `expire_pending_orders` را اجرا می‌کند. اولین tick و جبران وقفه نیز انقضا را بررسی می‌کنند. شکست استعلام مانع بررسی امن سایر سفارش‌ها نمی‌شود، ولی اجرای ناقص exit غیرصفر دارد. وجود بررسی دستی در نتیجه، به معنی failure زیرساخت نیست؛ شمارش آن و شناسه‌های مربوط در گزارش و warning هستند.

اجرای هم‌زمان service روی یک میزبان توسط نوع `oneshot` محدود می‌شود. ردیف مشترک `PaymentMaintenanceJob` در DB از هم‌پوشانی بین میزبان‌ها/کانتینرها نیز جلوگیری می‌کند؛ مالکیت آن پیش از هر مرحلهٔ HTTP بررسی و تمدید می‌شود. lease هر Payment محافظ مستقل است. UUID مالک قدیمی نمی‌تواند نتیجهٔ مالک جدید را بازنویسی کند. Redis، رمز درگاه یا قفل فایل میزبان تنها محافظ این کار نیستند.

بودجهٔ ۴۵ ثانیه‌ای، شروع تلاش تازه را محدود می‌کند و برای انقضای موعددار بخشی از بودجه کنار گذاشته می‌شود. تلاش جاری می‌تواند برای تکمیل امن مرحله بیشتر طول بکشد؛ `timeout` **داخل کانتینر** سقف سخت ۳۰۰ ثانیه و مهلت کشتن ۱۰ ثانیه دارد. سقف service میزبان ۳۳۰ ثانیه است. در timeout سخت، leaseهای پایدار به worker بعدی امکان استعلام و بازیابی می‌دهند. tickی که اجرای قبلی هنوز فعال باشد، اجرای موازی تازه نمی‌سازد.

### نصب روی سرور واقعی؛ فقط توسط مسئول استقرار

در این وظیفه هیچ timer روی سرور واقعی نصب یا فعال نشده است. پیش از نصب، Docker Engine و `docker.service`، Compose plugin، مسیر پایدار پروژه، ساعت همگام‌شدهٔ سرور، محیط خصوصی تولید و migration `payments/0006` را بررسی کنید. `timeout` در تصویر اجرایی Debian موجود است؛ با فرمان خواندنی `scripts/compose-production exec -T web timeout --version` آن را در تصویر مستقر کنترل کنید. فقط یک زمان‌بند برای این دو فرمان فعال باشد؛ Cron/timer قبلیِ همین کار را پس از شناسایی و بازبینی غیرفعال کنید، بدون تغییر jobهای بکاپ یا کارهای نامرتبط.

پس از اعمال نسخه و schema بررسی‌شده، از ریشهٔ واقعی پروژه:

```sh
sudo install -d -m 0750 /etc/minikandoo
sudo install -m 0644 scripts/systemd/minikandoo-payments.service /etc/systemd/system/minikandoo-payments.service
sudo install -m 0644 scripts/systemd/minikandoo-payments.timer /etc/systemd/system/minikandoo-payments.timer
sudo install -m 0644 scripts/systemd/payment-maintenance.conf.example /etc/minikandoo/payment-maintenance.conf
sudoedit /etc/minikandoo/payment-maintenance.conf
```

فقط مسیر واقعی را جایگزین کنید؛ فایل تنظیم timer هیچ secretی نمی‌خواهد:

```text
MINIKANDOO_PROJECT_DIR=/absolute/path/to/minikandoo
```

اگر wrapper از فایل محیط دیگری استفاده می‌کند، فقط مسیر خصوصی موجود را با `ENV_FILE` در همین فایل مشخص کنید. credentials از محیط همان کانتینر `web` خوانده می‌شوند؛ مقدارشان در آرگومان فرمان قرار نمی‌گیرد. فایل محیط واقعی را با فایل مثال جایگزین نکنید.

```sh
systemctl is-active docker.service
sudo systemd-analyze verify /etc/systemd/system/minikandoo-payments.service /etc/systemd/system/minikandoo-payments.timer
sudo systemctl daemon-reload
sudo systemctl enable --now minikandoo-payments.timer
sudo systemctl start minikandoo-payments.service
```

### مشاهده، شکست و راه‌اندازی مجدد

```sh
systemctl status minikandoo-payments.timer minikandoo-payments.service
systemctl list-timers --all minikandoo-payments.timer
journalctl -u minikandoo-payments.service --since '1 hour ago' --no-pager
scripts/compose-production exec -T web python manage.py run_payment_maintenance --status
```

`last_success_at`، `last_finished_at`، `last_error` و `last_expiration_at` در ردیف وظیفه و ادمین فقط‌خواندنی قابل مشاهده‌اند. موفقیت tick به معنی موفق‌بودن همهٔ پرداخت‌ها نیست؛ صف باقی‌مانده و شمارش بررسی دستی را در `summary` ببینید. کاربر پشتیبانی برای نمایش این ردیف به مجوز `payments.view_paymentmaintenancejob` نیاز دارد. استعلام status پیش‌فرض، هیچ ردیف یا پرداختی نمی‌سازد.

service از نوع `oneshot` است و inactive بودن آن بین tickها طبیعی است؛ timer باید active، موعد بعدی مشخص و نتیجهٔ اجرای قبلی موفق باشد. ذخیرهٔ فرم ادمین برای Payment و وضعیت وظیفه مجاز نیست تا نمونهٔ کهنه، شواهد یا lease تازه را بازنویسی نکند. عملیات مالی همچنان فقط از سرویس‌ها و فرمان‌های مجاز انجام می‌شود.

در قطعی API، Payment نامعلوم FAILED نمی‌شود، error پالایش‌شده و شناسه‌های محلی ثبت می‌شوند و command خروج غیرصفر دارد. timer در tick بعدی دوباره تلاش می‌کند؛ بعد از restart نیز `Persistent=true` یک اجرای جبرانی می‌دهد. در کرش، پس از پایان lease، مالک بعدی status را پیش از هر تصمیم می‌خواند. ابتدا مشکل واقعی را رفع و لاگ را بررسی کنید، سپس:

```sh
sudo systemctl reset-failed minikandoo-payments.service
sudo systemctl restart minikandoo-payments.timer
sudo systemctl start minikandoo-payments.service
```

برای به‌روزرسانی، timer را متوقف کنید و پایان اجرای service جاری را بررسی کنید؛ سپس طبق راهنمای استقرار، بکاپ و توقف writerها انجام شود. plan و migration فقط در پنجرهٔ مجاز و با تصویر بازبینی‌شده، یک بار اجرا شوند:

```sh
sudo systemctl stop minikandoo-payments.timer
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --plan
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --noinput
```

این فرمان‌های تولید دستورالعمل مسئول سرورند و در این پیاده‌سازی اجرا نشده‌اند. پس از rollout، سلامت وب، نتیجهٔ نخستین tick، موعد اجرای بعدی و آخرین موفقیت انقضا را واقعاً کنترل کنید؛ گذشتن تست‌ها به‌تنهایی تأیید آماده‌بودن سرور نیست.

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

`payments/0006_settlement_confirmation_and_maintenance` سه ستون nullable زمان مشاهده/lease/آخرین تلاش، index صف و ردیف وضعیت وظیفه را اضافه می‌کند. هیچ زمان تاریخی از callback، ایجاد پرداخت یا timestamp قدیمی DB حدس زده نمی‌شود و رکوردهای مالی زیبال تغییر نمی‌کنند. برگشت schema پس از وجود شواهد ترب‌پی یا وظیفه مسدود است؛ rollback باید با بکاپ و طرح بازبینی‌شده انجام شود.

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

نتیجهٔ بررسی سخت‌سازی در ۲۱ نوامبر ۲۰۲۶، بر پایهٔ آخرین `origin/main` یعنی `0176334`:

| بررسی | نتیجهٔ واقعی |
| --- | --- |
| خط مبنای نسخهٔ دریافت‌شده، SQLite | ۶۷۹ آزمون، موفق؛ ۲۷ مورد محدودیت قفل |
| مجموعهٔ کامل نهایی با `shop_test_settings` و SQLite | ۷۱۸ مورد؛ ۶۸۸ موفق و ۳۰ مورد محدودیت قفل |
| همان مجموعهٔ نهایی روی MariaDB 11.4.13 ایزوله | هر ۷۱۸ آزمون موفق؛ بدون مورد کنارگذاشته‌شده |
| آزمون متمرکز نهایی مشاهده، زمان‌بندی، هم‌زمانی و migration روی MariaDB | هر ۴۴ آزمون موفق |
| `manage.py check` با تنظیمات QA | بدون ایراد |
| `makemigrations --check --dry-run` با تنظیمات QA | بدون تغییر اعلام‌نشدهٔ مدل |
| `npm run build` | موفق؛ CSS و فایل‌های vendor ساخته شدند |
| `git diff --check` | موفق |
| اعتبارسنجی آفلاین service/timer | موفق؛ فایل‌های همسان با مخزن با مجوز ۶۴۴ و وابستگی ساختگی Docker بررسی شدند؛ واحد واقعی نصب/اجرا نشد |

برای این بررسی از `.venv/bin/python` استفاده شد؛ فرمان نهایی MariaDB، `manage.py test --settings=settings_settlement_mariadb --noinput --verbosity 2` با ماژول QA و سوکت خصوصی بود. تنظیمات/داده‌های QA جایگزین محیط تولید نیستند. آزمون‌های بازتولیدِ تأخیر ثبت/مرز مهلت و تعارض شناسه ابتدا مشکل را نشان دادند؛ ناسازگاری snapshot در اجرای کامل نیز رفع شد. نتیجهٔ نهایی بدون شکست است. هیچ وابستگی تازه‌ای اضافه نشد و هیچ خرید یا بازپرداخت زنده آزمایش نشد.
