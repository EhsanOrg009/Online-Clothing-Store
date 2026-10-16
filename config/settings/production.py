"""
پیکربندی محیط Production.

فایل `production.py` را با متغیرهای محیطی واقعی فعال کنید:

    export DJANGO_SETTINGS_MODULE=config.settings.production
"""

from .base import *  # noqa: F403

DEBUG = False

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

# ---------------------------------------------------------------------
# HTTPS و Cookies امن
# ---------------------------------------------------------------------
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

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
# لاگ در فایل
# ---------------------------------------------------------------------
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
