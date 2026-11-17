# بازیابی امن پرداخت زیبال

این runbook بازیابی با حفظ رسید و شواهد قبلی را توضیح می‌دهد. کد و آزمون‌ها محلی آماده شده‌اند؛ اجرای فرمان‌های production، migration، recovery یا refund در این کار انجام نشده است. [قرارداد پرداخت و timezone](zibal-payment.md)، [گزارش تراکنش ۷](zibal-manual-review-incident.md) و [عملیات بکاپ Docker](production-database-backups.md) را پیش از اجرا بخوانید.

## تراکنش ۷، سفارش ۳: علت ثبت‌شده و مسیر بعدی

شواهد پالایش‌شدهٔ گزارش read-only مالک برای این حادثه: Verify با `result=100` و `verify_result=100`، وضعیت درگاه `1` و `paidAt=2026-11-16T18:03:02.136000`. زمان بدون offset است؛ `metadata.verification.payment_time.reason` و `metadata.decision.reason` برابر `timezone_unconfirmed` هستند، source/timezone خالی‌اند و `gateway_paid_at=NULL` است. رسید `manual_review`، ایجاد `ready` و `needs_refund=False` است؛ مبلغ ریالی، order ID و مرجع قبلی مطابق گزارش مالک تطبیق دارند. این شواهد از production توسط این کار محلی دوباره خوانده نشده‌اند.

این رسید نباید با تلاش failed/pending عادی یا حادثهٔ قدیمی شمارهٔ مرجع اشتباه گرفته شود. `paid_at` و `verified_at` زمان‌های محلی ثبت/بررسی‌اند و timezone درگاه را اثبات نمی‌کنند. در مسیر فعلی، `verify_result=100` پاسخ مستقیم Verify را نشان می‌دهد؛ Inquiry برای نتیجهٔ ۲۰۱/۲۰۲ لازم است. علت مستقیم ثبت‌شدهٔ بررسی دستی معلوم است، اما پذیرش بعدی همچنان deadline، رزرو، وضعیت سفارش، همهٔ رسیدهای همان سفارش و هویت رسید را بازبینی می‌کند.

مقایسهٔ قبلی `trackId` با `authority` به‌صورت «MISMATCH» گزارش شد؛ وجود کلید و نوع مقدار هنوز مشخص نشده است. `authority` همان track ID زیبال با نوع رشته است. schema پاسخ Verify/Inquiry الزاماً این کلید را برنمی‌گرداند؛ نبود کلید یا مقایسهٔ عدد JSON با رشته اختلاف واقعی را ثابت نمی‌کند. اگر کلید حاضر و واقعاً متفاوت باشد، بازیابی متوقف می‌شود؛ authority نباید برای تطبیق بازنویسی شود.

برای بررسی خصوصی **فقط شواهد ذخیره‌شده**، در محیط مجازِ دارای نسخهٔ جدید:

```sh
python manage.py inspect_zibal_payment --payment-id 7
```

این command هیچ تماس درگاه یا DB write ندارد؛ شناسهٔ پذیرنده، کارت، مشتری، authority، UUID و payload کامل را چاپ نمی‌کند. تنها وجود/نوع `trackId`، تطبیق عددی و دلایل whitelisted گزارش می‌شوند:

| `verification.track_id_comparison` | تفسیر |
| --- | --- |
| `not_returned` | کلید در پاسخ ذخیره‌شده نیست؛ اختلاف شناسه محسوب نمی‌شود |
| `matched` | شناسهٔ معتبر، پس از لحاظ نوع عدد/رشته، همان authority است |
| `mismatch` | اختلاف واقعی؛ بدون تغییر شناسه یا پذیرش، بررسی مستقل لازم است |
| `invalid` | کلید حاضر ولی مقدار یا authority نامعتبر است؛ بررسی مستقل لازم است |

