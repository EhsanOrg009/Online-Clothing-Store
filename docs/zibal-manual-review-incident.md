# گزارش بررسی پرداخت زیبال؛ تراکنش ۷، سفارش ۳

تاریخ تکمیل بررسی محلی: ۲۰۲۶-۱۱-۱۷. علت مستقیم ثبت‌شدهٔ بررسی دستی، زمان پرداخت بدون offset و بدون timezone رسماً تأییدشده است. رسید موفق درگاه حفظ شده، اما شواهد موجود برای پذیرش سفارش بر اساس deadline کافی نیستند. اصلاح محلی وضعیت مشتری و ادمین را هماهنگ می‌کند و کنترل‌های مالی را حفظ می‌کند؛ تراکنش واقعی هنوز در این کار بازیابی نشده است.

## وضعیت مخزن و حفظ کار قبلی

بررسی اولیه شامل `pwd`، وضعیت و diff کامل معمولی و staged، پانزده commit اخیر، همهٔ branchها، stashها و worktreeها بود. branch محلی `main` و مبنای آن `ed76e3a` بود؛ مرجع محلی `origin/main` نیز همان commit را نشان می‌داد. commit پیشین سخت‌سازی `bf9d96b` و سابقهٔ recovery و پرداخت نیز بررسی شدند. staged change، stash، branch یا worktree دیگری وجود نداشت.

در آغاز دو فایل `payments/services.py` و `templates/partials/status_badge.html` از قبل تغییر داشتند: هماهنگی بررسی دستی سفارش، حفاظت در برابر تلاش قدیمی و نشان بررسی دستی. این منطق معتبر حفظ و تکمیل شد. بازنویسی مخرب، reset، clean، stash کور یا amend انجام نشد. تغییرات نهایی پیاده‌سازی، آزمون و سند در commit جدید محلی ثبت می‌شوند؛ SHA و وضعیت نهایی Git در تحویل گفتگو اعلام می‌شوند.

## شواهد و تشخیص

موارد زیر از گزارش read-only و پالایش‌شدهٔ مالک هستند؛ این بررسی به دیتابیس production متصل نشده است:

| شاهد | مقدار گزارش‌شده | نتیجه |
| --- | --- | --- |
| `Payment.status` / `creation_status` | `manual_review` / `ready` | جلسه ایجاد و رسید برای بررسی حفظ شده است |
| authority و request UUID | موجود | Payment موجود باید حفظ شود؛ درخواست جدید لازم نیست |
| `paid_at` / `verified_at` | موجود | ثبت محلی رسید و نتیجهٔ بررسی؛ اثبات زمان درگاه نیست |
| `gateway_paid_at` | `NULL` | زمان درگاه به لحظهٔ قابل اتکا تبدیل نشده است |
| `needs_refund` | `False` | بررسی دستی، تصمیم refund نیست |
| `verification.result` / `verify_result` / `status` | `100` / `100` / `1` | پاسخ مستقیم Verify موفق؛ در مسیر فعلی Inquiry لازم نبوده است |
| `verification.paidAt` | `2026-11-16T18:03:02.136000` | timestamp بدون offset |
| `payment_time.reason` / `decision.reason` | `timezone_unconfirmed` / `timezone_unconfirmed` | علت مستقیم ثبت‌شدهٔ بررسی دستی |
| `payment_time.source` / `timezone` | خالی / خالی | منبع زمانی تأییدشده‌ای استفاده نشده است |
| مبلغ ریالی، order ID و مرجع قبلی | منطبق، طبق گزارش مالک | این سه تطبیق علت گزارش‌شدهٔ بررسی دستی نیستند |
| تطبیق `trackId` | «MISMATCH»؛ وجود کلید و نوع هنوز نامعلوم | اختلاف واقعی هنوز اثبات نشده است |

مسیر کد: `_verified_result` پس از کنترل مبلغ، سفارش، track ID در صورت حضور، وضعیت و مرجع، `parse_gateway_paid_at` را اجرا می‌کند. timestamp بدون offset با تنظیم خالی `timezone_unconfirmed` می‌دهد؛ `_apply_verified_callback` از `classify_receipt` تصمیم بررسی دستی می‌گیرد، `paid_at` اولیه و verification را ثبت و `gateway_paid_at` را خالی نگه می‌دارد. `mark_order_paid` اجرا نمی‌شود. این رفتار برای شواهد گزارش‌شده حفاظت درست مالی است.

