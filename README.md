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
- HMAC-signed payment parameters, mandatory server-side verification, and idempotent callbacks
- A mock payment gateway for development only
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
7. Payment callbacks are processed idempotently so repeated callbacks do not repeat payment processing or deduct inventory again.
8. Unpaid orders can expire after the configured reservation period. The expiration command cancels eligible orders and releases their reserved stock.
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

Open `.env` and set the values required by your local environment. In particular, do not leave required secrets such as `SECRET_KEY`, `PAYMENT_SECRET`, or `DATABASE_PASSWORD` empty or set to example values.

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
- The mock payment gateway is for development only. It works with `DEBUG=True` and is disabled when `DEBUG=False`.
- With the default `SMS_PROVIDER=console`, OTP codes are printed to the server terminal rather than sent as real SMS messages.
- Demo account credentials and demo database passwords are for local development only. Change or remove them before exposing any deployment to a network.
- Keep real secrets in environment variables. Never commit your real `.env` file, payment credentials, or production database password.
- A real payment gateway requires a provider implementation and valid merchant credentials. Do not treat a successful browser redirect as proof of payment; always verify the transaction on the server.
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
grep -E "^(SECRET_KEY|PAYMENT_SECRET|DATABASE_PASSWORD)=" .env
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

The base `docker-compose.yml` is intended for production; the development override is omitted when you explicitly specify the base file:

```bash
docker compose -f docker-compose.yml build
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml exec web python manage.py createsuperuser
```

Important production settings include:

| Variable | Expected configuration | Purpose |
| --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | Loads production settings |
| `SECRET_KEY` | Required, strong random value | Django signing and security |
| `PAYMENT_SECRET` | Required, strong random value | Payment request/callback signing |
| `DATABASE_PASSWORD` | Required, strong password | Database authentication |
| `ALLOWED_HOSTS` | Your real domain | Allowed host names |
| `CSRF_TRUSTED_ORIGINS` | Full origins, including `https://` | CSRF protection behind the domain |
| `SECURE_SSL_REDIRECT` | `True` when HTTPS is configured | Redirects HTTP requests to HTTPS |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | `True` when HTTPS is configured | Sends cookies only over HTTPS |
| `MIGRATE_ON_START` | `1` by default | Runs migrations during startup when enabled |

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

### Moving a local database into Docker

Back up the local database first:

```bash
mysqldump -h 127.0.0.1 -u shop -p clothing_shop > backup.sql
```

Import the backup into the container, substituting your configured database credentials:

```bash
docker compose exec -T db mariadb -u root -p clothing_shop < backup.sql
```

Copy uploaded media files when applicable:

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

The project documentation reports **208 tests across 7 apps**, covering validation, rate limiting, OTP scenarios, pricing rules, overselling protection, order snapshots, cancellation and expiration, gateway signing and verification, idempotent callbacks, and end-to-end purchase flows.

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

SMS delivery is abstracted behind a provider interface. The OTP authentication flow does not need to depend on a specific SMS vendor.

Configure the provider in `.env`:

```env
SMS_PROVIDER=console
# SMS_PROVIDER=memory
# SMS_PROVIDER=myapp.sms.KavenegarProvider
```

- `console` prints OTP messages to the development server terminal.
- `memory` is intended for tests or in-memory development scenarios.
- A custom provider path can be configured for a real SMS service.

Related OTP settings:

```env
OTP_LENGTH=5
OTP_TTL_SECONDS=180
OTP_MAX_ATTEMPTS=5
OTP_RESEND_SECONDS=90
OTP_RESEND_LIMIT_PER_HOUR=5
```

### Add a real SMS provider

Create a class that implements the project's `SMSProvider` interface, then set `SMS_PROVIDER` to its Python import path. For example:

```python
# myapp/sms.py
from accounts.sms.base import SMSProvider


class KavenegarProvider(SMSProvider):
    name = "kavenegar"

    def send(self, phone_number: str, message: str) -> None:
        # Call the SMS provider's HTTP API here.
        ...
```

The example above is a template, not a complete provider integration. Add real API handling, error handling, and credential configuration before using a provider in production. An invalid import path should be handled with a clear provider-configuration error.

---
## 💳 Payment Gateway Configuration

The default development configuration uses the mock gateway:

