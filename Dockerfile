# syntax=docker/dockerfile:1.7
# =====================================================================
#  فروشگاه اینترنتی پوشاک — Dockerfile (چندمرحله‌ای)
#
#  سه مرحله:
#    ۱) assets   : ساخت CSS با Tailwind و کپی کتابخانه‌ها (Node)
#    ۲) builder  : نصب وابستگی‌های Python با uv
#    ۳) runtime  : فقط فایل‌های لازم برای اجرا (حجم نهایی کوچک)
#
#  نکته: مرحله assets فقط برای ساخت CSS با Tailwind لازم است و به‌خاطر
#  لایه‌بندی کش، تغییر کد Python باعث اجرای دوباره‌ی آن نمی‌شود.
# =====================================================================


# ---------------------------------------------------------------------
# مرحله ۱ — ساخت دارایی‌های Frontend
# ---------------------------------------------------------------------
FROM node:22-bookworm-slim AS assets

WORKDIR /build

# ابتدا فقط وابستگی‌ها تا لایه کش بشکند
COPY package.json package-lock.json ./
RUN npm ci

# حالا منابع و اسکریپت ساخت
COPY assets ./assets
COPY scripts/copy_vendor.js ./scripts/copy_vendor.js
COPY templates ./templates
COPY static/js ./static/js
COPY static/favicon.svg ./static/favicon.svg
RUN npm run build


# ---------------------------------------------------------------------
# مرحله ۲ — نصب وابستگی‌های Python با uv
# ---------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# ایندکس بسته‌ها باید با `uv.lock` یکی باشد؛ قفل این پروژه روی آینه‌ی
# Tsinghua resolve شده است. اگر جای دیگری resolve کردید، هنگام build عوض کنید:
#     docker compose build --build-arg UV_DEFAULT_INDEX=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    # خواندن بسته‌ها توسط uv
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    # نصب خودِ uv توسط pip از همان ایندکس
    PIP_INDEX_URL=${UV_DEFAULT_INDEX} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# uv از PyPI نصب می‌شود، نه ghcr.io.
# دلیل: `COPY --from=ghcr.io/astral-sh/uv` در برخی شبکه‌ها با
# «TLS handshake timeout» شکست می‌خورد، حال آنکه دسترسی به PyPI سالم است.
# نسخه عمداً با uv محلی یکی نگه داشته می‌شود تا رفتار خواندن uv.lock
# دقیقاً همانند محیط توسعه باشد. (با `uv --version` به‌روزرسانی کنید)
RUN pip install --no-cache-dir "uv==0.11.23"

# وابستگی‌های سیستمی مورنیاز `mysqlclient` و `Pillow` هنگام ساخت
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       pkg-config \
       default-libmysqlclient-dev \
       libjpeg62-turbo-dev \
       zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ابتدا فقط فایل‌های پیکربندی تا لایه کش بشکند
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra production

# حالا کد برنامه و نصب نهایی
COPY . .
RUN uv sync --frozen --no-dev --extra production


# ---------------------------------------------------------------------
# مرحله ۳ — ایمیج نهایی اجرا
# ---------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # مسیر محیط مجازی ساخته‌شده توسط uv
    PATH="/app/.venv/bin:$PATH"

# فقط کتابخانه‌های اجرایی (بدون ابزارهای ساخت)
#
# نکته: در Debian Bookworm پکیجی به نام `default-libmysqlclient-runtime`
# وجود ندارد. کتابخانان `mysqlclient` روی `libmariadb-dev-compat` (← `libmariadb-dev`)
# ساخته می‌شود، پس کتابخانه‌ی اجرایی موردنیاز همان `libmariadb3` است.
# (بررسی شد: `default-libmysqlclient-dev` ← `libmariadb-dev-compat` ← `libmariadb3`)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libmariadb3 \
       libjpeg62-turbo \
       zlib1g \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# محیط مجازی
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# کد برنامه
COPY --chown=app:app . .

# static ساخته‌شده در مرحله assets
COPY --from=assets --chown=app:app /build/static /app/static

# دایرکتوری‌هایی که در زمان اجرا لازم‌اند
RUN mkdir -p /app/media /app/logs /app/staticfiles \
    && rm -rf /app/node_modules /app/assets \
    && chown -R app:app /app/media /app/logs /app/staticfiles \
    && chmod +x /app/docker/entrypoint.sh

# اجرا با کاربر غیر root
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
    CMD curl -fsS -H 'Host: localhost' http://127.0.0.1:8000/healthz/ >/dev/null || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]

# به‌طور پیش‌فرض با Gunicorn اجرا می‌شود؛ در توسعه آن را با runserver عوض کنید
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--threads", "2", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
