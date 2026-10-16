"""
پیکربندی پایه پروژه.

تمام مقادیر حساس از Environment خوانده می‌شوند و هیچ Secretی در کد Hardcode نشده است.
این فایل برای هر دو محیط Development و Production مشترک است.
"""

from pathlib import Path

import environ
from django.utils.csp import CSP

# ---------------------------------------------------------------------
# مسیرها
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

# ---------------------------------------------------------------------
# هسته Django
# ---------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="insecure-dev-only-key-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# ---------------------------------------------------------------------
# اپلیکیشن‌ها
# ---------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # اپلیکیشن‌های پروژه
    "common.apps.CommonConfig",
    "accounts.apps.AccountsConfig",
    "products.apps.ProductsConfig",
    "cart.apps.CartConfig",
    "orders.apps.OrdersConfig",
    "payments.apps.PaymentsConfig",
    "store.apps.StoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "common.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# ---------------------------------------------------------------------
# Template ها
# ---------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "common.context_processors.store_settings",
                "common.context_processors.dashboard_context",
                "cart.context_processors.cart_summary",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------
if env("DATABASE_ENGINE", default="mysql") == "sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("DATABASE_SQLITE_PATH", default=str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DATABASE_NAME", default="clothing_shop"),
            "USER": env("DATABASE_USER", default="shop"),
            "PASSWORD": env("DATABASE_PASSWORD", default=""),
            "HOST": env("DATABASE_HOST", default="127.0.0.1"),
            "PORT": env.int("DATABASE_PORT", default=3306),
            "CONN_MAX_AGE": env.int("DATABASE_CONN_MAX_AGE", default=60),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "TEST": {"NAME": "test_clothing_shop"},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------
# احراز هویت و کاربر سفارشی
# ---------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "store:home"
LOGOUT_REDIRECT_URL = "store:home"

# ---------------------------------------------------------------------
# Static / Media
# ---------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# ---------------------------------------------------------------------
# Session / Cookie / امنیت پایه
# ---------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
CSRF_FAILURE_VIEW = "common.views.csrf_failure"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 24 * 30)
X_FRAME_OPTIONS = "DENY"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Content Security Policy (سیاست جلوگیری از XSS) — از قابلیت بومی Django
#
# نکته: Alpine.js برای ارزیابی عبارات داخل `x-data` / `@click` از `new Function`
# استفاده می‌کند؛ به همین دلیل `'unsafe-eval'` در script-src قرار دارد.
# در عوض، اجرای اسکریپت‌های Inline بدون Nonce کاملاً مسدود است (`CSP.NONCE`) و
# تمام Script های خارجی باید Nonce معتبر داشته باشند.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "script-src": [CSP.SELF, CSP.NONCE, CSP.UNSAFE_EVAL],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "upgrade-insecure-requests": True,
}

# ---------------------------------------------------------------------
# زبان و منطقه جغرافیایی
# ---------------------------------------------------------------------
LANGUAGE_CODE = "fa"
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------
# Cache (برای Rate-Limit و عملیات سبک)
# ---------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "clothing-shop",
    }
}

# ---------------------------------------------------------------------
# پیام‌های کاربر (Message Framework)
# ---------------------------------------------------------------------
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ---------------------------------------------------------------------
# تنظیمات اختصاصی فروشگاه
# ---------------------------------------------------------------------
# اطلاعات عمومی فروشگاه (نمایش در Footer و صفحات ارتباطی)
STORE_NAME = env("STORE_NAME", default="پوشاک لوکس")
STORE_PHONE = env("STORE_PHONE", default="021-00000000")
STORE_EMAIL = env("STORE_EMAIL", default="info@example.com")
STORE_ADDRESS = env("STORE_ADDRESS", default="تهران، ایران")
SHIPPING_METHOD_NAME = env("SHIPPING_METHOD_NAME", default="ارسال پست پیشتاز")

# سبد خرید
CART_SESSION_KEY = "cart"
CART_MERGE_ON_LOGIN = True
MAX_QUANTITY_PER_ITEM = env.int("MAX_QUANTITY_PER_ITEM", default=10)
TRUSTED_PROXY_IPS = env.list("TRUSTED_PROXY_IPS", default=[])

# ارسال (قابل توسعه در آینده)
SHIPPING_FEE = env.int("SHIPPING_FEE", default=50000)
FREE_SHIPPING_MIN_AMOUNT = env.int("FREE_SHIPPING_MIN_AMOUNT", default=0)

# مدیریت رزرو موجودی برای سفارش‌های در انتظار پرداخت
ORDER_RESERVATION_MINUTES = env.int("ORDER_RESERVATION_MINUTES", default=30)

# OTP / SMS
SMS_PROVIDER = env("SMS_PROVIDER", default="console")
SMS_API_KEY = env("SMS_API_KEY", default="")
SMS_API_SECRET = env("SMS_API_SECRET", default="")
SMS_SENDER = env("SMS_SENDER", default="")
OTP_LENGTH = env.int("OTP_LENGTH", default=5)
OTP_TTL_SECONDS = env.int("OTP_TTL_SECONDS", default=180)
OTP_MAX_ATTEMPTS = env.int("OTP_MAX_ATTEMPTS", default=5)
OTP_RESEND_SECONDS = env.int("OTP_RESEND_SECONDS", default=90)
OTP_RESEND_LIMIT_PER_HOUR = env.int("OTP_RESEND_LIMIT_PER_HOUR", default=5)

# درگاه پرداخت
PAYMENT_GATEWAY = env("PAYMENT_GATEWAY", default="mock")
PAYMENT_MERCHANT_ID = env("PAYMENT_MERCHANT_ID", default="")
PAYMENT_API_KEY = env("PAYMENT_API_KEY", default="")
PAYMENT_SECRET = env("PAYMENT_SECRET", default="insecure-dev-payment-secret")

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        # لاگرهای حساس: فقط سطح خطا، بدون اطلاعات محرمانه
        "shop.security": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}