تا زمان پاسخ رسمی، تنظیم `ZIBAL_PAID_AT_TIMEZONE` خالی، رسید در بررسی دستی و پرداخت تازه ممنوع می‌ماند. متن پیشنهادی برای ارسال **توسط پذیرنده** به پشتیبانی زیبال:

> در پاسخ productionِ `/v1/verify` برای یک پرداخت تأییدشده، `paidAt` به صورت `2026-11-16T18:03:02.136000` و بدون `Z` یا offset دریافت شده است. timezone دقیق این مقادیر چیست و نام IANA تضمین‌شدهٔ آن کدام است؟ آیا `/v1/inquiry` دقیقاً همان قرارداد زمانی را دارد؟ آیا این تضمین برای پاسخ‌های تاریخی و تغییرات DST نیز برقرار است؟ آیا می‌توان پاسخ RFC3339 دارای `Z` یا offset صریح دریافت کرد و روش درخواست آن چیست؟ لطفاً پاسخ کتبی یا لینک قرارداد رسمی را ارائه کنید؛ حدس بر اساس محل پذیرنده یا timezone سرور برای کنترل deadline پرداخت کافی نیست.

ارسال پرسش، تماس با زیبال و هیچ Inquiry/Verify واقعی در این کار انجام نشده‌اند. `Asia/Tehran` در آزمون‌ها صرفاً تأیید مصنوعی است.

پس از رفع ابهام شناسه و دریافت پاسخ رسمی، مسئول production این مسیر را جداگانه بازبینی می‌کند:

1. شمارهٔ واقعی `Order.order_number` برای Order ID 3 و authority **موجود** Payment ID 7 را خصوصی بردارد. اگر رسید بالقوه موفق دیگری برای همان سفارش هست، همه را بررسی و صریحاً انتخاب کند؛ رسیدهای سفارش قدیمی به این پرونده اضافه نشوند.
2. منبع، تاریخ و محدودهٔ تضمین timezone را ثبت کند. حالت بدون `--apply` فرمان `reconcile_zibal_payments`، برخلاف command شواهد ذخیره‌شده، **Inquiry واقعی** انجام می‌دهد و عملیات جداگانهٔ محیط مجاز است.
3. قبل از apply، بکاپ SQL تازهٔ حداکثر یک‌ساعته، sidecar SHA256 ثبت‌شده، صحت کامل gzip/SQL و قفل حفاظت بکاپ را طبق بخش پایین بررسی کند. checksum تازهٔ بدون sidecar قبلی یا بکاپ کهنه کافی نیست.
4. با همان track ID و همهٔ کنترل‌های فرمان، reverify کند. نمونهٔ زیر placeholder دارد و دستور اجرایی آمادهٔ production نیست:

   ```sh
   python manage.py reconcile_zibal_payments \
     --order-number ORDER_NUMBER_FOR_ID_3 \
     --track-id EXISTING_TRACK_ID_FOR_PAYMENT_7 \
     --gateway-timezone OFFICIALLY_CONFIRMED_IANA_ZONE
   # فقط پس از بازبینی Inquiry و بکاپ معتبر، همان فرمان با این دو گزینه:
   # --apply --backup /READ_ONLY_BACKUP_PATH/FRESH_BACKUP.sql.gz
   ```

5. نتیجهٔ replay، `actual_primary`، تصمیم ثبت‌شده، `needs_refund`، وضعیت سفارش، رزرو و audit را بررسی کند. فقط اگر receipt و وضعیت فعلی سفارش معتبر باشند، پذیرش یک بار انجام می‌شود؛ لغو قبلی، موجودی آزادشده یا deadline نامنطبق با تنظیم timezone دور زده نمی‌شوند.

برای این رسید با `creation_status=ready` و authority موجود، اتصال دوباره با `--request-id`، تعویض Payment، درخواست `/v1/request` تازه یا تغییر مستقیم SQL لازم و مجاز نیست. Payment ID، مبلغ، authority، UUID، مرجع، `paid_at` اولیه، raw `paidAt` و تاریخچهٔ verification/decision/recovery حفظ می‌شوند. بررسی دستی به‌تنهایی دلیل refund نیست.

