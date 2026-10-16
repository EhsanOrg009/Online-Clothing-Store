"""لایه انتزاعی ارسال پیامک (SMS Provider).

برای اتصال سرویس واقعی در آینده کافی است یک کلاس جدید با همین Interface
بسازید و نام آن را در `SMS_PROVIDER` تنظیم کنید؛ نیازی به تغییر هسته احراز هویت نیست.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SMSProviderError(Exception):
    """خطا در ارسال پیامک توسط Provider."""


class SMSProvider(ABC):
    """Interface مشترک همه Provider های پیامک."""

    name: str = "base"

    @abstractmethod
    def send(self, phone_number: str, message: str) -> None:
        """ارسال یک پیامک متنی به شماره داده‌شده."""

    @classmethod
    def describe(cls) -> str:
        return cls.name
