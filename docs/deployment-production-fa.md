# راهنمای استقرار Production روی Ubuntu با Docker

این راهنما برای دامنه‌های `minikandoo.ir` و `www.minikandoo.ir` و میزبان API یعنی `api.minikandoo.ir` است. فعلاً کل برنامه یک Django SSR است؛ Nginx هر سه hostname را به همان Gunicorn می‌فرستد، اما میزبان API در Django فقط URLconf محدود سلامت و پاسخ JSON 404 دارد. مسیرهای REST فروشگاه، پرداخت، سبد یا حساب کاربری هنوز API نشده‌اند. صفحات فروشگاه و `/admin/` روی دامنه اصلی می‌مانند. هیچ CORS یا endpoint جعلی در این تنظیمات فعال نشده است.

سرویس مستقل `db-backup` نیز در همین Compose project است. [عملیات بکاپ دیتابیس](production-database-backups.md) و [بازیابی پرداخت زیبال](zibal-verification-recovery.md) شامل dry-run، migrationهای جدید و مراحل استقرار بررسی‌شده‌اند. این سند دستور استقرار است؛ در کار اصلاح پرداخت/بکاپ هیچ restart یا deployment تولید اجرا نشده است.

نگهداری پرداخت با timer واحد `minikandoo-payments.timer` و فرمان `run_payment_maintenance --apply` در `web` موجود انجام می‌شود. پس از schema جدید `payments/0006`، فایل‌های `scripts/systemd/` باید توسط مسئول سرور طبق [راهنمای ترب‌پی](torobpay-payment.md) نصب شوند؛ بازیابی هر دقیقه و انقضا در مرزهای پنج‌دقیقه‌ای UTC اجرا می‌شوند. هنگام maintenance window، ابتدا timer را متوقف و پایان اجرای جاری را کنترل کنید؛ پس از rollout و سلامت وب، timer و نتیجهٔ نخستین اجرای آن را بررسی کنید. این زمان‌بندی در مخزن آماده شده و روی سرور واقعی در این وظیفه نصب نشده است.

## 1. سرور و DNS