## سابقهٔ حادثهٔ قدیمی شمارهٔ مرجع

اعتبارسنجی قدیمی `refNumber` تنها integer را قبول می‌کرد. numeric string واقعی باعث خطای «شماره مرجع گزارش زیبال نامعتبر است.» و rollback محلی callback می‌شد. اصلاح رشتهٔ ASCII مثبت و حفظ صفرهای ابتدایی باقی مانده است. بازبینی بعدی دو خطر مستقل را پوشش می‌دهد: مقایسهٔ زمان ورود callback با deadline، و آزادسازی موجودی همزمان با تأیید پرداخت.

مالک پیش‌تر سه رسید موفقِ منطبق برای یک سفارش قدیمی، با reference رشته‌ای معتبر و `paidAt` موجود گزارش کرده بود. این پرونده متعلق به Payment 7 / Order 3 نیست و استعلام تازه‌ای برای آن انجام نشده است. شناسه‌ها از مثال‌های اجرایی حذف شده‌اند تا بازیابی سفارش جدید با پروندهٔ قبلی مخلوط نشود. صرف وجود `paidAt` به معنای timezone مشخص نیست؛ چند timestamp بدون offset نیز با مرتب‌سازی روی «ساعت API» قابل پذیرش نیستند.

## حالت پیش‌فرض: Inquiry بدون تغییر دیتابیس

پس از نصب نسخهٔ جدید و اعمال migrationهای بررسی‌شده، در ریشهٔ پروژه:

```sh
scripts/compose-production exec -T web python manage.py reconcile_zibal_payments \
  --order-number ORDER_NUMBER \
  --track-id EXISTING_TRACK_ID
```

فرمان بدون `--apply` فقط Query محلی و `/v1/inquiry` خواندنی انجام می‌دهد. Verify، ساخت payment، تغییر callback count، تغییر موجودی یا refund ندارد. تمام تلاش‌های پرداخت همان سفارش بررسی می‌شوند؛ شناسه‌های انتخابی باید صریح، یکتا، معتبر و متعلق به همان سفارش باشند.

گزارش JSON شامل موارد زیر است:

- `before` و `after`: وضعیت مالی سفارش/پرداخت‌ها، deadline، زمان‌ها و snapshot موجودی؛ در inspect بدون تغییرند.
- `gateway`: مبلغ ریالی و تطبیق تومان، order/track/reference، زمان خام قابل نمایش، زمان UTC و تفسیر timestamp.
- `ambiguous_timestamps`، `duplicate_successful_payments` و action/reason هر رسید.
- `blocking_issues`، `gateway_time_order` و `planned_primary`؛ فقدان primary مجوز پذیرش همهٔ رسیدها نیست.
- `manual_review_required`، `refund_required` و `actual_primary`: وضعیت ثبت‌شدهٔ محلی، مستقل از preview Inquiry.

raw timestamp خراب که احتمالاً متن دلخواه یا object باشد در console چاپ نمی‌شود؛ مقدار اصلی در audit خصوصی پس از verification حفظ می‌شود. اطلاعات مشتری، کارت، merchant و پاسخ دلخواه API وارد گزارش نمی‌شوند. گزارش‌های مالی را خصوصی و خارج از Git نگه دارید.

## انتخاب primary و رفتار apply

برای سفارش فعال و رزرو آزاد‌نشده، تنها رسید دارای زمان معتبر درون مهلت قابل پذیرش است. primary بر اساس یکتابودن زودترین `gateway_paid_at` معتبر تعیین می‌شود؛ تساوی زمان درگاه با زمان ایجاد یا PK محلی شکسته نمی‌شود و نیازمند بررسی است. ترتیب track ID و زمان callback ملاک نیستند. موارد مبهم مانع انتخاب خودکار primary هستند. یک سفارش پرداخت‌شده باید دقیقاً یک primary محلی داشته باشد که با Inquiry مستقل قابل اثبات باشد.

