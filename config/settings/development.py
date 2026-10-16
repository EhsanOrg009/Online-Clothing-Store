"""پیکربندی محیط Development."""

from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ALLOWED_HOSTS + ["localhost", "127.0.0.1", "[::1]"]  # noqa: F405

if not SECRET_KEY or SECRET_KEY.startswith("insecure"):  # noqa: F405
    SECRET_KEY = "dev-only-not-for-production-9f2b7c1d4e5a6f708192a3b4c5d6e7f8"

# سرو فایل‌های Static بدون نیاز به collectstatic در Development
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

# Static/Media در Development مستقیم سرو می‌شوند
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