- VPS تازه/پشتیبانی‌شده با Ubuntu 26.04، دسترسی SSH با کاربر sudo، فضای دیسک کافی برای دیتابیس، تصاویر و بکاپ‌ها.
- Docker Engine و Compose plugin رسمی را طبق [راهنمای Docker برای Ubuntu](https://docs.docker.com/engine/install/ubuntu/) نصب کنید. کانتینرها را با Compose اجرا می‌کنیم؛ Docker Desktop لازم نیست.
- رکوردهای DNS از نوع A برای `@`، `www` و `api` را به IPv4 سرور بگذارید. اگر IPv6 ندارید، رکورد AAAA قدیمی/نادرست این نام‌ها را حذف کنید؛ اگر IPv6 دارید فایروال و مسیر Nginx باید آن را هم پشتیبانی کنند.
- فایروال ارائه‌دهنده و UFW را فقط برای SSH (ترجیحاً IP مدیریتی)، TCP/80 و TCP/443 باز کنید. پورت‌های 3306، 6379 و 8000 نباید عمومی باشند.

## 2. دریافت پروژه و secrets

پروژه را به مسیر پایدار سرور منتقل/clone کنید، سپس:

دستورهای کپی زیر فقط برای نصب تازه‌اند؛ در سرور موجود، `.env.production`، تنظیمات محلی و stashها را حفظ و فقط کلیدهای لازم را به فایل موجود اضافه کنید.

```sh
cp .env.production.example .env.production
chmod 600 .env.production
openssl rand -base64 48  # یک بار برای SECRET_KEY
openssl rand -base64 32  # برای DATABASE_PASSWORD
openssl rand -base64 32  # مقدار جداگانه برای MARIADB_ROOT_PASSWORD
openssl rand -base64 32  # مقدار مستقل برای BACKUP_DB_PASSWORD
nano .env.production
```

مقادیر `REPLACE_...` را حتماً جایگزین کنید. `.env.production` را commit یا در image کپی نکنید. subnetهای Docker در فایل نمونه باید با شبکه‌های VPN/شبکه داخلی VPS تداخل نداشته باشند؛ در صورت تغییر `PROXY_SUBNET`، `NGINX_PROXY_IP` نیز باید داخل همان subnet باشد. Compose production اختصاصی دو شبکه‌ی non-overlapping می‌سازد و شبکه‌ی اپ داخلی است.

درگاه پرداخت زیبال پیاده‌سازی شده است. `PAYMENT_GATEWAY=zibal` و `ZIBAL_MERCHANT` واقعی پنل زیبال را تنظیم کنید؛ `SITE_URL=https://minikandoo.ir` باید با دامنه ثبت‌شده درگاه مرتبط باشد و IP خروجی VPS، در صورت فعال بودن محدودیت IP، در پنل مجاز باشد. حساب تست `zibal` در Production رد می‌شود. اتصال HTTPS خروجی به `gateway.zibal.ir` لازم است. [جزئیات چرخه پرداخت و بازیابی](zibal-payment.md) را ببینید. برای `SMS_PROVIDER` همچنان adapter و کلید واقعی لازم است؛ `console` پیامک واقعی ارسال نمی‌کند.

`ZIBAL_PAID_AT_TIMEZONE` تا تأیید کتبی نام IANA از زیبال خالی می‌ماند؛ زمان‌های بدون offset بررسی دستی می‌شوند. `BACKUP_HOST_DIR` پیش‌فرض `/root/minikandoo-backups` باید موجود و با UID/GID سرویس سازگار باشد. قبل از روشن‌کردن deletion، retention dry-run و engine/ظرفیت واقعی را طبق سند بکاپ بازبینی کنید. بکاپ تاریخی `db-20261114-190647.sql.gz` خارج از الگوی حذف سرویس است.

حساب مستقل `BACKUP_DB_USER` و `BACKUP_DB_PASSWORD` الزامی است؛ worker طولانی‌عمر root password نمی‌گیرد. روی سرور موجود فقط کلیدهای جدید را به محیط خصوصی اضافه کنید. provisioning در مرحلهٔ بعد صریح است؛ init scriptهای DB روی volume موجود دوباره اجرا نمی‌شوند.

## 3. صدور اولیه TLS

پیش از درخواست گواهی، DNS باید resolve شود و ورودی اینترنت TCP/80 به سرور برسد. ابتدا Nginx موقت HTTP بالا می‌آید:

```sh
scripts/compose-production --bootstrap up -d nginx
scripts/compose-production --bootstrap ps
```

هر سه نام را با Certbot در یک گواهی بگیرید:

```sh
scripts/compose-production --bootstrap run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d minikandoo.ir -d www.minikandoo.ir -d api.minikandoo.ir \
  --email YOUR_EMAIL --agree-tos --no-eff-email
```

این گواهی در مسیر مشترک `/etc/letsencrypt/live/minikandoo.ir/` ساخته می‌شود؛ فایل Nginx دامنه API از همان certificate استفاده می‌کند. فقط Nginx موقت را متوقف کنید؛ نسخهٔ نهایی پس از migration در مرحلهٔ بعد بالا می‌آید:

```sh
scripts/compose-production --bootstrap stop nginx
```

## 4. راه‌اندازی و بررسی

پس از بازبینی و اجازهٔ استقرار، کانفیگ Compose را parse و تصاویر را build کنید. هنوز وب و scheduler جدید را شروع نکنید:

```sh
scripts/compose-production config --quiet
scripts/compose-production --bootstrap config --quiet
scripts/compose-production build web
scripts/compose-production --profile backup-admin build db-backup db-backup-provision
```

فقط در **نصب تازه**، پیش از migration دیتابیس و Redis را راه‌اندازی کنید. دیتابیس خالی هنوز recovery point معنادار ندارد؛ نخستین بکاپ scheduler پس از ایجاد schema گرفته می‌شود:

```sh
scripts/compose-production up -d --wait db redis
```

برای هر دو حالت نصب تازه و به‌روزرسانی، قبل از manual backup یا شروع worker جدید، حساب بکاپ را با DB سالم و مجوز استقرار صریح provision کنید:

```sh
scripts/compose-production --profile backup-admin run --rm -T db-backup-provision
```

این سرویس کوتاه‌عمر فقط حساب جداگانه را می‌سازد و grants حداقلی dump را تنظیم می‌کند؛ رمز حساب موجود فقط با `--rotate-password` تغییر می‌کند. failure را پیش از ادامه رفع کنید. دستورهای rotation و grants اختیاری non-InnoDB در [سند بکاپ](production-database-backups.md) هستند؛ به worker دسترسی root یا ALL PRIVILEGES ندهید.

در **به‌روزرسانی سرور موجود**، DB را recreate نکنید. از schema فعلی بکاپ دستی بگیرید، completion و SHA256/gzip را طبق سند بکاپ بررسی کنید، سپس در maintenance window وب، scheduler بکاپ و سایر نویسنده‌های سفارش را متوقف کنید. اگر backup با exit غیرصفر تمام شد، به migration نروید:

```sh
scripts/backup-production-db
scripts/compose-production stop web db-backup
```

Migration در هر deploy خودکار نیست. با تصویر بررسی‌شده، plan را بررسی و migration را یک‌بار اجرا کنید؛ وب و scheduler پس از آن شروع می‌شوند. نسخهٔ ایجاد پایدار پرداخت به `payments/0004_payment_creation_status_payment_request_id_and_more` نیاز دارد و اطلاعات مالی قبلی را تغییر نمی‌دهد. برگشت این migration پس از ثبت درخواست‌های جدید، برای حفظ شواهد عمداً متوقف می‌شود؛ rollback schema ابزار رفع خطای پرداخت نیست:

```sh
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --plan
scripts/compose-production run --rm --no-deps -T --entrypoint python web manage.py migrate --noinput
scripts/compose-production up -d --no-deps web db-backup
scripts/compose-production ps
scripts/compose-production logs --tail=200 web nginx db db-backup
scripts/compose-production exec web python manage.py check --deploy
```

با `MIGRATE_ON_START=0`، entrypoint قبل از شروع Gunicorn، `python manage.py migrate --check --noinput` را اجرا می‌کند. این کنترل فقط خواندنی است؛ migration معوق یا خطای بررسی، شروع وب را متوقف می‌کند و migration خودکار اجرا نمی‌شود. فرمان‌های migration بالا با `--entrypoint python` مستقیماً عملیات بررسی‌شده را اجرا می‌کنند؛ guard برای شروع معمول وب است. در failure، علت را با حفظ بکاپ و writerهای متوقف بررسی کنید؛ migration مالی معکوس یا بازسازی DB خودکار مجاز نیست.

در نصب تازه، پس از migration، کل stack از جمله Nginx را شروع کنید. در سرور موجود، فرمان بالا DB و Redis موجود را recreate نمی‌کند؛ reload یا راه‌اندازی Nginx را متناسب با آرایش واقعی سرور بازبینی کنید:

```sh
# فقط نصب تازه؛ schema و DB/Redis اکنون آماده‌اند.
scripts/compose-production up -d
```

ساخت superuser فقط برای نصب تازه لازم است:

```sh
scripts/compose-production exec web python manage.py createsuperuser
```

بررسی endpointها از خود سرور:

```sh
curl -I http://minikandoo.ir/
curl -I https://minikandoo.ir/
curl -I https://www.minikandoo.ir/
curl -i https://api.minikandoo.ir/healthz/
curl -i https://api.minikandoo.ir/any-not-yet-implemented-endpoint/
```

انتظار API در وضعیت فعلی: `/healthz/` سلامت Django را JSON می‌دهد و مسیر API تعریف‌نشده JSON 404 می‌دهد. اگر JavaScript سایت قرار است از `minikandoo.ir` به این subdomain درخواست fetch بزند، ابتدا endpoint و روش احراز هویت واقعی طراحی شود، سپس CORS/CSRF متناسب با آن اضافه گردد. افزودن صرف hostname، API فروشگاه تولید نمی‌کند.

## 5. تمدید TLS

Certbot به‌صورت خودکار daemon نیست. تمدید و reload را با همین image و volumeهای Compose اجرا کنید و روند عملیاتی فعلی TLS را حفظ کنید:

```sh
scripts/compose-production --bootstrap run --rm certbot renew --webroot -w /var/www/certbot
scripts/compose-production exec -T nginx nginx -s reload
```

ابتدا `renew --dry-run` را آزمایش کنید. reload فقط پس از موفقیت تمدید انجام شود. scheduler جدید بکاپ به این فرایند وابسته نیست و هیچ سرویس زمان‌بندی میزبان ایجاد نمی‌کند.

## 6. داده‌ها و نگهداری

- Compose named volumes را نگه می‌دارد: `db-data`, `media-data`, `redis-data`, `staticfiles`, `certbot-webroot`, `letsencrypt`. از `docker compose down -v` و پاک‌کردن volumeها استفاده نکنید.
- پیش از deploy/migration، از MariaDB بکاپ معتبر بگیرید و بازیابی آن را جداگانه آزمایش کنید. same-host backup از نابودی کامل VPS محافظت نمی‌کند؛ off-site رمزگذاری‌شده و بکاپ media نیازمند برنامه و اجازهٔ جداگانه‌اند.
- بکاپ دستی از همان implementation کانتینر، بدون password در argv:

```sh
scripts/backup-production-db
scripts/compose-production run --rm --no-deps -T db-backup retention --dry-run
scripts/compose-production logs --tail=100 db-backup
```

- لاگ‌ها از stdout/stderr جمع‌آوری و با محدودیت اندازه چرخانده می‌شوند. دیسک، وضعیت volumeها، TLS expiration، error rate و `/readyz/` را مانیتور کنید.
- نسخه‌های imageها را در چرخه نگهداری به‌روز و پس از تغییر dependency/Dockerfile build و تست کنید.

بکاپ اولیه پس از startup و سپس در مرزهای ۰۰/۳۰ دقیقهٔ UTC اجرا می‌شود. bind mount از recreate کانتینر محفوظ است. در حالت `apply`، recent 48 و daily 7 با هم‌پوشانی نگهداری و فقط نام‌های managed پاک می‌شوند؛ در فشار ظرفیت، eligible pairها پیش از dump قابل حذف‌اند و سپس ظرفیت دوباره بررسی می‌شود. مجموعهٔ protected هرگز برای ساخت dump تازه پایین آورده نمی‌شود. پیش‌فرض dry-run تا بازبینی اولیه هیچ حذف واقعی ندارد. checksum sidecar، gzip و restore آزمایشی معیارهای جداگانه‌اند. [راهنمای کامل](production-database-backups.md) دستورهای verification، محدودیت ظرفیت، restore با تأیید انسانی و عیب‌یابی health را دارد.

## مرزهای موجود

MariaDB و Redis فقط در شبکه داخلی Compose هستند. Nginx تنها سرویس دارای host ports است. Django/Gunicorn روی 8000 داخل Docker است. فایل‌های static و media از volume read-only توسط Nginx سرو می‌شوند. پنل ادمین همان `/admin/` دامنه اصلی است؛ subdomain API پنل یا storefront را mirror نمی‌کند.