تاریخچهٔ نامعتبر، رسید تاریخی از درگاه دیگر، ledger ناسازگار و رسید تأییدشدهٔ دیگری که صریحاً انتخاب نشده است، application را مسدود می‌کنند. primary ثبت‌شده به‌طور خودکار عوض نمی‌شود. فقط track IDهای انتخابی replay می‌شوند.

در apply، هر رسید مجدداً از مسیر `process_callback`، Verify و در صورت نیاز Inquiry بررسی می‌شود. هر replay تراکنش اتمیک مستقل دارد. قفل سفارش، پرداخت و واریانت‌ها eligibility فعلی را دوباره کنترل می‌کند. انقضای محلی پرداخت، مانع دریافت شواهد موفق درگاه نیست؛ سفارش لغوشده یا رزرو آزادشده هرگز احیا نمی‌شود.

اگر replay primary timeout یا شکست بخورد، سایر رسیدهای زودهنگام به‌عنوان primary جایگزین نمی‌شوند؛ شواهد موفقشان در manual review می‌ماند. retry معتبر همان primary را یک بار نهایی و رسیدهای اضافه را برای refund handling علامت می‌زند. اگر بین preview و replay timestamp مبهم شود یا انقضا زودتر قفل بگیرد، fulfillment متوقف می‌شود. نتیجهٔ جزئی یا نیازمند review با گزارش روشن و exit غیرصفر برمی‌گردد؛ تکرار فرمان موفقیت‌های قبلی را دوباره اعمال نمی‌کند.

`metadata.recovery_history` شامل منبع فرمان، SHA256 بکاپ، نام بکاپ، شناسه‌های انتخابی، primary برنامه‌ریزی‌شده و زمان مشاهده است. `verification_history` و raw `paidAt` حفظ می‌شوند؛ `decision_history` نیز تصمیم قبلی را قبل از تغییر `decision` آرشیو می‌کند. علت تعارض یا زمان آینده در تاریخچه، حتی اگر آخرین تصمیم فقط timezone review باشد، انتخاب primary و پذیرش خودکار را متوقف می‌کند. callback تکراری رسیدی که قبلاً successful شده فقط شمارنده را افزایش می‌دهد و رسید/fulfillment را بازنویسی نمی‌کند.

## رسیدهای حل‌نشده و مجوز محدود primary

کل history سفارش بررسی می‌شود: `MANUAL_REVIEW`، `paid_at`/`gateway_paid_at`، verification موفقِ فعلی یا آرشیوشده در `verification_history` حتی با status غیرنهایی، درگاه تاریخی و receiptهای خارج از `--track-id`. پاسخ ناموفق تازه شواهد موفق قدیمی را پاک نمی‌کند. برای primary تازهٔ سفارش هنوز پرداخت‌نشده، تمام رسیدهای بالقوه موفق باید صریحاً انتخاب شوند. گزارش بخش‌های مستقل `existing_manual_review_receipts`، `unselected_manual_review_receipts`، `unselected_potentially_successful_receipts`، `potentially_successful_receipts`، `confirmed_successful_receipts`، `incomplete_inquiries`، `ambiguous_timestamps` و `primary_selection_blockers` دارد.

رسید دستیِ انتخاب‌نشده یا رسید بالقوه موفقِ بدون Inquiry معتبر، انتخاب primary و apply را مسدود می‌کند. timestamp مبهم، خطای selected Inquiry یا زمان‌های مساویِ زودترین receipt، primary یکتا ایجاد نمی‌کنند. در apply بدون primary، شواهد مجاز می‌توانند در بررسی دستی ثبت شوند؛ هیچ رسیدی به جای primaryِ نامعتبر promote نمی‌شود. referenceهای متعارض و timestamp قبلاً آینده نیازمند بررسی انسانی مستقل‌اند.