```env
PAYMENT_GATEWAY=mock
PAYMENT_SECRET=change-me-to-a-long-random-string
ORDER_RESERVATION_MINUTES=30
```

Replace the example `PAYMENT_SECRET` with a strong random value in your local environment. The mock gateway is available only when `DEBUG=True` and is automatically disabled with `DEBUG=False`.

### Add a real payment gateway

Implement the `PaymentGateway` interface and configure `PAYMENT_GATEWAY` to point to your provider class. The following is a structural example, not a complete integration with a payment provider:

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

Example environment configuration:

```env
PAYMENT_GATEWAY=payments.gateways.zarinpal.ZarinpalGateway
PAYMENT_MERCHANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PAYMENT_SECRET=<long-random-secret>
```

Any real gateway implementation must follow these rules:

- Read the transaction amount from `order.total`; never trust an amount supplied by the client.
- Verify payment on the server with the payment provider. Query parameters alone are not proof of payment.
- Keep callback processing idempotent so repeated callbacks cannot cause repeated side effects.
- Do not store or process card details in this application.
- Store merchant credentials and signing secrets only in environment variables or a secure secrets manager.

---
## 🚀 Production Deployment

Production deployment requires a valid secret configuration, a production database, HTTPS, static-file collection, a production application server, and a scheduled task for expiring unpaid orders.

### 1. Configure environment variables

```bash
cp .env.example .env
```

Set production values in `.env` (the values below are examples and must be adapted to your host):

```env
DJANGO_SETTINGS_MODULE=config.settings.production
DEBUG=False
SECRET_KEY=<strong-random-secret-at-least-50-characters>
ALLOWED_HOSTS=shop.example.com
CSRF_TRUSTED_ORIGINS=https://shop.example.com

DATABASE_PASSWORD=<strong-database-password>

PAYMENT_GATEWAY=payments.gateways.zarinpal.ZarinpalGateway
PAYMENT_SECRET=<long-random-secret>

SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
```

The production settings are designed to refuse startup when critical values such as `SECRET_KEY`, `ALLOWED_HOSTS`, or the database password are missing or weak.

### 2. Create a restricted database user

Use a dedicated account with permissions limited to this application's database. Adapt the host, database, and password to your environment:

```sql
CREATE USER 'shop_prod'@'127.0.0.1' IDENTIFIED BY '<strong-password>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER
  ON clothing_shop.* TO 'shop_prod'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Do not grant `ALL ON *.*` to an application account in production. If your deployment runs tests, provision a separate test database and grant only the permissions needed for that database to a separate testing account.

Apply migrations and collect static files:

```bash
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
```

### 3. Schedule unpaid-order expiration

Unpaid orders should be expired periodically so their reserved stock can be released. For example, run the command every five minutes using cron, adjusting the paths for your deployment:

```cron
*/5 * * * * cd /path/to/shop && /path/to/.venv/bin/python manage.py expire_pending_orders
```

### 4. Run Gunicorn

Install the production dependencies and start the WSGI application:

```bash
uv sync --extra production
uv run gunicorn config.wsgi:application --workers 3 --bind 127.0.0.1:8000
```

For an ASGI deployment, the project documentation provides this alternative:

```bash
uv run gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000
```

Use the server type and worker configuration appropriate for the final deployment. Keep the application server bound to a private interface when it is intended to sit behind a reverse proxy.

### 5. Nginx reverse proxy example

This example redirects HTTP to HTTPS and proxies requests to Gunicorn. Update the domain, certificate paths, and filesystem paths before using it:

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

    client_max_body_size 20m;

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

Forwarding `X-Forwarded-For` is important for IP-based rate limiting. Configure Django's proxy/secure settings to match the reverse proxy and trust only headers from a proxy you control.

### 6. Use a shared cache with multiple workers

A local-memory cache is separate for each worker process. With multiple workers, rate-limit counts may therefore be inconsistent unless a shared cache is configured. The project documentation suggests Redis:

```bash
uv add django-redis
```

```env
CACHE_URL=redis://127.0.0.1:6379/1
```

Confirm that the project's settings read `CACHE_URL` and that Redis is configured and protected before relying on it in production.

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
| The mock gateway says it is only available in development | Use `DEBUG=True` in development. The mock gateway is intentionally disabled when `DEBUG=False`. |
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
