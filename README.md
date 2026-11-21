# 🛍️ Clothing E-Commerce Store

A full-featured clothing e-commerce application built with **Django 6.1 and MySQL/MariaDB**, featuring a fully Persian, right-to-left (RTL) storefront and administration panel.

It includes phone-number authentication with one-time passwords (OTP), product variants, shopping carts, inventory-safe order creation, and redirect-based payments with server-side verification.

---
## 📌 Features

- Persian RTL storefront and administration panel
- Homepage featuring selected and discounted products
- Product catalog with filters for color, size, price range, availability, and discounts, plus sorting
- Search across product names, descriptions, and category names
- Categories, subcategories, Persian URL slugs, product details, and related products
- Product variants based on color and size, with variant-level inventory tracking
- Passwordless sign-in using a phone number and one-time password (OTP)
- OTP expiration, attempt limits, resend cooldowns, hourly request limits, and cache-based rate limiting
- HMAC-SHA256 OTP hashing; plaintext OTP values are not stored
- Automatic merging of a guest cart into the signed-in user's cart
- Customer profile and address management
- Session-based carts for guests and database-backed carts for authenticated users
- HTMX-powered cart updates without a full page reload
- Transactional order creation using row locks and conditional SQL updates to prevent overselling
- Order snapshots that preserve product, variant, price, and shipping-address details at checkout
- Backend-only financial calculations using Python `Decimal`, not `float`
- Shipping options, free shipping above a configured threshold, and customer order notes
- Expiration of unpaid orders and release of reserved inventory
- Hosted/redirect-based payment flow; card details are not stored or processed by this application
- Zibal payments with server-side verification, amount/order validation, and idempotent callbacks
- The official Zibal test merchant for development; real merchant credentials for production
- Admin dashboard for sales, orders, and low-stock products
- Controlled order-status transitions and management of products, categories, variants, discounts, customers, addresses, and transactions
- Security headers, Content Security Policy (CSP) with nonces, input validation, and brute-force protection

---
## 🛠️ Technologies Used

- **Python 3.14** and **Django 6.1** — backend and application framework
- **uv** — Python dependency and environment management
- **MySQL 8+ / MariaDB 10.6+** — relational database (`mysqlclient` driver)
- **Tailwind CSS v4** — styling and design system
- **HTMX** — asynchronous interface updates
- **Alpine.js** — lightweight client-side interactions
- **Vazirmatn** — Persian typeface
- **WhiteNoise** — static-file serving
- **jdatetime** — Jalali date support
- **Pillow** — image processing and generated demo images
- **Node.js and npm** — frontend asset building only
- **Docker and Docker Compose** — containerized development and deployment
- **Gunicorn** — production WSGI server

React, Next.js, and Node.js are not used to serve the application at runtime. Node.js is only needed to build CSS and copy vendor assets.

---
## 🚀 How It Works

1. Customers browse products, categories, discounts, and available color/size variants.
2. A customer adds items to a session-based guest cart or their authenticated cart.
3. At checkout, the backend validates the selected variants and recalculates prices and totals using `Decimal`.
4. The order service locks the relevant inventory rows and updates stock inside a database transaction to prevent concurrent purchases from overselling available items.
5. The order stores a snapshot of the product name, selected variant, price, and shipping address as they were at checkout.
6. The customer is redirected to the configured payment gateway. The application does not trust query parameters alone; payment status must be verified server-side.
7. Payment verification runs before acquiring database row locks. Its result is then applied atomically to the current order and payment state; duplicate callbacks do not repeat fulfillment or inventory operations.
8. An unresolved successful receipt in manual review blocks another receipt from automatically paying the order. Its reserved stock stays held until review is resolved; other eligible unpaid orders continue to expire normally.
9. Administrators manage catalog data, discounts, customers, transactions, and order-status transitions through Django's administration interface.

---
## ⚙️ Installation

### Prerequisites

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/)
- Node.js and npm
- MySQL 8+ or MariaDB 10.6+