فقط وقتی کل مجموعهٔ موفق صریحاً انتخاب و معتبر شده، فرمان یک مجوز داخلی برای همان primary می‌سازد. `metadata.recovery_history.recovery_plan` شامل همهٔ payment IDهای بررسی‌شده، شناسه‌های انتخابی، شواهد نرمال‌شده، primary، fingerprint history و SHA256 بکاپ است. این مجوز boolean یا گزینهٔ عمومی CLI نیست. callback زیر قفل، history و شواهد Verify تازه را با plan مقایسه می‌کند. تغییر هم‌زمان، اجازهٔ fulfillment را از بین می‌برد. مجوز cancellation، inventory release، deadline، future time، reference یا idempotency را دور نمی‌زند.

پس از پذیرش primary، receiptهای موفقِ اضافه بدون fulfillment دوم برای refund handling ثبت می‌شوند؛ receipt مبهم همچنان manual می‌ماند. سفارش دارای receipt دستی از انقضای خودکار و آزادسازی stock مستثناست. پشتیبانی باید پرونده را سریع با این فرمان و شواهد رسمی حل کند؛ ویرایش مستقیم فیلدهای مالی یا refund/fulfillment یک‌کلیکی وجود ندارد.

HTTP خارج از تراکنشِ قفل‌دار است. اگر انقضا هنگام انتظار برای Verify زودتر commit کند، receipt بعدی سفارش را احیا نمی‌کند. اگر application معتبر callback اول commit کند، انقضا paid بودن را دوباره بررسی می‌کند. receipt دستی که پس از انتخاب candidate انقضا ثبت شده نیز با check دومِ زیر قفل از آزادسازی موجودی محافظت می‌شود.

## اتصال تلاش ایجادِ نامطمئن به شناسهٔ زیبال

تلاش ایجادِ `requesting`، `uncertain` یا `review` و تلاش بدون شناسهٔ معتبر در `unresolved_creation_requests` جداگانه گزارش می‌شود و primary تازه را مسدود می‌کند. UUID و وضعیت ایجاد در fingerprint کل history هستند؛ تلاش تازه هنگام Inquiry/Verify مجوز قدیمی را نامعتبر می‌کند. درخواست دارای رد مستند و بدون track ID، رسید موفق شمرده نمی‌شود و تاریخچهٔ رد آن حفظ می‌شود.

ابتدا UUID را از Payment Admin و track ID را از گزارش مستقل زیبال دریافت کنید. زیبال Inquiry با UUID محلی یا idempotency درخواست ایجاد را مستند نکرده است؛ اگر track ID قابل‌اعتبارسنجی ندارید، پرونده نیازمند پشتیبانی زیبال است و درخواست دوبارهٔ خودکار مجاز نیست.

حالت خواندنی، با دقیقاً یک track ID و UUID همان سفارش:

```sh
scripts/compose-production exec -T web python manage.py reconcile_zibal_payments \
  --order-number ORDER_NUMBER --track-id ZIBAL_TRACK_ID --request-id LOCAL_REQUEST_UUID
```

`--request-id` یک حالت اتصال شواهد است؛ primary انتخاب و سفارش fulfill نمی‌شود. Inquiry مبلغ، شمارهٔ سفارش و track ID را اعتبارسنجی می‌کند. مسیر apply همان backup تازه، sidecar و lock را لازم دارد؛ از دستور apply همین runbook با `--request-id LOCAL_REQUEST_UUID` و دقیقاً یک track ID استفاده کنید. Verify خارج قفل انجام و اتصال فقط روی وضعیت تازهٔ قفل‌شده ثبت می‌شود. شناسهٔ متعلق به تلاش دیگر بازنویسی نمی‌شود. برنامهٔ اتصال و SHA بکاپ در `creation_resolution_plan` و `recovery_history` ثبت می‌شوند.

