# فروشگاه اینترنتی پوشاک

فروشگاه اینترنتی کامل و آماده‌ی استفاده‌ی واقعی، با **Django 6.1 + MySQL/MariaDB** و رابط کاربری تماماً فارسی و RTL.

بدون هیچ Placeholder، تستی یا منطق جعلی. همه‌چیز واقعی است: احراز هویت با OTP، سبد خرید، ثبت سفارش با قفل ردیفی، و پرداخت Redirect-Based با تأیید سمت سرور.

---

## فهرست

- [امکانات](#امکانات)
- [پشته‌ی فناوری](#پشتهی-فناوری)
- [راه‌اندازی سریع](#راهاندازی-سریع)
- [اجرا با Docker (پیشنهادی)](#اجرا-با-docker-پیشنهادی)
- [ساخت دیتابیس و اتصال phpMyAdmin](#ساخت-دیتابیس-و-اتصال-phpmyadmin)
- [اجرای پروژه](#اجرای-پروژه)
- [داده‌های نمونه](#دادههای-نمونه)
- [تست‌ها](#تستها)
- [ساخت فایل‌های CSS/JS](#ساخت-فایلهای-cssjs)
- [پیکربندی پیامک](#پیکربندی-پیامک)
- [پیکربندی درگاه پرداخت](#پیکربندی-درگاه-پرداخت)
- [استقرار Production](#استقرار-production)
- [عیب‌یابی](#عیبیابی)

---

## امکانات

### فروشگاه
- صفحه‌ی خانه با محصولات ویژه و تخفیف‌دار
- فروشگاه با فیلتر رنگ، سایز، بازه‌ی قیمت، فقط تخفیف‌دار، فقط موجود و مرتب‌سازی
- جستجوی واقعی در نام، توضیحات و نام دسته‌بندی
- صفحه‌ی دسته‌بندی‌ها و صفحه‌ی اختصاصی هر دسته (شامل زیردسته‌ها)
- جزئیات محصول با انتخاب رنگ/سایز، نمایش موجودی لحظه‌ای و محصولات مرتبط
- اسلاگ فارسی در URL

### احراز هویت (فقط شماره موبایل + OTP)
- بدون رمز عبور؛ ورود با شماره‌ی موبایل و کد یک‌بارمصرف
- کد هرگز خام ذخیره نمی‌شود؛ فقط **HMAC-SHA256** آن با `SECRET_KEY`
- انقضا، محدودیت تلاش، فاصله‌ی ارسال مجدد و سقف درخواست در ساعت
- Rate Limiting مبتنی بر Cache برای IP و شماره
- ادغام خودکار سبد خرید مهمان با سبد کاربر پس از ورود
- تکمیل پروفایل و مدیریت آدرس‌ها

### سبد خرید و سفارش
- سبد برای مهمان (Session) و کاربر واردشده
- بروزرسانی و حذف آیتم با **HTMX** بدون Reload
- **موجودی در سطح واریانت (رنگ + سایز)**
- جلوگیری از Overselling با `Transaction` + `SELECT ... FOR UPDATE` + شرط SQL
- Snapshot کامل قیمت، نام، رنگ، سایز و آدرس در لحظه‌ی ثبت سفارش
- محاسبه‌ی همه‌ی مقادیر مالی **فقط در Backend** با `Decimal` (هرگز `float`)
- ارسال پست پیشتاز، ارسال رایگان بالای سقف، یادداشت مشتری
- انقضای خودکار سفارش‌های پرداخت‌نشده و آزادسازی موجودی

### پرداخت (Hosted / Redirect Gateway)
- هیچ اطلاعات کارتی روی سرور ما ذخیره یا پردازش نمی‌شود
- انتقال به درگاه با `meta http-equiv=refresh` — **بدون نیاز به JavaScript**
- امضای HMAC روی پارامترهای شروع و بازگشت
- **همیشه Verify سمت سرور**؛ داده‌ی Query به‌تنهایی پذیرفته نمی‌شود
- Callback **Idempotent** — ارسال دوباره باعث پردازش مجدد یا کم‌شدن دوباره‌ی موجودی نمی‌شود
- درگاه Mock برای توسعه با صفحه‌ی شبیه‌سازی‌شده‌ی درگاه بانکی

### پنل مدیریت
- ظاهر فارسی و RTL
- داشبورد آماری (فروش، سفارش‌ها، موجودی رو به اتمام)
- گذار وضعیت سفارش از مسیر کنترل‌شده
- مدیریت محصول، دسته‌بندی، رنگ، سایز، تخفیف، کاربران، آدرس‌ها و تراکنش‌ها

### امنیت
- همه‌ی Secret ها در Environment؛ فایل `.env` در `.gitignore`
- CSP بومی Django 6.1 با Nonce؛ `unsafe-inline` برای Script ممنوع
- هیچ Inline Event Handler ای در قالب‌ها وجود ندارد
- Security Headers کامل (`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, HSTS)
- اعتبارسنجی کامل ورودی‌ها در هر دو سطح Form و Service
- محافظت Brute-Force با شمارنده‌ی تلاش‌ها

---

## پشته‌ی فناوری

| لایه | فناوری |
| --- | --- |
| Backend | Python 3.14، Django 6.1 |
| ابزار مدیریت بسته | `uv` |
| دیتابیس | MySQL / MariaDB (درایور `mysqlclient`) |
| فرانت‌اند | Tailwind CSS v4، HTMX، Alpine.js |
| فونت | وزیرمتن (Vazirmatn) |
| Static | WhiteNoise |
| تاریخ شمسی | `jdatetime` |
| تصاویر | Pillow |

هیچ React، Next.js یا Node در Runtime استفاده نمی‌شود؛ Node فقط برای ساخت CSS و کپی فایل‌های Vendor است.

---

## راه‌اندازی سریع

پیش‌نیازها: **Python 3.14**، **`uv`**، **Node.js**، **MySQL 8+ یا MariaDB 10.6+**.

```bash
# ۱) نصب وابستگی‌های Python
uv sync

# ۲) ساخت فایل پیکربندی
cp .env.example .env
# سپس مقادیر واقعی را در .env قرار دهید

# ۳) ساخت دیتابیس و کاربر
bash scripts/mysql_dev.sh

# ۴) اعمال Migration
uv run python manage.py migrate

# ۵) ساخت فایل‌های CSS/JS
npm install
npm run build

# ۶) داده‌های نمونه (اختیاری اما توصیه‌شده)
uv run python manage.py seed_demo_data

# ۷) اجرا
uv run python manage.py runserver
```

فروشگاه روی <http://127.0.0.1:8000/> و پنل مدیریت روی <http://127.0.0.1:8000/admin/> در دسترس است.

> **نکته درباره‌ی PyPI:** اگر دسترسی به `pypi.org` مسدود است، از آینه استفاده کنید:
> ```bash
> export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
> export UV_LINK_MODE=copy
> uv sync
> ```

---

## اجرا با Docker (پیشنهادی)

کل پروژه به‌همراه **تمام پیش‌نیازها** (Python 3.14، Node، MariaDB، Tailwind، Gunicorn)
داکرایز شده است. با یک دستور بالا می‌آید و برای انتقال به هاست اصلی هم همین روش کافی است.

### پیش‌نیاز

- Docker Engine 24+ و Docker Compose v2
- دسترسی اجرا به کاربر `docker` (یا `sudo`)

```bash
docker --version
docker compose version
```

### راه‌اندازی (توسعه — با Hot-Reload)

> **نکته:** `docker compose` به‌طور خودکار همان فایل `.env` پروژه را می‌خواند؛
> پس نیازی به ساخت فایل جدید نیست و **هرگز `.env` واقعی را بازنویسی نکنید**.
> فقط یک بار مطمئن شوید این سه مقدار پُر هستند:

```bash
# بررسی مقادیر الزامی (باید خالی یا change-me نباشند)
grep -E "^(SECRET_KEY|PAYMENT_SECRET|DATABASE_PASSWORD)=" .env

# اگر لازم بود ویرایش کنید
nano .env
```

> اگر `.env` را ندارید، از `.env.example` یک نسخه بگیرید و **همه‌ی مقادیر `change-me`
> را با مقدار واقعی عوض کنید**؛ وگرنه هم Django و هم Docker با خطای
> `Access denied for user 'shop'@'localhost'` متوقف می‌شوند.
> فایل `.docker.env.example` فقط **مرجع** است و نباید روی `.env` نوشته شود.

```bash
# بالا آوردن کل سرویس‌ها
docker compose up -d --build

# (اختیاری) ساخت داده‌های نمونه
docker compose exec web python manage.py seed_demo_data
```

سرویس‌ها:

| سرویس | توضیح | آدرس |
| --- | --- | --- |
| `web` | Django + `runserver` (توسعه) | <http://localhost:8000/> |
| `db` | MariaDB 11.4 با Volume دائمی | `localhost:3307` |
| `phpmyadmin` | فقط با `--profile tools` | <http://localhost:8080/> |

```bash
docker compose ps                  # وضعیت سرویس‌ها
docker compose logs -f web         # لاگ وب
docker compose logs -f db          # لاگ دیتابیس
docker compose --profile tools up -d phpmyadmin   # phpMyAdmin
```

### اجرای دستورهای مدیریتی

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py test
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py shell
```

### استقرار Production با Docker

```bash
# حالت Production از فایل docker-compose.yml استفاده می‌کند
# (بدون Override: Gunicorn به‌جای runserver، و DJANGO_SETTINGS_MODULE=production)
docker compose -f docker-compose.yml build
docker compose -f docker-compose.yml up -d

# ساخت ابرکاربر
docker compose -f docker-compose.yml exec web python manage.py createsuperuser
```

نکات مهم Production:

| متغیر | مقدار پیش‌فرض | توضیح |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | در فایل اصلی قاطع ست شده؛ `check --deploy` بدون هشدار |
| `SECRET_KEY` / `PAYMENT_SECRET` / `DATABASE_PASSWORD` | **الزامی** | بدون آن‌ها اجرا نمی‌شود |
| `SECURE_SSL_REDIRECT` | `True` | اگر Reverse-Proxy ندارید `False` کنید |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` | فقط با HTTPS |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | دامنه‌ی واقعی | با `https://` |
| `MIGRATE_ON_START` | `1` | اگر Migration را جداگانه اجرا می‌کنید `0` |

> ⚠️ اگر پشت **Nginx/Caddy با HTTPS** هستید، `X-Forwarded-Proto` را حتماً پاس دهید؛
> در غیر این صورت `SECURE_SSL_REDIRECT` باعث حلقه‌ی Redirect می‌شود.

### ساختار فایل‌های Docker

```
Dockerfile                  # سه مرحله: assets (Node) → builder (uv) → runtime
docker-compose.yml          # Production: web + db
docker-compose.override.yml # توسعه: Hot-Reload، runserver، DEBUG=True
.dockerignore
.docker.env.example         # نمونه پیکربندی Docker Compose
docker/
├── entrypoint.sh           # انتظار برای DB → collectstatic → migrate → اجرا
└── db/
    ├── init/01-load-timezones.sh   # بارگذاری جدول‌های Timezone (لازم برای date_hierarchy)
    └── conf.d/shop.cnf             # utf8mb4 + READ-COMMITTED + InnoDB
```

### مهاجرت از محیط محلی به Docker

```bash
# بکاپ‌گیری از دیتابیس محلی
mysqldump -h 127.0.0.1 -u shop -p clothing_shop > backup.sql

# ورود به کانتینر دیتابیس و ورود داده‌ها
docker compose exec -T db mariadb -u root -p"your-root-pass" clothing_shop < backup.sql

# انتقال تصاویر آپلودشده
docker compose cp media/. web:/app/media/
```

### عیب‌یابی Docker

| مشکل | علت | راه‌حل |
| --- | --- | --- |
| `کلید SECRET_KEY تنظیم نشده است` | `.env` خالی است | مقادیر الزامی را پر کنید |
| `entrpoint: اتصال به دیتابیس ناموفق بود` | `DATABASE_HOST` نادرست | باید دقیقاً `db` باشد |
| خطای مجوز روی `media/` | مالک ریشه است | `sudo chown -R $USER:$USER media logs` |
| `port is already allocated` | پورت 8000/3307 اشغال است | `WEB_PORT`/`DB_PORT` را در `.env` عوض کنید |
| CSS قدیمی نمایش داده می‌شود | فایل‌های static کهنه‌اند | `docker compose build --no-cache web` |
| `mounts denied: ... is not shared from the host` | مسیر پروژه در File Sharing Docker Desktop نیست (معمولاً وقتی روی درایو خارجی/NTFS است) | Docker Desktop → Settings → Resources → **File Sharing** → مسیر پروژه را اضافه کنید → Apply & Restart |
| `stat /app/docker/entrypoint.sh: no such file` | کانتینر قبل از اصلاح File Sharing ساخته شده و Mount ناقص مانده است | `docker compose up -d --force-recreate web` |
| `TLS handshake timeout` هنگام کشیدن `ghcr.io/astral-sh/uv` | دسترسی به GHCR در برخی شبکه‌ها ناپایدار است | **رفع شده:** uv اکنون با pip از PyPI نصب می‌شود؛ در صورت تکرار، `docker compose build --no-cache` |
| `Unable to locate package default-libmysqlclient-runtime` | نام پکیج اشتباه بود | **رفع شده:** از `libmariadb3` استفاده می‌شود |

---

## ساخت دیتابیس و اتصال phpMyAdmin

### ساخت خودکار

اسکریپت زیر دیتابیس، کاربر و جدول‌های Timezone را می‌سازد:

```bash
bash scripts/mysql_dev.sh
```

با مقادیر پیش‌فرض:

| مقدار | مقدار |
| --- | --- |
| نام دیتابیس | `clothing_shop` |
| کاربر | `shop` |
| رمز | `shop_secret_2026` |
| هاست | `127.0.0.1:3306` |

هر کدام با متغیرهای `DATABASE_NAME`، `DATABASE_USER`، `DATABASE_PASSWORD`، `DATABASE_HOST` و `DATABASE_PORT` قابل تغییر است.

### ساخت دستی

```sql
CREATE DATABASE clothing_shop
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'shop'@'127.0.0.1' IDENTIFIED BY 'shop_secret_2026';
GRANT ALL PRIVILEGES ON clothing_shop.* TO 'shop'@'127.0.0.1';
FLUSH PRIVILEGES;
```

### اتصال به phpMyAdmin

> ### ⚠️ هشدار مهم درباره‌ی پورت 3306
>
> اجرای دستور `/opt/lampp/lampp start` سرویس **MariaDB خودِ XAMPP** را روی پورت `3306`
> بالا می‌آورد و جای دیتابیس پروژه را می‌گیرد. در این حالت Django با خطای زیر مواجه می‌شود:
>
> ```
> MySQLdb.OperationalError: (1045, "Access denied for user 'shop'@'localhost'")
> ```
>
> چون کاربر `shop` در دیتابیس XAMPP وجود ندارد. **راه‌حل‌ها:**
>
> - **(پیشنهادی)** از phpMyAdmin داکر استفاده کنید (بخش زیر) و اصلاً LAMPP را اجرا نکنید.
> - یا پورت MySQL داخل XAMPP را عوض کنید: در `/opt/lampp/etc/my.cnf` مقدار `port=3306`
>   را به `port=3308` تغییر دهید و سپس `/opt/lampp/lampp start` را اجرا کنید.
>   (عمداً `3308` و نه `3307`، چون `3307` پیش‌فرض پورت دیتابیس Docker است.)
> - یا فقط Apache را بالا بیاورید و MySQL آن را خاموش نگه دارید:
>   ```bash
>   /opt/lampp/lampp startapache      # فقط وب‌سرور
>   /opt/lampp/lampp stopmysql        # دیتابیس XAMPP خاموش بماند
>   ```
>
> برای تشخیص اینکه چه کسی پورت 3306 را گرفته:
> ```bash
> ss -ltnp | grep 3306
> ```

#### گزینه ۱ — phpMyAdmin با Docker (پیشنهادی)

کانینر phpMyAdmin از قبل در `docker-compose.yml` تعریف شده و با `profile` فعال می‌شود؛
**بدون اینکه پورت 3306 دیتابیس پروژه را بشکند**:

```bash
docker compose --profile tools up -d phpmyadmin
```

سپس <http://localhost:8080/> را باز کنید و با کاربر `shop` و رمز آن وارد شوید.

#### گزینه ۲ — phpMyAdmin دستی (بدون LAMPP)

اگر phpMyAdmin را خودتان نصب کرده‌اید، فقط کافی است هاست را `127.0.0.1` بگذارید
(نباید `localhost` باشد تا به‌جای سوکتِ XAMPP، از TCP استفاده شود):

```php
$i = 0;
$i++;
$cfg['Servers'][$i]['host'] = '127.0.0.1';
$cfg['Servers'][$i]['port'] = '3306';
$cfg['Servers'][$i]['auth_type'] = 'cookie';
$cfg['Servers'][$i]['AllowNoPassword'] = false;
$cfg['Servers'][$i]['hide_db'] = '^(information_schema|performance_schema|mysql|sys)$';
```

> phpMyAdmin از سوکت MySQL هم پشتیبانی می‌کند؛ اگر سرور MySQL شما فقط با سوکت در دسترس است،
> در فیلد Host مسیر سوکت را بگذارید: `/var/run/mysqld/mysqld.sock`
>
> ⚠️ هرگز مسیر سوکتِ XAMPP (مثل `/opt/lampp/var/mysql/mysql.sock`) را وارد نکنید؛
> دیتابیس پروژه روی آن سوکت نیست.

---

## اجرای پروژه

همه‌ی دستورات با `uv` اجرا می‌شوند:

```bash
uv run python manage.py runserver          # سرور توسعه
uv run python manage.py migrate            # اعمال Migration
uv run python manage.py makemigrations     # ساخت Migration جدید
uv run python manage.py check              # بررسی سلامت پیکربندی
uv run python manage.py collectstatic      # جمع‌آوری Static (Production)
uv run python manage.py shell              # Shell تعاملی
```

---

## داده‌های نمونه

```bash
uv run python manage.py seed_demo_data
```

این دستور می‌سازد:

- ۱۳ دسته‌بندی (با ساختار سلسله‌مراتبی)
- ۸ رنگ و ۹ سایز
- ۱۶ محصول واقعی با چند واریانت و تصاویر Placeholder تولیدشده با Pillow
- ۵ تخفیف فعال
- سوپروزر: شماره `09120000000`، رمز `admin1234`
- کاربر نمونه: شماره `09120000001`
- دو سفارش نمونه (یکی پرداخت‌شده، یکی در انتظار پرداخت)

برای پاک‌سازی کامل و شروع دوباره:

```bash
uv run python manage.py flush
uv run python manage.py seed_demo_data
```

### ورود به حساب کاربری

۱. به `/accounts/login/` بروید و شماره را وارد کنید.
۲. کد OTP در **کنسول سرور** چاپ می‌شود (Provider پیامک از نوع `console`).
۳. کد را در صفحه‌ی تأیید وارد کنید.

---

## تست‌ها

```bash
uv run python manage.py test                     # همه تست‌ها
uv run python manage.py test products            # فقط یک اپ
uv run python manage.py test payments.tests.CallbackTests   # یک کلاس
uv run python manage.py test --keepdb            # بدون ساخت دیتابیس آزمایشی جدید
```

تست‌ها روی دیتابیس آزمایشی جداگانه‌ای به نام `test_clothing_shop` اجرا می‌شوند و داده‌های اصلی شما دست نمی‌خورند.

پوشش **۲۰۸ تست** در ۷ اپ: اعتبارسنجی‌ها، Rate Limiter، سناریوهای OTP (کد درست، غلط، منقضی، قفل تلاش، ارقام فارسی)، قیمت‌گذاری (درصدی، ثابت، منقضی، سقف)، جلوگیری از Overselling، Snapshot سفارش، لغو و انقضا، امضا و Verify درگاه، Idempotency Callback و سناریوی کامل خرید سرانجام‌به‌سرانجام.

---

## ساخت فایل‌های CSS/JS

```bash
npm install          # فقط یک بار
npm run build        # ساخت app.css + کپی htmx/alpine/فونت‌ها
npm run watch        # ساخت ممتد هنگام توسعه
```

خروجی در `static/` قرار می‌گیرد:

```
static/
├── css/app.css
├── vendor/htmx.min.js
├── vendor/alpine.min.js
└── fonts/Vazirmatn-*.woff2
```

**نکته:** کلاس‌های سفارشی در Tailwind v4 باید با `@utility` تعریف شوند، نه `@layer components`. فایل منبع `assets/css/app.css` است.

---

## پیکربندی پیامک

ارسال پیامک پشت یک Interface قرار دارد؛ هسته‌ی OTP هیچ وابستگی‌ای به Provider خاصی ندارد.

```env
SMS_PROVIDER=console          # توسعه: کد در کنسول چاپ می‌شود
# SMS_PROVIDER=memory         # تست: پیامک‌ها در حافظه نگه داشته می‌شوند
# SMS_PROVIDER=myapp.sms.KavenegarProvider   # سرویس واقعی
```

تنظیمات مرتبط:

```env
OTP_LENGTH=5
OTP_TTL_SECONDS=180
OTP_MAX_ATTEMPTS=5
OTP_RESEND_SECONDS=90
OTP_RESEND_LIMIT_PER_HOUR=5
```

### افزودن سرویس پیامک واقعی

کافی است یک کلاس با همین Interface بسازید و نام آن را در `SMS_PROVIDER` قرار دهید؛ نیازی به تغییر هسته‌ی احراز هویت نیست.

```python
# myapp/sms.py
from accounts.sms.base import SMSProvider


class KavenegarProvider(SMSProvider):
    name = "kavenegar"

    def send(self, phone_number: str, message: str) -> None:
        # فراخوانی HTTP به API کاوه‌نگار
        ...
```

اگر مسیر کلاس اشتباه باشد، به‌جای خطای 500 پیام روشن «ارائه‌دهنده پیامک پیدا نشد» نمایش داده می‌شود.

---

## پیکربندی درگاه پرداخت

```env
PAYMENT_GATEWAY=mock
PAYMENT_SECRET=change-me-to-a-long-random-string
ORDER_RESERVATION_MINUTES=30
```

> درگاه Mock فقط وقتی `DEBUG=True` است کار می‌کند. با `DEBUG=False` به‌طور خودکار غیرفعال می‌شود.

### افزودن درگاه واقعی

```python
# payments/gateways/zarinpal.py
from .base import PaymentGateway, PaymentRequest, VerificationResult


class ZarinpalGateway(PaymentGateway):
    name = "zarinpal"

    def create_payment(self, order, request=None) -> PaymentRequest:
        ...

    def verify_payment(self, payment, callback_data=None) -> VerificationResult:
        ...
```

```env
PAYMENT_GATEWAY=payments.gateways.zarinpal.ZarinpalGateway
PAYMENT_MERCHANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PAYMENT_SECRET=یک رشته تصادفی بلند
```

**قوانینی که باید رعایت شود:**

- مبلغ تراکنش فقط از `order.total` خوانده شود، هرگز از Client
- Verify سمت سرور انجام شود؛ Query به‌تنهایی کافی نیست
- `process_callback` ایدمپوتنت است؛ پردازش مجدد اثری ندارد
- هیچ اطلاعات کارتی ذخیره نشود

---

## استقرار Production

### ۱) پیکربندی

```bash
cp .env.example .env
```

```env
DJANGO_SETTINGS_MODULE=config.settings.production
DEBUG=False
SECRET_KEY=<یک کلید تصادفی حداقل ۵۰ کاراکتری>
ALLOWED_HOSTS=shop.example.com
CSRF_TRUSTED_ORIGINS=https://shop.example.com

DATABASE_PASSWORD=<رمز قوی>

PAYMENT_GATEWAY=payments.gateways.zarinpal.ZarinpalGateway
PAYMENT_SECRET=<یک رشته تصادفی بلند>

SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
```

`config/settings/production.py` در صورت ضعیف‌بودن `SECRET_KEY`، خالی‌بودن `ALLOWED_HOSTS` یا تنظیم‌نشدن رمز دیتابیس، بالا آمدن را متوقف می‌کند.

### ۲) دیتابیس

```sql
-- کاربر اختصاصیِ فقط برای این پروژه
CREATE USER 'shop_prod'@'127.0.0.1' IDENTIFIED BY '<رمز قوی>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER
  ON clothing_shop.* TO 'shop_prod'@'127.0.0.1';
FLUSH PRIVILEGES;
```

> در Production هرگز دسترسی `ALL ON *.*` ندهید. دسترسی سراسری فقط برای محیط توسعه است تا بتواند دیتابیس آزمایشی تست‌ها را بسازد.

```bash
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
```

### ۳) زمان‌بندی انقضای سفارش‌ها

سفارش‌های پرداخت‌نشده باید دوره‌ای لغو و موجودی‌شان آزاد شود:

```bash
*/5 * * * * cd /path/to/shop && /path/to/.venv/bin/python manage.py expire_pending_orders
```

### ۴) سرو با Gunicorn

```bash
uv sync --extra production
uv run gunicorn config.wsgi:application --workers 3 --bind 127.0.0.1:8000
```

یا ASGI:

```bash
uv run gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000
```

### ۵) Nginx (پشتیبان Reverse-Proxy)

```nginx
server {
    listen 80;
    server_name shop.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name shop.example.com;

    ssl_certificate     /etc/letsencrypt/live/shop.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/shop.example.com/privkey.pem;

    client_max_body_size 20m;   # آپلود تصاویر

    location /static/ {
        alias /path/to/shop/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location /media/ {
        alias /path/to/shop/media/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> `X-Forwarded-For` لازم است تا Rate Limiting مبتنی بر IP درست کار کند.

### ۶) Cache برای چند-پردازشی

با چند Worker، شمارنده‌های Rate Limit باید مشترک باشند:

```bash
uv add django-redis
```

```env
CACHE_URL=redis://127.0.0.1:6379/1
```

بدون Redis، `LocMemCache` برای هر Worker جداگانه شمارش می‌کند و سقف درخواست‌ها چند برابر می‌شود.

---

## عیب‌یابی

| مشکل | راه‌حل |
| --- | --- |
| `django.db.utils.OperationalError: Can't connect` | MySQL/MariaDB در حال اجراست؟ `DATABASE_HOST` و `DATABASE_PORT` درست‌اند؟ |
| `Access denied; you need (at least one of) the CREATE privilege(s)` | هنگام اجرای تست‌ها دسترسی ساخت دیتابیس لازم است. در توسعه `GRANT ALL PRIVILEGES ON *.*` و در Production یک دیتابیس آزمایشی جداگانه بسازید. |
| `Database returned an invalid datetime value` | جدول‌های Timezone دیتابیس خالی‌اند. `bash scripts/mysql_dev.sh` را اجرا کنید یا `mysql_tzinfo_to_sql /usr/share/zoneinfo \| mysql mysql` را دستی اجرا کنید. |
| صفحه‌ی محصول CSS ندارد | `npm install && npm run build` را اجرا کنید. |
| کد OTP چاپ نمی‌شود | `SMS_PROVIDER=console` در `.env` باشد و سرور را در ترمینال اجرا کنید نه در پس‌زمینه. |
| `DisallowedHost: testserver` | تست‌ها را با `manage.py test` اجرا کنید نه اسکریپت دستی. |
| درگاه Mock خطای «فقط در محیط Development» می‌دهد | `DEBUG=True` باشد. با `DEBUG=False` این درگاه عمداً غیرفعال است. |
| دکمه‌ی «افزودن به سبد» غیرفعال است | رنگ و سایز را انتخاب کنید؛ محصول موجودی داشته باشد. |
| `npm` دسترسی ندارد | `node_modules` را پاک کنید و دوباره `npm install` بزنید. |

---

## ساختار پروژه

```
├── config/                  پیکربندی (settings/urls/wsgi/asgi)
├── common/                  اعتبارسنجی‌ها، Rate Limiter، فیلترهای قالب، داشبورد
├── accounts/                کاربر، OTP، پروفایل، آدرس، Provider های پیامک
├── products/                محصول، دسته‌بندی، رنگ، سایز، واریانت، تخفیف، قیمت‌گذاری
├── cart/                    سبد خرید و سرویس‌های آن
├── orders/                  سفارش، گذار وضعیت، ارسال، انقضا
├── payments/                درگاه‌ها، تراکنش، Callback
├── store/                   صفحات فروشگاه و داده‌های نمونه
├── templates/               همه‌ی قالب‌ها
├── assets/css/              منبع CSS (Design System)
├── static/                  خروجی build
└── scripts/                 اسکریپت‌های کمکی
```

جزئیات معماری در [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md) آمده است.

---

## مجوز

پروژه‌ی آموزشی و عملی. تمام حقوق محفوظ است.
