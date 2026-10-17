# راهنمای استقرار Production روی Ubuntu با Docker

این راهنما برای دامنه‌های `minikandoo.ir` و `www.minikandoo.ir` و میزبان API یعنی `api.minikandoo.ir` است. فعلاً کل برنامه یک Django SSR است؛ Nginx هر سه hostname را به همان Gunicorn می‌فرستد، اما میزبان API در Django فقط URLconf محدود سلامت و پاسخ JSON 404 دارد. مسیرهای REST فروشگاه، پرداخت، سبد یا حساب کاربری هنوز API نشده‌اند. صفحات فروشگاه و `/admin/` روی دامنه اصلی می‌مانند. هیچ CORS یا endpoint جعلی در این تنظیمات فعال نشده است.

## 1. سرور و DNS

- VPS تازه/پشتیبانی‌شده با Ubuntu 26.04، دسترسی SSH با کاربر sudo، فضای دیسک کافی برای دیتابیس، تصاویر و بکاپ‌ها.
- Docker Engine و Compose plugin رسمی را طبق [راهنمای Docker برای Ubuntu](https://docs.docker.com/engine/install/ubuntu/) نصب کنید. کانتینرها را با Compose اجرا می‌کنیم؛ Docker Desktop لازم نیست.
- رکوردهای DNS از نوع A برای `@`، `www` و `api` را به IPv4 سرور بگذارید. اگر IPv6 ندارید، رکورد AAAA قدیمی/نادرست این نام‌ها را حذف کنید؛ اگر IPv6 دارید فایروال و مسیر Nginx باید آن را هم پشتیبانی کنند.
- فایروال ارائه‌دهنده و UFW را فقط برای SSH (ترجیحاً IP مدیریتی)، TCP/80 و TCP/443 باز کنید. پورت‌های 3306، 6379 و 8000 نباید عمومی باشند.

## 2. دریافت پروژه و secrets

پروژه را به مسیر پایدار سرور منتقل/clone کنید، سپس:

```sh
cp .env.production.example .env.production
chmod 600 .env.production
openssl rand -base64 48  # یک بار برای SECRET_KEY
openssl rand -base64 48  # مقدار جداگانه برای PAYMENT_SECRET
openssl rand -base64 32  # برای DATABASE_PASSWORD
openssl rand -base64 32  # مقدار جداگانه برای MARIADB_ROOT_PASSWORD
nano .env.production
```

مقادیر `REPLACE_...` را حتماً جایگزین کنید. `.env.production` را commit یا در image کپی نکنید. subnetهای Docker در فایل نمونه باید با شبکه‌های VPN/شبکه داخلی VPS تداخل نداشته باشند؛ در صورت تغییر `PROXY_SUBNET`، `NGINX_PROXY_IP` نیز باید داخل همان subnet باشد. Compose production اختصاصی دو شبکه‌ی non-overlapping می‌سازد و شبکه‌ی اپ داخلی است.

برای سرویس‌های واقعی `SMS_PROVIDER` و `PAYMENT_GATEWAY` باید adapter سازگار همین پروژه و کلیدهای واقعی پیکربندی شوند. در کد کنونی `console` پیامک واقعی ارسال نمی‌کند و `mock` تراکنش واقعی انجام نمی‌دهد؛ راه‌اندازی وب با این مقادیر به معنی آماده بودن فروش برای مشتری نیست.

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

این گواهی در مسیر مشترک `/etc/letsencrypt/live/minikandoo.ir/` ساخته می‌شود؛ فایل Nginx دامنه API از همان certificate استفاده می‌کند. سپس bootstrap را متوقف و Nginx نهایی را اجرا کنید:

```sh
scripts/compose-production --bootstrap down
scripts/compose-production up -d --build
```

## 4. راه‌اندازی و بررسی

کانفیگ Compose را قبل از اجرا parse کنید:

```sh
scripts/compose-production config --quiet
scripts/compose-production --bootstrap config --quiet
scripts/compose-production up -d --build
scripts/compose-production ps
scripts/compose-production logs --tail=200 web nginx db
```

Migration در هر deploy خودکار نیست. پس از backup و در maintenance window، migration را یک‌بار اجرا و سپس سرویس را بالا بیاورید:

```sh
scripts/compose-production run --rm --no-deps web python manage.py migrate --noinput
scripts/compose-production up -d
scripts/compose-production exec web python manage.py createsuperuser
scripts/compose-production exec web python manage.py check --deploy
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

Certbot به‌صورت خودکار daemon نیست. برای تمدید دوره‌ای، cron/systemd timer میزبان یا job زمان‌بندی‌شده ایجاد کنید که از همین image و volumeها استفاده کند:

```sh
scripts/compose-production --bootstrap run --rm certbot renew --webroot -w /var/www/certbot
scripts/compose-production exec -T nginx nginx -s reload
```

آن را مثلاً روزی دو بار زمان‌بندی کنید. ابتدا `renew --dry-run` را آزمایش کنید. reload فقط پس از موفقیت تمدید انجام شود.

## 6. داده‌ها و نگهداری

- Compose named volumes را نگه می‌دارد: `db-data`, `media-data`, `redis-data`, `staticfiles`, `certbot-webroot`, `letsencrypt`. از `docker compose down -v` و پاک‌کردن volumeها استفاده نکنید.
- پیش از deploy/migration، از MariaDB بکاپ بگیرید و خارج از همان VPS، رمزگذاری‌شده نگهداری کنید؛ برای `media-data` نیز snapshot/copy زمان‌بندی‌شده بگیرید. بازیابی بکاپ را دوره‌ای آزمایش کنید.
- نمونه بکاپ دیتابیس (رمز را در history خط فرمان قرار ندهید؛ از prompt یا secret file محدود استفاده کنید):

```sh
scripts/compose-production exec -T db sh -c 'mariadb-dump -u root -p"$MARIADB_ROOT_PASSWORD" "$MARIADB_DATABASE"' > backup.sql
```

- لاگ‌ها از stdout/stderr جمع‌آوری و با محدودیت اندازه چرخانده می‌شوند. دیسک، وضعیت volumeها، TLS expiration، error rate و `/readyz/` را مانیتور کنید.
- نسخه‌های imageها را در چرخه نگهداری به‌روز و پس از تغییر dependency/Dockerfile build و تست کنید.

## مرزهای موجود

MariaDB و Redis فقط در شبکه داخلی Compose هستند. Nginx تنها سرویس دارای host ports است. Django/Gunicorn روی 8000 داخل Docker است. فایل‌های static و media از volume read-only توسط Nginx سرو می‌شوند. پنل ادمین همان `/admin/` دامنه اصلی است؛ subdomain API پنل یا storefront را mirror نمی‌کند.
