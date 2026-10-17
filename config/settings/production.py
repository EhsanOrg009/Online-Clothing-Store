"""
پیکربندی محیط Production.

فایل `production.py` را با متغیرهای محیطی واقعی فعال کنید:

    export DJANGO_SETTINGS_MODULE=config.settings.production
"""

from .base import *  # noqa: F403
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured

DEBUG = False

# Public origins are explicit; cookies remain host-only (no .minikandoo.ir scope).
SITE_URL = env("SITE_URL", default="https://minikandoo.ir").rstrip("/")
API_URL = env("API_URL", default="https://api.minikandoo.ir").rstrip("/")
for _name, _origin in (("SITE_URL", SITE_URL), ("API_URL", API_URL)):
    _parsed = urlsplit(_origin)
    if (_parsed.scheme != "https" or not _parsed.hostname or _parsed.username
            or _parsed.password or _parsed.path or _parsed.query or _parsed.fragment):
        raise ImproperlyConfigured(f"{_name} must be an HTTPS origin without a path.")
if urlsplit(SITE_URL).netloc == urlsplit(API_URL).netloc:
    raise ImproperlyConfigured("SITE_URL and API_URL must use different hosts.")
SECURE_CSP = {**SECURE_CSP, "connect-src": [CSP.SELF, API_URL]}

# Shared counters across Gunicorn workers; only reachable on the backend network.
REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
CACHES = {"default": {
    "BACKEND": "django.core.cache.backends.redis.RedisCache",
    "LOCATION": REDIS_URL,
    "OPTIONS": {"socket_connect_timeout": 3, "socket_timeout": 3},
}}

# ---------------------------------------------------------------------
# الزامی: کلیدها و میزبان‌ها باید از Environment بیایند
# ---------------------------------------------------------------------
if SECRET_KEY.startswith("insecure") or len(SECRET_KEY) < 50:  # noqa: F405
    raise RuntimeError(
        "SECRET_KEY ضعیف یا خالی است. یک کلید تصادفی بلند در Environment تنظیم کنید."
    )

if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError("ALLOWED_HOSTS در Production نمی‌تواند خالی باشد.")

if env("DATABASE_PASSWORD", default="") in ("", "change-me"):  # noqa: F405
    raise RuntimeError("رمز عبور دیتابیس در Production تنظیم نشده است.")
if PAYMENT_SECRET.startswith(("insecure", "change-me")) or len(PAYMENT_SECRET) < 50:
    raise ImproperlyConfigured("PAYMENT_SECRET must contain at least 50 random characters.")
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("Wildcard ALLOWED_HOSTS is forbidden in production.")

# ---------------------------------------------------------------------
# HTTPS و Cookies امن
# ---------------------------------------------------------------------
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

# Healthcheck داخلی کانتینر (Docker HEALTHCHECK) روی HTTP انجام می‌شود و
# نباید به HTTPS ریدایرکت شود؛ این مسیر حساس نیست و هیچ داده‌ای برنمی‌گرداند.
# nginx هم این مسیر را فقط برای شبکه‌های داخلی Docker باز می‌کند.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^readyz/$"]

# ---------------------------------------------------------------------
# Static Files: باید ابتدا `uv run python manage.py collectstatic` اجرا شود
# ---------------------------------------------------------------------
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ---------------------------------------------------------------------
# لاگ
# ---------------------------------------------------------------------
# پیش‌فرض: فقط stdout/stderr (مناسب Docker و جمع‌آوری لاگ در VPS).
# اگر می‌خواهید نسخه‌ای در فایل هم نگه دارید: LOG_TO_FILE=1
if env.bool("LOG_TO_FILE", default=False):  # noqa: F405
    # نکته: دایرکتوری `logs/` در `.gitignore` است و در هر استقرار تازه وجود
    # ندارد. بدون ساخت خودکار آن، Django هنگام راه‌اندازی با
    # `ValueError: Unable to configure handler 'file'` بالا نمی‌آید.
    _LOG_DIR = BASE_DIR / "logs"  # noqa: F405
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    LOGGING["handlers"]["file"] = {  # noqa: F405
        "class": "logging.FileHandler",
        "filename": _LOG_DIR / "django.log",
        "formatter": "verbose",
        "encoding": "utf-8",
    }
    LOGGING["root"]["handlers"] = ["file", "console"]  # noqa: F405
    LOGGING["loggers"]["django"]["handlers"] = ["file", "console"]  # noqa: F405