`Payment.authority` همان track ID زیبال به شکل رشته است. schema رسمی Verify/Inquiry این کلید را در پاسخ ندارد؛ مقدار مفقود با authority قابل مقایسه نیست و عدد JSON نیز بدون لحاظ نوع با رشته برابر نخواهد شد. اگر کلید حاضر باشد، کد عدد صحیح معتبر را با authority تطبیق می‌دهد و اختلاف واقعی را پیش از پذیرش رد می‌کند. به همین دلیل نتیجهٔ اسکریپت قبلی برای بستن این ابهام کافی نیست. command جدید `inspect_zibal_payment --payment-id 7`، فقط وجود کلید، نوع و تطبیق را بدون چاپ شناسه‌ها گزارش می‌کند. این command روی production اجرا نشده است.

علت ثبت‌شده معلوم است، اما وضعیت فعلی deadline، رزرو و کل ledger سفارش مستقیماً در این محیط بررسی نشده است. تأیید timezone به‌تنهایی اثبات قابل‌پذیرش‌بودن سفارش نیست. تعارض واقعی track/reference یا علت تاریخی مستقل باید پیش از بازیابی رفع شود.

## مستندات رسمی و سیاست timezone

در ۲۰۲۶-۱۱-۱۷ [IPG رسمی](https://help.zibal.ir/ipg/) و [OpenAPI رسمی](https://api.zibal.ir/static/helpdocs/ipg.json) دوباره دریافت شدند. صفحهٔ IPG رابط نمایش مستندات است؛ قرارداد در OpenAPI بررسی شد: `3.1.1`، نسخهٔ `1.0.0`، با SHA256 زیر، یکسان با بررسی قبلی:

```text
0231758f40572777e98b12107926363dcf84a9af120c5485b5e3209a9a31a3e2
```

endpointهای request/start/verify/inquiry تغییر نکرده‌اند. مبلغ ریالی، `trackId` مثبت int64، callback GET و semantics نتیجه‌های ۱۰۰/۲۰۱/۲۰۲ بررسی شدند؛ HTTP 200 یا callbackِ مدعی موفقیت، مجوز fulfillment نیست. UUID محلی کلید idempotency مستند زیبال نیست و retry کور ایجاد پرداخت اضافه نشده است.

`paidAt` در هر دو پاسخ Verify/Inquiry `string/date-time/ISODate` است؛ نمونه‌های رسمی بدون offset هستند. تضمین UTC، `Asia/Tehran` یا timezone دیگری در منابع عمومی بررسی‌شده پیدا نشد. روش درخواست offset صریح نیز مستند نشده است؛ وجود قالب date-time چنین تضمینی نمی‌دهد. بنابراین `ZIBAL_PAID_AT_TIMEZONE` خالی می‌ماند. تنظیمات production یا محلی timezone تغییر نکرده‌اند. نام `Asia/Tehran` فقط در آزمون مصنوعی استفاده شده و اثبات timezone واقعی زیبال نیست.

پرسش دقیق دربارهٔ نام IANA، یکسان‌بودن قرارداد Verify/Inquiry، پاسخ دارای offset، تاریخچه و DST در [راهنمای بازیابی](zibal-verification-recovery.md) نوشته شده است. ارسال پرسش به پشتیبانی زیبال بر عهدهٔ پذیرنده است و در این کار انجام نشده است.

## اختلاف وضعیت نمایشی

در کد پیشین، view واقعی `orders:detail`، قالب `orders/receipt.html` را با warning مربوط به رسید دستی نشان می‌داد؛ همان قالب badgeها را از `order.payment_status=pending` و وضعیت چرخهٔ عمر سفارش می‌ساخت. جدول پرداخت در همان زمان می‌توانست `manual_review` باشد و badge این وضعیت را کامل پوشش نمی‌داد. ادمین سفارش نیز وضعیت خام سفارش را نشان می‌داد. این منشأ اختلاف warning بررسی دستی با pending در سورس اثبات شد.

منشأ نمایش **اولیهٔ موفقیت نهایی در production** از شواهد در دسترس اثبات نشده است؛ trace درخواست، متن دقیق پیام و نسخهٔ اجراشده مستقیماً بررسی نشده‌اند. در سورس بررسی‌شده callback بررسی دستی warning می‌دهد و به رسید سفارش می‌رود؛ پیام داخلی موفقیت adapter به معنای سفارش paid نیست. آزمون جدید این redirect و سطح warning را کنترل می‌کند و URLهای نتیجهٔ قدیمی نیز برای سفارش unpaid دارای بررسی دستی به رسید هدایت می‌شوند. نباید ادعا کرد callback واقعی production در این کار بازسازی یا پیام اولیه قطعاً شناسایی شده است.

## تغییرات پیاده‌سازی

- `payments/services.py`: هماهنگی `Order.payment_status=manual_review` زیر قفل Order-first، حفاظت در برابر پاسخ تلاش قدیمی، آرشیو `decision_history` و مؤثرماندن علت‌های تاریخی زمان آینده/تعارض. فقط تصمیم معتبر مالی می‌تواند آخرین حفاظت بررسی دستی را رفع کند؛ پاسخ failed به‌تنهایی کافی نیست.
- `orders/models.py` و `orders/choices.py`: وضعیت نمایشی با بررسی همهٔ رسیدهای دستی، پیام فارسی مشترک و منع `can_pay`. `Order.is_paid` تنها با وضعیت مالی موفق true است. annotation نمایش در سرویس مالی معتبر فرض نمی‌شود؛ Order تازه زیر قفل دوباره خوانده می‌شود.
- `orders/services.py`: بازبینی وضعیت بررسی دستی سفارش پس از قفل در انقضا؛ رزرو خودکار آزاد نمی‌شود. پذیرش معتبر موجودی را دوباره کم نمی‌کند؛ refund resolution نیز خودش stock را آزاد نمی‌کند.
- `payments/views.py` و `orders/views.py`: redirect صادقانه، منع پرداخت مجدد و بررسی تمام رسیدها در نمایش، شامل رکوردهای legacy و receipt قدیمی‌تر از پنج تلاش اخیر.
- `templates/orders/receipt.html`، `history.html`، `order_detail.html`، `order_list.html` و `partials/status_badge.html`: نشان «نیازمند بررسی پشتیبانی»، پیام منع پرداخت تازه و تفکیک تأیید درگاه از پذیرش سفارش؛ بدون اعلام شروع آماده‌سازی.
- `orders/admin.py`، `payments/admin.py`، `common/dashboard.py` و `templates/admin/dashboard.html`: عنوان، فیلتر و آمار هماهنگ؛ review در pending و فروش موفق محاسبه نمی‌شود. سفارش واقعاً paid با رسید اضافهٔ مبهم تنزل نمی‌یابد.
- `payments/gateways/zibal.py`: استخراج validator مرجع برای اشتراک با command خواندنی؛ پذیرش مرجع عددی/رشته‌ای و حفظ صفرهای ابتدایی تغییر نکرده است. parser زمان و قرارداد اصلی gateway دور زده نشده‌اند.
- `payments/management/commands/reconcile_zibal_payments.py`: تاریخچهٔ تعارض/زمان آینده نیز انتخاب primary را مسدود می‌کند. command جدید `inspect_zibal_payment.py` فقط شواهد پالایش‌شدهٔ ذخیره‌شده را می‌خواند؛ gateway call و DB write ندارد.
- `payments/tests.py`، `payments/test_timestamps.py` و `orders/tests.py`: رگرسیون‌های مالی، زمان، recovery، UI/admin و هم‌زمانی تکمیل شده‌اند. دو راهنمای زیبال به‌روز و مثال‌های عملیاتی قدیمی با placeholder جایگزین شدند.

schema تازه‌ای لازم نشد. migrationهای موجود orders/0002 و payments/0003 و payments/0004 در suite کامل، روی دیتابیس‌های تست، بررسی شدند؛ طبق گزارش مالک در production قبلاً اعمال شده‌اند. هیچ migration تولیدی در این کار اجرا نشده است.

## آزمون‌ها و حدود اعتبار

نتایج نهایی روی همین تغییرات:

| محیط و اجرا | کل | قبول | شکست/خطا | ردشده |
| --- | ---: | ---: | ---: | ---: |
| SQLite، متمرکز، ۲۰۲۶-۱۱-۱۶ | ۲۳۲ | ۲۱۰ | ۰ | ۲۲ |
| SQLite، کامل، ۲۰۲۶-۱۱-۱۶ | ۵۳۹ | ۵۱۷ | ۰ | ۲۲ |
| MariaDB 11.4.13 موقت، متمرکز، ۲۰۲۶-۱۱-۱۷ | ۲۳۲ | ۲۳۲ | ۰ | ۰ |
| MariaDB 11.4.13 موقت، کامل، ۲۰۲۶-۱۱-۱۷ | ۵۳۹ | ۵۳۹ | ۰ | ۰ |

هر ۲۲ skipِ SQLite به `has_select_for_update` مربوط‌اند؛ همه در MariaDB اجرا و موفق شدند. آزمون‌ها offset صریح، زمان بدون offset با تنظیم خالی، timezone مصنوعی، DST fold/gap، offset نامعلوم/نامعتبر، زمان آینده، amount/order/track/reference نامنطبق، callback تکراری/خراب، Verify/Inquiry، سوابق متعارض، backup/checksum recovery و وضعیت مشتری/ادمین را پوشش می‌دهند. قفل و raceهای callback، پرداخت ایجادشده، انقضا و لغو روی MariaDB اجرا شدند؛ SQLite اثبات قفل ردیفی نیست.

دستورهای SQLite:

```sh
PYTHONPATH=scripts/qa .venv/bin/python manage.py test payments.tests payments.test_timestamps --settings=shop_test_settings --noinput --verbosity 2
PYTHONPATH=scripts/qa .venv/bin/python manage.py test --settings=shop_test_settings --noinput --verbosity 2
```

دستورهای دقیق آخرین اجرای MariaDB؛ مسیر `/tmp` و module تنظیمات زیر موقت‌اند و در Git ثبت نمی‌شوند:

```sh
PYTHONPATH='/tmp/minikandoo-zibal-final-mariadb-3jk9mi_e:scripts/qa' .venv/bin/python manage.py test payments.tests payments.test_timestamps --settings=minikandoo_zibal_final_mariadb_settings --noinput --verbosity 2 > /tmp/minikandoo-zibal-focused-final-mariadb-20261117.log 2>&1
PYTHONPATH='/tmp/minikandoo-zibal-final-mariadb-3jk9mi_e:scripts/qa' .venv/bin/python manage.py test --settings=minikandoo_zibal_final_mariadb_settings --noinput --verbosity 2 > /tmp/minikandoo-zibal-full-final-mariadb-20261117.log 2>&1
```

این MariaDB با datadir تازه، `--no-defaults`، `--skip-networking` و Unix socket خصوصی در `/tmp` ساخته شد. تنظیمات QA، credential و دیتابیس production را استفاده نمی‌کنند. جدول‌های timezone سیستم تنها در همین محیط موقت بارگذاری شدند تا `date_hierarchy` ادمین آزمودنی باشد.

در اجرای مقدماتیِ مرحلهٔ قبلی MariaDB، ۲۳۰ آزمون از ۲۳۱ قبول و یک آزمون ادمین به علت نبود جدول timezone خطا داشت؛ آزمون‌های قفل گذشته بودند. پس از وقفه، اجرای مجدد با تنظیمات موقت حذف‌شده به `ModuleNotFoundError` خورد و هیچ آزمونی اجرا نکرد. محیط تازه این دو مانع را رفع کرد؛ نتایج نهایی بالا صفر شکست دارند. درخواست‌های اولیهٔ دانلود مستندات و bind/اتصال socket داخل sandbox نیز رد شدند؛ فقط مستندات عمومی و دیتابیس موقت پس از مجوز محیط اجرا شدند.

بررسی‌های تکمیلی:

```sh
PYTHONPATH=scripts/qa .venv/bin/python manage.py check --settings=shop_test_settings
PYTHONPATH=scripts/qa .venv/bin/python manage.py makemigrations --check --dry-run --settings=shop_test_settings
git diff --check
```

`check`: صفر مسئله؛ `makemigrations`: بدون تغییر؛ بررسی whitespace: موفق. تمام پاسخ‌های زیبال در آزمون‌ها mock هستند. Verify/Inquiry زنده، تراکنش ۷ در production، restore یک backup واقعی production و استقرار این نسخه در این کار آزموده نشده‌اند. موفقیت suite، اثبات پرداخت زنده نیست.

## بازیابی رسید قبلی و کار باقی‌مانده

ابتدا ابهام وجود/نوع track ID با command شواهد ذخیره‌شده رفع و تأیید کتبی timezone از زیبال گرفته شود. بدون آن، receipt در بررسی دستی باقی می‌ماند. پس از تأیید، فقط workflow موجود `reconcile_zibal_payments` با authority و Payment موجود، کل ledger همان سفارش، بکاپ تازهٔ معتبر و checksum ثبت‌شده مجاز به reverify است. جزئیات و placeholderهای امن در [راهنمای بازیابی](zibal-verification-recovery.md) آمده‌اند.

تلاش `ready` و دارای authority برای بازیابی نیاز به `--request-id` یا `/v1/request` تازه ندارد. اگر زمان معتبر، deadline، رزرو و هویت رسید سازگار باشند، مسیر پذیرفته‌شده سفارش را دقیقاً یک بار paid می‌کند؛ لغو یا آزادسازی قبلی موجودی احیا نمی‌شود. `needs_refund=False` فعلی با حدس به True تبدیل نمی‌شود. Payment ID، order ID، مبلغ، authority، UUID، مرجع، `paid_at` اولیه، raw timestamp و history حفظ می‌شوند.

دادهٔ production در این کار حذف یا تغییر نکرده است؛ هیچ درخواست واقعی پرداخت، refund، بازیابی، migration، deploy یا push انجام نشده است. فایل محیط، credential، backup، log یا export خصوصی به تغییرات Git اضافه نشده‌اند. تصمیم و عملیات بعدی نیازمند پاسخ پشتیبانی زیبال، بررسی مجاز شواهد تولیدی و اجازهٔ مستقل عملیات production هستند.
