# بازیابی امن پرداخت زیبال

این runbook اصلاحات هدفمند پس از `45d5a5d0aa99022e00a13455925d7e69709d4fc0` را توضیح می‌دهد. کد و آزمون‌ها محلی آماده شده‌اند؛ اجرای فرمان‌های production، migration، recovery یا refund در این کار انجام نشده است. [قرارداد پرداخت و timezone](zibal-payment.md) و [عملیات بکاپ Docker](production-database-backups.md) را پیش از اجرا بخوانید.

## علت حادثه و وضعیت شواهد

اعتبارسنجی قدیمی `refNumber` تنها integer را قبول می‌کرد. numeric string واقعی باعث خطای «شماره مرجع گزارش زیبال نامعتبر است.» و rollback محلی callback می‌شد. اصلاح رشتهٔ ASCII مثبت و حفظ صفرهای ابتدایی باقی مانده است. بازبینی بعدی دو خطر مستقل را پوشش می‌دهد: مقایسهٔ زمان ورود callback با deadline، و آزادسازی موجودی همزمان با تأیید پرداخت.

شواهد زیر **از گزارش مالک** هستند و در این محیط از زیبال دوباره inquiry نشده‌اند:

| سفارش | Track ID | شواهد گزارش‌شده |
| --- | --- | --- |
| `ORD-20261114-9BQ4H2` | `4911955398` | `result=100`، `status=1`، مبلغ و سفارش منطبق، reference رشته‌ای معتبر، `paidAt` موجود |
| همان سفارش | `4911974825` | همان شرایط |
| همان سفارش | `4911986194` | همان شرایط |

وضعیت فعلی production، مقدار واقعی `paidAt`، reference، موجودی و primary هنوز باید در سرور خوانده شوند. صرف وجود `paidAt` به معنای timezone مشخص نیست. با نبود تأیید رسمی timezone، زمان‌های بدون offset همگی manual review هستند؛ حتی اگر هر سه timestamp در یک قالب naive باشند، روی «ساعت API» مرتب و پذیرفته نمی‌شوند.

## حالت پیش‌فرض: Inquiry بدون تغییر دیتابیس

پس از نصب نسخهٔ جدید و اعمال migrationهای بررسی‌شده، در ریشهٔ پروژه:

