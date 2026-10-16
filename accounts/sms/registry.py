"""انتخاب Provider فعال پیامک بر اساس تنظیمات."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import SMSProvider
from .console import ConsoleSMSProvider
from .memory import InMemorySMSProvider

_BUILTIN_PROVIDERS: dict[str, type[SMSProvider]] = {
    ConsoleSMSProvider.name: ConsoleSMSProvider,
    InMemorySMSProvider.name: InMemorySMSProvider,
}

_cached_provider: SMSProvider | None = None


def get_sms_provider() -> SMSProvider:
    """
    نمونه Provider فعال را برمی‌گرداند.

    برای افزودن Provider جدید:
      ۱) کلاس مورد نظر را با همان Interface بسازید.
      ۲) مسیر کلاس را در `SMS_PROVIDER` تنظیم کنید، مانند:
         SMS_PROVIDER=myapp.sms.KavenegarProvider
    """
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider

    name = settings.SMS_PROVIDER
    if name in _BUILTIN_PROVIDERS:
        provider = _BUILTIN_PROVIDERS[name]()
    else:
        # یک مسیر نادرست در `SMS_PROVIDER` نباید به خطای 500 تبدیل شود
        try:
            provider = import_string(name)()
        except (ImportError, AttributeError) as exc:
            raise SMSProviderError(
                f"ارائه‌دهنده پیامک «{name}» پیدا نشد. مقدار SMS_PROVIDER را بررسی کنید."
            ) from exc

    if not isinstance(provider, SMSProvider):
        raise TypeError("Provider پیامک باید از SMSProvider ارث‌بری کند.")
    _cached_provider = provider
    return provider


def reset_provider_cache() -> None:
    """فقط برای تست‌ها."""
    global _cached_provider
    _cached_provider = None


__all__ = [
    "SMSProvider",
    "get_sms_provider",
    "reset_provider_cache",
]