### 1. Clone the repository

```bash
git clone https://github.com/your-username/your-repository.git
cd your-repository
```

Replace the example repository URL with the URL of your GitHub repository.

---

### 2. Install Python dependencies and configure the environment

```bash
uv sync
cp .env.example .env
```

Open `.env` and set the values required by your local environment. In particular, do not leave required secrets such as `SECRET_KEY` or `DATABASE_PASSWORD` empty or set to example values. Configure `ZIBAL_MERCHANT` from the Zibal panel for production.

---

### 3. Create the database

```bash
bash scripts/mysql_dev.sh
```

This development helper creates the database, database user, and required MySQL time-zone data. Its defaults are documented in the [Database Setup](#-database-setup-and-phpmyadmin) section below.

---

### 4. Apply migrations and build frontend assets

```bash
uv run python manage.py migrate
npm install
npm run build
```

---

### 5. Load optional demo data

```bash
uv run python manage.py seed_demo_data
```

This creates example categories, products, variants, discounts, users, and orders. The command also creates a development-only administrator account; see [Demo Data](#-demo-data) before using it.

---

### 6. Start the development server

```bash
uv run python manage.py runserver
```

The storefront will be available at <http://127.0.0.1:8000/> and the Django administration panel at <http://127.0.0.1:8000/admin/>.

> **Package index note:** If `pypi.org` is inaccessible on your network, configure an accessible package index before running `uv sync`. For example:
>
> ```bash
> export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
> export UV_LINK_MODE=copy
> uv sync
> ```
>
> Use a package index you trust and that is appropriate for your environment.

---
## 📸 Output Example

After starting the application, you can open these pages locally:

- **Storefront:** <http://127.0.0.1:8000/>
- **Administration panel:** <http://127.0.0.1:8000/admin/>
- **Phone-number sign-in:** <http://127.0.0.1:8000/accounts/login/>

With `SMS_PROVIDER=console`, development OTP messages are printed to the server terminal. After signing in, users can browse products, choose variants, manage their cart, and proceed through checkout using the configured payment gateway.

---
## ❗ Notes

- The interface is Persian and RTL by design.
- The official test merchant `ZIBAL_MERCHANT=zibal` is accepted only with `DEBUG=True`.
- With the default `SMS_PROVIDER=console`, OTP codes are printed to the server terminal rather than sent as real SMS messages.
- Demo account credentials and demo database passwords are for local development only. Change or remove them before exposing any deployment to a network.
- Keep real secrets in environment variables. Never commit your real `.env` file, payment credentials, or production database password.
- Zibal requires valid merchant credentials and a registered callback domain. A browser redirect is followed by server-side verification before an order is marked paid.
- Tailwind CSS v4 custom utilities should be declared with `@utility`; the source stylesheet is `assets/css/app.css`.
- The project uses Python `Decimal` for monetary calculations to avoid floating-point rounding issues.

---
## 🐳 Docker Setup

Docker is available for running the application with MariaDB and the required runtime/build dependencies.

### Prerequisites

- Docker Engine 24 or later
- Docker Compose v2

Check your installation:

```bash
docker --version
docker compose version
```

### Development setup

Docker Compose reads the project's `.env` file. If it does not exist, create it from `.env.example` and replace all example values before starting the application.

```bash
cp .env.example .env
```

Check the required settings:

```bash
uv run python manage.py check
```

Start the development services:

```bash
docker compose up -d --build
```

Optionally load demo data:

```bash
docker compose exec web python manage.py seed_demo_data
```

The development Compose override uses Django's development server and hot reloading. Do not use the development configuration as a public production deployment.

### Docker services

| Service | Purpose | Local address |
| --- | --- | --- |
| `web` | Django development server | <http://localhost:8000/> |
| `db` | MariaDB 11.4 with a persistent volume | `localhost:3307` |
| `phpmyadmin` | Optional database administration tool | <http://localhost:8080/> |

Start phpMyAdmin with its Compose profile:

```bash
docker compose --profile tools up -d phpmyadmin
```

Useful service commands:

```bash
docker compose ps
docker compose logs -f web
docker compose logs -f db
```

### Django management commands in Docker

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py test
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py shell
```

### Docker production configuration

MiniKandoo production uses the base and production Compose files through `scripts/compose-production`, which loads `.env.production` and omits the development override. Review existing server changes, backup storage permissions and retention dry-run before deployment:

```bash
scripts/compose-production config --quiet
scripts/compose-production build web
scripts/compose-production --profile backup-admin build db-backup db-backup-provision
scripts/compose-production ps
```

Before starting the backup worker or taking a manual backup with the new image, configure its separate database account and explicitly provision it against the healthy database:

```bash
scripts/compose-production --profile backup-admin run --rm -T db-backup-provision
```

The long-running worker receives only `BACKUP_DB_USER` and `BACKUP_DB_PASSWORD`. Root credentials are confined to the short-lived provisioning service. For an existing server, verify a fresh backup, pause writers in the approved maintenance window and review the migration plan before starting updated services. The [deployment guide](docs/deployment-production-fa.md) distinguishes this sequence from initializing a fresh database; these commands require a separately approved production deployment.

Backups run inside the same Compose project on UTC half-hour boundaries, with an initial backup after startup. Retention defaults to a preview. When explicitly set to `apply`, it can remove eligible old managed pairs before a capacity-blocked dump while preserving the required recent and daily recovery points. See [backup operations](docs/production-database-backups.md) for capacity, verification, provisioning and restore procedures.

Important production settings include:

| Variable | Expected configuration | Purpose |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | Loads production settings |
| `SECRET_KEY` | Required, strong random value | Django signing and security |
| `ZIBAL_MERCHANT` | Required, real Zibal merchant in production | Zibal API authentication |
| `ZIBAL_TIMEOUT_SECONDS` | `10` by default | HTTP timeout in seconds |
| `ZIBAL_PAID_AT_TIMEZONE` | Empty until officially confirmed | IANA timezone for gateway timestamps without an offset |
| `SITE_URL` | Storefront origin; HTTPS in production | Builds the payment callback URL |
| `DATABASE_PASSWORD` | Required, strong password | Database authentication |
| `BACKUP_DB_USER` | Required, separate backup account | Restricted MariaDB dump user |
| `BACKUP_DB_PASSWORD` | Required, strong independent password | Backup account authentication; no root fallback |
| `ALLOWED_HOSTS` | Your real domain | Allowed host names |
| `CSRF_TRUSTED_ORIGINS` | Full origins, including `https://` | CSRF protection behind the domain |
| `SECURE_SSL_REDIRECT` | `True` when HTTPS is configured | Redirects HTTP requests to HTTPS |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` when HTTPS is configured | Sends cookies only over HTTPS |
| `MIGRATE_ON_START` | `0` in the production wrapper configuration | Apply reviewed migrations once in a maintenance window |
| `BACKUP_HOST_DIR` | `/root/minikandoo-backups` | Persistent bind mount for the Compose backup service |
| `BACKUP_RETENTION_MODE` | `dry-run` until reviewed, then `apply` | Enable safe managed-file retention after the initial review |

If Nginx or Caddy terminates HTTPS in front of the application, configure the reverse proxy to forward the original scheme correctly. Otherwise, `SECURE_SSL_REDIRECT` can cause redirect loops. Review the proxy and Django security settings together before deployment.

### Docker-related files

```text
Dockerfile
 docker-compose.yml
 docker-compose.override.yml
 .dockerignore
 .docker.env.example
 docker/
 ├── entrypoint.sh
 └── db/
     ├── init/01-load-timezones.sh
     └── conf.d/shop.cnf
```

The entrypoint waits for the database, collects static files, runs configured startup tasks, and launches the application. The database configuration enables `utf8mb4`, `READ-COMMITTED`, and InnoDB settings.

### Moving a development database into Docker

Back up the local database first:

```bash
mysqldump -h 127.0.0.1 -u shop -p clothing_shop > backup.sql
```

Import the backup into the container, substituting your configured database credentials:

```bash
docker compose exec -T db mariadb -u root -p clothing_shop < backup.sql
```

These import commands are for an isolated development database. For production, use the approved backup and restore procedure in [database backup operations](docs/production-database-backups.md). Copy uploaded media files when applicable:

```bash
docker compose cp media/. web:/app/media/
```

> The import command will prompt for a password where supported by the client. Do not place real passwords directly in shell history or public documentation.

---
## 🗄️ Database Setup and phpMyAdmin

### Automatic database setup

Run the helper script in a local development environment:

```bash
bash scripts/mysql_dev.sh
```

Its documented defaults are:

| Setting | Development default |
| --- | --- |
| Database name | `clothing_shop` |
| Database user | `shop` |
| Database password | `shop_secret_2026` — development only |
| Host and port | `127.0.0.1:3306` |

You can override these values with `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, and `DATABASE_PORT`. Do not reuse the documented development password in production.

### Manual database creation

The following is an example for a local development database. Replace the password with a strong local value and keep it consistent with `.env`:

```sql
CREATE DATABASE clothing_shop
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'shop'@'127.0.0.1' IDENTIFIED BY '<strong-development-password>';
GRANT ALL PRIVILEGES ON clothing_shop.* TO 'shop'@'127.0.0.1';
FLUSH PRIVILEGES;
```

### Connect with Docker phpMyAdmin

Start the service and open <http://localhost:8080/>:

```bash
docker compose --profile tools up -d phpmyadmin
```

Sign in using the database host and credentials configured for your Compose setup.

### Local phpMyAdmin configuration

If you run phpMyAdmin locally, use `127.0.0.1` as the host when you need a TCP connection to the local MySQL/MariaDB service:

```php
$i = 0;
$i++;
$cfg['Servers'][$i]['host'] = '127.0.0.1';
$cfg['Servers'][$i]['port'] = '3306';
$cfg['Servers'][$i]['auth_type'] = 'cookie';
$cfg['Servers'][$i]['AllowNoPassword'] = false;
$cfg['Servers'][$i]['hide_db'] = '^(information_schema|performance_schema|mysql|sys)$';
```

A local MySQL installation may also be accessed over its configured Unix socket, for example `/var/run/mysqld/mysqld.sock`. Use the socket path belonging to the database instance that actually hosts this project's database.

### Port 3306 conflicts with XAMPP/LAMPP

On Linux, starting XAMPP/LAMPP may start its own MariaDB service on port `3306`. Django can then connect to the wrong database instance and report an authentication error for the `shop` user.

Check which process is listening on that port:

```bash
ss -ltnp | grep 3306
```

Possible solutions are to use the Docker database and phpMyAdmin, configure XAMPP's MySQL/MariaDB to use a different port such as `3308`, or start only XAMPP's web server while leaving its database stopped. Make sure the database address in `.env` points to the intended service.

---
## 📦 Demo Data

Populate the development database with sample content:

```bash
uv run python manage.py seed_demo_data
```

The command creates:

- 13 categories in a hierarchy
- 8 colors and 9 sizes
- 16 sample products with multiple variants and placeholder images generated with Pillow
- 5 active discounts
- A sample administrator account: phone number `09120000000`, password `admin1234`
- A sample customer account: phone number `09120000001`
- 2 sample orders: one paid and one awaiting payment

The listed credentials are development defaults only. Change or remove them before deploying, and do not use them for any real account.

To clear the database and reseed it from scratch, run the following command **only if you intend to delete all data in the configured database**:

```bash
uv run python manage.py flush
uv run python manage.py seed_demo_data
```

When `SMS_PROVIDER=console`, sign in at `/accounts/login/` and retrieve the one-time code from the server terminal.

---
## 🧪 Tests

Run the Django test suite with:

```bash
uv run python manage.py test
```

Run specific apps or test classes:

```bash
uv run python manage.py test products
uv run python manage.py test payments.tests.CallbackTests
uv run python manage.py test --keepdb
```

The suites cover validation, rate limiting, OTP, pricing, overselling, order snapshots, expiration, durable gateway requests, receipt reconciliation, idempotent callbacks and backup/restore. Use the isolated test configuration for local validation:

```bash
PYTHONPATH=scripts/qa uv run python manage.py test --settings=shop_test_settings --noinput
uv run python scripts/qa/test_backup_stack.py
```

The second command runs the full suite with real MariaDB locks and the isolated Compose backup/restore harness. GitHub Actions workflows were removed at the project owner's request; these checks are run locally. Payment APIs are mocked, and the harness uses synthetic credentials, unique networks and temporary database storage.

Tests use a separate database named `test_clothing_shop`. Ensure the database account used for local testing has the permissions required for that test database. Avoid granting broad global database privileges to production accounts merely to make tests pass.

---
## 🎨 Building CSS and JavaScript Assets

Install frontend dependencies and build the static assets:

```bash
npm install
npm run build
```

During frontend development, rebuild assets automatically when files change:

```bash
npm run watch
```

The generated files are placed under `static/`:

```text
static/
├── css/app.css
├── vendor/htmx.min.js
├── vendor/alpine.min.js
└── fonts/Vazirmatn-*.woff2
```

The main CSS source file is `assets/css/app.css`. In Tailwind CSS v4, custom utilities should be declared with `@utility` rather than `@layer components`.

---
## 📱 SMS Provider Configuration

SMS delivery is abstracted behind a provider interface (`SMSProvider`). The OTP authentication flow does not depend directly on a specific SMS vendor.

Configure the active provider in `.env`:

```env
SMS_PROVIDER=console
# SMS_PROVIDER=memory
# SMS_PROVIDER=sms_ir
# SMS_PROVIDER=myapp.sms.CustomProvider
```

- `console`: Prints OTP messages to the development server terminal. Blocked in production (`DEBUG=False`).
- `memory`: In-memory outbox intended for automated tests and isolated QA.
- `sms_ir`: Built-in production integration with SMS.ir REST API v1 (`/send/verify`) for template-based OTP delivery.
- Custom provider: Any class implementing `accounts.sms.base.SMSProvider` can be referenced by its Python import path.

Related OTP settings:

```env
OTP_LENGTH=5
OTP_TTL_SECONDS=180
OTP_MAX_ATTEMPTS=5
OTP_RESEND_SECONDS=90
OTP_RESEND_LIMIT_PER_HOUR=5
```

### SMS.ir Integration (Production OTP Provider)

The project includes a built-in provider for [SMS.ir](https://sms.ir/rest-api/) using the REST v1 Fast Send / Verify endpoint (`POST https://api.sms.ir/v1/send/verify`).

#### 1. Obtain API Key from SMS.ir Panel
1. Sign in to your dashboard on [sms.ir](https://sms.ir).
2. Navigate to the developer / API Keys section.
3. Create a new API Key and copy the key value. This key is used in the `X-API-KEY` request header.

#### 2. Create and Configure an OTP Template
1. In the SMS.ir dashboard, navigate to the verification templates section (Send Verify / Templates).
2. Create a new template for OTP login.
3. Example approved template text:
   ```text
   پوشاک لوکس: کد ورود شما #Code# است.
   ```
   In SMS.ir, dynamic parameters in the template text are enclosed by `#` characters (e.g., `#Code#`).

#### 3. Obtain Numeric Template ID and Parameter Name
- **Template ID**: After the template is approved, the panel displays a numeric ID (e.g. `123456`). Copy this number to `SMS_IR_TEMPLATE_ID`.
- **Parameter Name**: The parameter name in Django must match the variable name configured in the SMS.ir panel without `#` characters. For `#Code#`, the parameter name is `Code`.

#### 4. Configure Environment Variables
Set the following variables in `.env` (or `.env.production`):

```env
SMS_PROVIDER=sms_ir
SMS_IR_API_KEY=your_actual_sms_ir_api_key
SMS_IR_TEMPLATE_ID=123456
SMS_IR_OTP_PARAMETER_NAME=Code
SMS_IR_TIMEOUT_SECONDS=10
```

#### 5. Safety and Testing
- **Local Testing & Automated Tests**: All automated tests mock external HTTP communication. Run tests safely without consuming credit:
  ```bash
  PYTHONPATH=scripts/qa uv run python manage.py test --settings=shop_test_settings accounts
  ```
- **Real-World Verification**: During development, keep `SMS_PROVIDER=console` to print codes to the terminal. In staging or production, activate `SMS_PROVIDER=sms_ir` with valid credentials. The provider automatically masks recipient phone numbers in application logs (e.g. `0912***6789`) and never logs raw OTP codes or API keys.
- **Production Validation**: In production (`DJANGO_SETTINGS_MODULE=config.settings.production`), setting `SMS_PROVIDER=sms_ir` validates that `SMS_IR_API_KEY` and a positive `SMS_IR_TEMPLATE_ID` are configured, preventing startup with missing credentials.

### Add a custom SMS provider

Create a class that implements the project's `SMSProvider` interface, then set `SMS_PROVIDER` to its Python import path. For example:

```python
# myapp/sms.py
from accounts.sms.base import SMSProvider


class CustomSMSProvider(SMSProvider):
    name = "custom"

    def send(self, phone_number: str, message: str) -> None:
        # Call custom SMS vendor API here
        ...
```

---
## 💳 Payment Gateway Configuration

Zibal remains the default payment gateway. TorobPay CPG can also be offered when server-side eligibility and checkout details are valid, using the same `PaymentGateway` architecture. Zibal follows the [official IPG documentation](https://help.zibal.ir/ipg/); the supplied TorobPay CPG PDF 1.3.31 is the credit gateway contract.

For optional multi-provider checkout set `PAYMENT_GATEWAYS=zibal,torobpay` while retaining `PAYMENT_GATEWAY=zibal`. Leaving the new list empty preserves the legacy single-provider configuration and its compatible class import path. See the [Persian TorobPay guide](docs/torobpay-payment.md) for credentials, callback registration, currency/cart assumptions, verify/settle, refund safeguards and recovery commands. No live payment, refund or production deployment is implied by local mocked tests.

```env
PAYMENT_GATEWAY=zibal
ZIBAL_MERCHANT=zibal
ZIBAL_TIMEOUT_SECONDS=10
ZIBAL_PAID_AT_TIMEZONE=
SITE_URL=http://localhost:8000
ORDER_RESERVATION_MINUTES=30
```

The literal `zibal` merchant is Zibal's public test account and is allowed only in development. For production, set `ZIBAL_MERCHANT` to the merchant identifier shown in the Zibal panel and `SITE_URL` to the registered HTTPS storefront origin. Production settings reject a missing merchant or the test account.

### Payment lifecycle

| Operation | Endpoint |
| --- | --- |
| Request payment | `POST https://gateway.zibal.ir/v1/request` |
| Open hosted payment page | `GET https://gateway.zibal.ir/start/{trackId}` |
| Verify payment | `POST https://gateway.zibal.ir/v1/verify` |
| Recover an already verified payment / inspect an unpaid transaction | `POST https://gateway.zibal.ir/v1/inquiry` |

- The database keeps amounts in Toman. Only the adapter converts `order.total` to integer Rial (`×10`).
- Zibal's `trackId` is stored in the existing unique `Payment.authority` field. `reference_id` accepts positive integers or up to 64 ASCII digits and preserves string leading zeros; 64 is the local storage limit, not a documented Zibal bound.
- The existing `/payments/callback/` URL accepts Zibal's GET parameters `trackId`, `success`, `status` and the `orderId` sent with the request. Malformed, duplicated or mismatched parameters cannot change an order.
- A verified payment requires server result `100`, status `1`, and matching amount/order identifiers with a valid reference. Verify result `201` is followed by inquiry; that code alone never marks an order paid.
- `Payment.gateway_paid_at` stores the independently interpreted gateway time. A delayed callback can accept a payment made within the deadline while the order and reservation remain active. Callback arrival does not determine lateness.
- Offset-bearing timestamps use their actual offset. Zibal's official examples omit offsets and do not document a timezone; without written confirmation and explicit configuration, those receipts require `manual_review` with no automatic fulfillment or refund decision.
- Callback and expiration lock the order before payment/variant rows. Cancelled orders and released reservations are never revived. Extra successful receipts with reliable timestamps are flagged for refund handling; duplicate callbacks repeat no fulfillment.
- Payment creation persists a unique local request before unlocked HTTP, then reloads locked Order/Payment state. Pending sessions are reused; a timeout or unknown response preserves the attempt and blocks blind gateway retries. A UUID in the callback path supports verified correlation and is not a Zibal idempotency key.
- `payments/0004` adds creation state/local UUID and allows an unknown gateway ID without changing historical amounts, references or payment times. Its reverse guard refuses to erase new request evidence. Apply reviewed migrations before web startup; `MIGRATE_ON_START=0` performs a read-only schema check.
- Verification errors remain retryable. Creation errors distinguish documented rejection from an uncertain remote outcome. Support can bind an independently obtained track ID using recovery's `--request-id` mode and a fresh checked backup, then inspect all receipts before primary selection.
- The redirect page sends only the storefront origin as `Referer`, as required by Zibal, including automatic navigation without JavaScript. Global referrer settings and frontend styling stay unchanged.
- Credentials and card data are never included in the redirect URL, frontend code, stored verification data or error logs.

Automated payment tests mock HTTP responses; browser QA intercepts gateway navigation and uses only the guarded temporary database. See the [Persian integration guide](docs/zibal-payment.md) for configuration, recovery and validation details.

---
## 🚀 Production Deployment

MiniKandoo runs Django/Gunicorn, Nginx, MariaDB, Redis and the automated `db-backup` scheduler in the existing production Compose project. Follow the [Persian deployment guide](docs/deployment-production-fa.md), [payment recovery runbook](docs/zibal-verification-recovery.md) and [database backup operations](docs/production-database-backups.md).

For a fresh installation, create a private `.env.production` from its example. Preserve the existing file and local server configuration during an update. Use `scripts/compose-production` for every production stack operation. Review and approve deployment before rebuilding or recreating running production services.

The backup container performs an initial backup, then attempts one at each UTC half-hour boundary. It uses the existing internal MariaDB service, secure client option files, shared flock, gzip validation and recorded SHA256 sidecars. Completed names are unique UTC filenames. Retention keeps the most recent 48 plus the latest backup on each of the previous seven UTC days; historical files remain untouched. Review the initial dry-run before enabling deletion and set capacity limits from real measurements.

```sh
scripts/compose-production config --quiet
scripts/compose-production ps
scripts/compose-production logs --tail=100 db-backup
scripts/compose-production run --rm --no-deps -T db-backup inspect
scripts/backup-production-db
```

Back up before migrations. Apply all reviewed migrations, including `payments/0004`, once using the reviewed web image and verify readiness before payment recovery. Keep `ZIBAL_PAID_AT_TIMEZONE` empty until Zibal confirms an IANA timezone. Inquiry is read-only by default; recovery apply requires a fresh validated backup and never issues a refund. Production restoration requires explicit human approval and a successful isolated restore test of the selected file.

Unpaid-order expiration uses the existing management command through the same wrapper:

```sh
scripts/compose-production exec -T web python manage.py expire_pending_orders
```

Preserve the existing operational schedule for this command. The new backup scheduler runs entirely in Docker and requires no host Cron or systemd timer. Same-host backups protect against database failures but not complete VPS/disk loss; off-site transfer is a separate authorized enhancement.

برای بازیابی پرداخت، timer اصلی `scripts/systemd/minikandoo-payments.timer` هر دقیقه فرمان کنترل‌شدهٔ `run_payment_maintenance --apply` را در `web` موجود اجرا می‌کند؛ انقضا در مرزهای پنج‌دقیقه‌ای انجام می‌شود. lease مشترک DB، ثبت `settle_confirmed_at` و کنترل مستقل انقضا از رقابت و اجرای تکراری جلوگیری می‌کنند. نصب، لاگ‌ها و بازیابی در [راهنمای فارسی ترب‌پی](docs/torobpay-payment.md) آمده است؛ timer فقط در مخزن آماده شده و باید مسئول سرور آن را نصب و نتیجهٔ واقعی را بررسی کند. زمان‌بندی مستقل بکاپ تغییر نکرده است.

---
## 🛠️ Troubleshooting

| Problem | Suggested solution |
| --- | --- |
| `django.db.utils.OperationalError: Can't connect` | Confirm MySQL/MariaDB is running and check `DATABASE_HOST` and `DATABASE_PORT`. |
| `Access denied` for the database user | Check the selected database instance, credentials, and grants. A local XAMPP database may be using port `3306` instead of the project's database. |
| `Access denied` while creating the test database | Grant the local testing account the required permissions on the test database, or provision the test database separately. Avoid broad global grants in production. |
| `Database returned an invalid datetime value` | MySQL time-zone tables may be missing. Run `bash scripts/mysql_dev.sh`, or load time-zone data using the database vendor's documented procedure. |
| Product pages load without CSS | Run `npm install && npm run build`. |
| OTP code is not printed | Check that `SMS_PROVIDER=console` is set and run the development server in a visible terminal. |
| `DisallowedHost: testserver` | Run tests with `uv run python manage.py test`. |
| Zibal refuses the test merchant in production | Set `ZIBAL_MERCHANT` to the real merchant identifier from the Zibal panel. |
| The Add to Cart button is unavailable | Select a color and size and confirm the selected variant is in stock. |
| `npm` dependencies appear broken | Remove `node_modules` and reinstall with `npm install`. |
| `port is already allocated` in Docker | Change the configured `WEB_PORT` or `DB_PORT` in `.env`, if supported by your Compose configuration. |
| Docker reports a missing entrypoint or incomplete mount | Check file-sharing settings and mounts, then recreate the affected service. |
| Docker cannot reach the database | In Compose, verify that `DATABASE_HOST` uses the database service name, usually `db`, rather than `localhost`. |
| Old CSS persists in Docker | Rebuild the web image, for example with `docker compose build --no-cache web`. |

---
## 📂 Project Structure

```text
├── config/                  Django settings, URLs, WSGI, and ASGI configuration
├── common/                  Validation, rate limiting, template filters, admin dashboard
├── accounts/                Users, OTP, profiles, addresses, SMS providers
├── products/                Products, categories, colors, sizes, variants, discounts, pricing
├── cart/                    Shopping cart and related services
├── orders/                  Orders, status transitions, shipping, expiration
├── payments/                Gateways, transactions, and payment callbacks
├── store/                   Storefront pages and demo data
├── templates/               HTML templates
├── assets/css/              Source CSS and design system
├── static/                  Built static assets and vendor files
├── scripts/                 Helper scripts
├── docker/                  Docker entrypoint and database configuration
├── Dockerfile               Container build configuration
└── docker-compose.yml       Production Compose configuration
```

For more details about the internal design, see [`PROJECT_ARCHITECTURE.md`](PROJECT_ARCHITECTURE.md).

---
## 📄 License

This project is described as an educational and practical project. No explicit open-source license is specified here. Unless a `LICENSE` file grants permissions, do not assume that the code is available for unrestricted reuse, modification, or redistribution.