```sh
scripts/compose-production exec -T web python manage.py reconcile_zibal_payments \
  --order-number ORD-20261114-9BQ4H2 \
  --track-id 4911955398 4911974825 4911986194
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

`metadata.recovery_history` شامل منبع فرمان، SHA256 بکاپ، نام بکاپ، شناسه‌های انتخابی، primary برنامه‌ریزی‌شده و زمان مشاهده است. `verification_history`، raw `paidAt` و `decision` نیز حفظ می‌شوند. callback تکراری رسیدی که قبلاً successful شده فقط شمارنده را افزایش می‌دهد و رسید/fulfillment را بازنویسی نمی‌کند.

## رسیدهای حل‌نشده و مجوز محدود primary

کل history سفارش بررسی می‌شود: `MANUAL_REVIEW`، `paid_at`/`gateway_paid_at`، verification موفقِ فعلی یا آرشیوشده در `verification_history` حتی با status غیرنهایی، درگاه تاریخی و receiptهای خارج از `--track-id`. پاسخ ناموفق تازه شواهد موفق قدیمی را پاک نمی‌کند. برای primary تازهٔ سفارش هنوز پرداخت‌نشده، تمام رسیدهای بالقوه موفق باید صریحاً انتخاب شوند. گزارش بخش‌های مستقل `existing_manual_review_receipts`، `unselected_manual_review_receipts`، `unselected_potentially_successful_receipts`، `potentially_successful_receipts`، `confirmed_successful_receipts`، `incomplete_inquiries`، `ambiguous_timestamps` و `primary_selection_blockers` دارد.

رسید دستیِ انتخاب‌نشده یا رسید بالقوه موفقِ بدون Inquiry معتبر، انتخاب primary و apply را مسدود می‌کند. timestamp مبهم، خطای selected Inquiry یا زمان‌های مساویِ زودترین receipt، primary یکتا ایجاد نمی‌کنند. در apply بدون primary، شواهد مجاز می‌توانند در بررسی دستی ثبت شوند؛ هیچ رسیدی به جای primaryِ نامعتبر promote نمی‌شود. referenceهای متعارض و timestamp قبلاً آینده نیازمند بررسی انسانی مستقل‌اند.

فقط وقتی کل مجموعهٔ موفق صریحاً انتخاب و معتبر شده، فرمان یک مجوز داخلی برای همان primary می‌سازد. `metadata.recovery_history.recovery_plan` شامل همهٔ payment IDهای بررسی‌شده، شناسه‌های انتخابی، شواهد نرمال‌شده، primary، fingerprint history و SHA256 بکاپ است. این مجوز boolean یا گزینهٔ عمومی CLI نیست. callback زیر قفل، history و شواهد Verify تازه را با plan مقایسه می‌کند. تغییر هم‌زمان، اجازهٔ fulfillment را از بین می‌برد. مجوز cancellation، inventory release، deadline، future time، reference یا idempotency را دور نمی‌زند.

پس از پذیرش primary، receiptهای موفقِ اضافه بدون fulfillment دوم برای refund handling ثبت می‌شوند؛ receipt مبهم همچنان manual می‌ماند. سفارش دارای receipt دستی از انقضای خودکار و آزادسازی stock مستثناست. پشتیبانی باید پرونده را سریع با این فرمان و شواهد رسمی حل کند؛ ویرایش مستقیم فیلدهای مالی یا refund/fulfillment یک‌کلیکی وجود ندارد.

HTTP خارج از تراکنشِ قفل‌دار است. اگر انقضا هنگام انتظار برای Verify زودتر commit کند، receipt بعدی سفارش را احیا نمی‌کند. اگر application معتبر callback اول commit کند، انقضا paid بودن را دوباره بررسی می‌کند. receipt دستی که پس از انتخاب candidate انقضا ثبت شده نیز با check دومِ زیر قفل از آزادسازی موجودی محافظت می‌شود.

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

این مراحل نیازمند بازبینی و اجازهٔ مسئول production هستند؛ در این کار اجرا نشده‌اند. در `/root/Online-Clothing-Store` وضعیت را ثبت کنید:

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

توقف کنترل‌شدهٔ نویسنده‌ها و reload Nginx باید متناسب با آرایش واقعی سرور بازبینی شوند. `up` عمومی در استقرار معمول سرویس بکاپ را هم شامل می‌شود؛ نیازی به project یا دیتابیس مستقل نیست. این دو migration لازم‌اند:

```text
orders/0002_order_inventory_released_alter_order_payment_status
payments/0003_payment_gateway_paid_at_alter_payment_needs_refund_and_more
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
  --order-number ORD-20261114-9BQ4H2 \
  --track-id 4911955398 4911974825 4911986194 \
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

فعال‌شدن workflow در GitHub با افزودن فایل CI، به معنی موفقیت اجرای CI نیست؛ نتیجهٔ runner باید جداگانه بررسی شود. push و commit نهایی در گزارش تحویل ثبت می‌شوند. سرور production، سه تراکنش واقعی، historical backup واقعی و فعال‌بودن `db-backup` هنوز در این محیط تأیید نشده‌اند.

## refund و حل موارد دستی

این فرمان هیچ refund API را فراخوانی نمی‌کند. پرداخت اضافه یا late معتبر فقط `needs_refund=True` دارد؛ «نیازمند»، «درخواست‌شده» و «تأیید‌شده در زیبال» سه وضعیت عملیاتی متفاوت‌اند. refund تنها با اجازهٔ انسانی و روش رسمی فعال پذیرنده انجام شود؛ flag محلی اثبات بازگشت پول نیست.

ادمین باید raw `paidAt`، تفسیر، reference، مبلغ، وضعیت سفارش، سابقهٔ آزادسازی موجودی و شواهد پنل را بررسی کند. تأیید timezone نمی‌تواند رزرو آزادشده را برگرداند. برای احیای دستی یک سفارش لغوشده، بررسی موجودی و اقدام تجاری جداگانه لازم است؛ این recovery چنین عملیاتی ندارد.
