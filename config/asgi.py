"""Entry Point ASGI (سازگار با WebSockets و سرویس‌دهنده‌های ناهمگام)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "config.settings.production"
)

application = get_asgi_application()