وجه تأییدشدهٔ سفارش هنوز unpaid، پس از این مرحله در manual review می‌ماند و `needs_refund=False` است؛ timestamp مبهم به refund تبدیل نمی‌شود. اگر سفارش قبلاً لغو یا با رسید معتبر دیگری paid شده باشد، قواعد موجود برای receipt معتبر همچنان اعمال می‌شوند. پس از اتصال، inspect عادی را **بدون `--request-id`** با تمام track IDهای سفارش اجرا کنید؛ تنها preflight کامل و primary یکتا می‌تواند مجوز محدود fulfillment صادر کند. رسید تازهٔ متصل‌شده اگر از انتخاب عادی حذف شود، primary را همچنان مسدود می‌کند.

## الزام بکاپ برای apply

`--apply` بدون `--backup` متوقف می‌شود. بکاپ باید:

1. فایل معمولی `.sql.gz` با mtime در یک ساعت گذشته و بدون زمان آینده باشد.
2. sidecar معمولی `.sql.gz.sha256` موجود و قابل خواندن داشته باشد.
3. SHA256 واقعی‌اش با digest **ثبت‌شده** در sidecar برابر باشد؛ محاسبهٔ digest تازه به‌تنهایی کافی نیست.
4. تا EOF با CRC صحیح gzip باز شود و header دیتابیس تنظیم‌شده، جدول‌های `payments_payment`، `orders_order` و `products_productvariant` و footer اتمام dump را داشته باشد.
5. در طول اعتبارسنجی تغییر نکرده باشد.

basename داخل sidecar باید با فایل انتخابی تطبیق کند؛ مسیر مطلق legacy فقط برای تشخیص basename استفاده می‌شود و فرمان فایل دیگری را از مسیر داخل sidecar hash نمی‌کند. SHA ناموجود/اشتباه پیش از Inquiry و هر تغییر محلی application را متوقف می‌کند. این بررسی‌ها اثبات قابلیت restore نیستند؛ restore drill جداگانه لازم است.

بکاپ‌های نام‌گذاری‌شده توسط سرویس زیر shared flock پوشه در تمام apply نگه داشته می‌شوند. mount برای وب read-only است؛ flock روی descriptor خواندنی گرفته می‌شود. backup/retention همزمان با recovery از ادامه صرف‌نظر می‌کند. نبود lock معتبر یا فعال‌بودن writer، recovery را متوقف می‌کند. نام‌های legacy خارج از retention هستند.

## آماده‌سازی production با حفظ تنظیمات

این بخش، دستورالعمل عمومی استقرار نسخه‌های قدیمی‌تر نیز بوده است. طبق گزارش مالک، migrationهای پرداخت در production اعمال و سرویس وب بازسازی شده و healthy است؛ این وضعیت در این کار مستقیماً بررسی نشده است. اصلاح حاضر migration تازه ندارد. بخش build/provision/migrate زیر نباید صرفاً برای تراکنش ۷ دوباره اجرا شود؛ هر استقرار بعدی نیازمند بازبینی وضعیت واقعی و اجازهٔ مستقل مسئول production است و در این کار انجام نشده است. در محیط production مجاز، وضعیت را ثبت کنید:

```sh
git status --short --branch
git log -1 --format='%H %s'
git diff --name-only
git diff --cached --name-only
git stash list --format='%gd %h %s'
git merge-base --is-ancestor 910c231f64e2c78677b21699c096e70bbf8756ec HEAD
scripts/compose-production ps --all
scripts/compose-production config --quiet
```

اگر فایل‌های سرور، Enamad، Nginx/healthcheck، Compose یا stash محلی تفاوت دارند، آن‌ها را خصوصی بررسی و حفظ کنید. تغییرات mission را با تنظیمات سرور merge و بازبینی کنید؛ فایل محیط نمونه جایگزین `.env.production` نیست. reset، clean، force push، حذف volume و بازنویسی کار محلی مجاز نیستند. rendered `config` یا `docker inspect` کامل را در خروجی عمومی نگذارید؛ credential دارند.

ترتیب پیشنهادی پس از بازبینی تغییرات:

1. بازبینی مسیر bind mount، owner/mode، engineهای واقعی، ظرفیت دیسک و retention dry-run طبق سند بکاپ.
2. build تصاویر `web` و `db-backup`؛ گرفتن بکاپ دستی تازه از schema فعلی و بررسی sidecar/gzip.
3. maintenance window با توقف نویسنده‌های سفارش؛ اجرای migrationهای آزمایش‌شده یک بار، با `MIGRATE_ON_START=0` و `SEED_ON_START=0`.
4. راه‌اندازی وب و سرویس بکاپ با همان production project؛ بررسی readiness، logs و منبع داخل image.
5. Inquiry خواندنی، بازبینی گزارش و تصمیم مسئول دربارهٔ apply؛ در صورت گذشته‌بودن یک ساعت بکاپ تازه بگیرید.

دستورهای آماده برای مرحلهٔ build و migration، **فقط پس از اجازهٔ استقرار**:

```sh
scripts/compose-production --profile backup-admin build web db-backup db-backup-provision
# پس از تنظیم credential جداگانهٔ بکاپ در محیط خصوصی و healthy بودن DB:
scripts/compose-production --profile backup-admin run --rm -T db-backup-provision
scripts/backup-production-db
# پس از بررسی completion، SHA256 و gzip و توقف سایر نویسنده‌های سفارش:
scripts/compose-production stop web db-backup
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --plan
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --noinput
scripts/compose-production up -d --no-deps web db-backup
scripts/compose-production ps
scripts/compose-production logs --tail=100 db-backup
scripts/compose-production exec -T web python manage.py check --deploy
```

توقف کنترل‌شدهٔ نویسنده‌ها و reload Nginx باید متناسب با آرایش واقعی سرور بازبینی شوند. `up` عمومی در استقرار معمول سرویس بکاپ را هم شامل می‌شود؛ نیازی به project یا دیتابیس مستقل نیست. این migrationها پیش‌نیاز نسخه‌اند و طبق گزارش مالک قبلاً اعمال شده‌اند؛ بدون migration معوق، اجرای دوباره لازم نیست:

```text
orders/0002_order_inventory_released_alter_order_payment_status
payments/0003_payment_gateway_paid_at_alter_payment_needs_refund_and_more
payments/0004_payment_creation_status_payment_request_id_and_more
```

## اجرای apply بررسی‌شده

بکاپ تازه را با نام واقعی completion log انتخاب کنید؛ wildcard یا بکاپ تاریخی را جایگزین نکنید. `BACKUP_HOST_DIR` زیر باید دقیقاً با bind mount بازبینی‌شده برابر باشد. مثال در shell نوع Bash:

```sh
BACKUP_HOST_DIR=/root/minikandoo-backups
BACKUP_NAME=NEW_BACKUP_FILENAME.sql.gz
recovery_report=$(mktemp /root/zibal-recovery-XXXXXXXX.json)
chmod 600 "$recovery_report"
scripts/compose-production run --rm --no-deps -T --user 0:0 --entrypoint python \
  --volume "$BACKUP_HOST_DIR:/run/minikandoo-backups:ro" \
  web manage.py reconcile_zibal_payments \
  --order-number ORDER_NUMBER \
  --track-id EXISTING_TRACK_ID \
  --apply --backup "/run/minikandoo-backups/$BACKUP_NAME" \
  > "$recovery_report"
```

entrypoint مستقیم Python مانع collectstatic/migration/seed ناخواسته می‌شود؛ UID صفر فقط برای خواندن بکاپ root-owned است و Django از حساب دیتابیس موجود خود استفاده می‌کند. خروج غیرصفر و فایل گزارش را بررسی کنید. مخفی‌کردن خطا با `|| true` یا تغییر دستی وضعیت برای successful نشان دادن رسید مجاز نیست.

تا زمان تأیید رسمی، `ZIBAL_PAID_AT_TIMEZONE` خالی و `--gateway-timezone` حذف می‌ماند. برای پاسخ‌های naive، apply شواهد را manual ثبت می‌کند و غیرصفر گزارش می‌دهد؛ یک primary و دو refund پیش‌فرض فرض نکنید. پس از تأیید رسمی، تنظیم IANA و منبع تأیید ثبت و همان inspect دوباره اجرا می‌شود. موارد زمان آینده یا تعارض شواهد نیاز به بررسی انسانی مستقل دارند.

پس از apply، دوباره inspect اجرا و `actual_primary`، `after`، `manual_review_required`، `refund_required`، تصمیم هر replay و موجودی را بررسی کنید. اختلاف snapshot موجودی می‌تواند از خرید همزمان دیگران هم ناشی شود؛ به‌تنهایی تاریخچهٔ موجودی را اثبات نمی‌کند.

## آزمون و حدود اعتبار

suite شامل رگرسیون receipt دستیِ قبل از callback دوم، تاریخچهٔ ناقص/انتخاب‌نشده، مجوز محدود recovery، رزرو دستی، fingerprint متغیر و primary یکتا است. `TransactionTestCase` روی MariaDB، HTTP کند بدون قفل، expiry هنگام HTTP، هر دو ترتیب قفل، receipt دستی پس از candidate selection، callbackهای هم‌زمان و حفظ reference قطعی را آزمایش می‌کند. SQLite اثبات قفل ردیفی نیست. نتایج واقعی اجرای این نسخه در گزارش تحویل ثبت می‌شوند؛ نتایج نسخهٔ قبلی جایگزین اجرای دوباره نیستند.

Docker driver از حساب بکاپ جداگانه، provisioning کوتاه‌عمر، rotation، مجوزهای محدود، capacity cleanup و restore واقعی target موقت استفاده می‌کند. هیچ داده یا درخواست مالی production در این آزمون‌ها استفاده نمی‌شود.

```sh
PYTHONPATH=scripts/qa .venv/bin/python manage.py test --settings=shop_test_settings --noinput
.venv/bin/python scripts/qa/test_backup_stack.py
```

نتایج اجرای همین تغییرات، شامل SQLite و MariaDB 11.4.13 جداگانه، در [گزارش تحویل](zibal-manual-review-incident.md#آزمونها-و-حدود-اعتبار) ثبت شده‌اند. همهٔ پاسخ‌های زیبال mock هستند؛ موفقیت این آزمون‌ها اثبات Verify یک پرداخت زنده نیست. هیچ workflow تازه، push یا استقرار انجام نشده و بکاپ واقعی production و سرویس آن مستقیماً بررسی نشده‌اند.

## refund و حل موارد دستی

این فرمان هیچ refund API را فراخوانی نمی‌کند. پرداخت اضافه یا late معتبر فقط `needs_refund=True` دارد؛ «نیازمند»، «درخواست‌شده» و «تأیید‌شده در زیبال» سه وضعیت عملیاتی متفاوت‌اند. refund تنها با اجازهٔ انسانی و روش رسمی فعال پذیرنده انجام شود؛ flag محلی اثبات بازگشت پول نیست.

ادمین باید raw `paidAt`، تفسیر، reference، مبلغ، وضعیت سفارش، سابقهٔ آزادسازی موجودی و شواهد پنل را بررسی کند. تأیید timezone نمی‌تواند رزرو آزادشده را برگرداند. برای احیای دستی یک سفارش لغوشده، بررسی موجودی و اقدام تجاری جداگانه لازم است؛ این recovery چنین عملیاتی ندارد.
