"""
لایه ارسال (Shipping).

فعلاً فقط یک روش ساده با هزینه ثابت + آستانه ارسال رایگان پیاده شده،
اما ساختار به‌گونه‌ای است که افزودن روش‌های جدید (بر اساس شهر، شرکت حمل و ...)
بدون تغییر در هسته سفارش امکان‌پذیر باشد.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings


@dataclass(frozen=True)
class ShippingOption:
    """یک روش ارسال قابل انتخاب در Checkout."""

    code: str
    label: str
    fee: Decimal
    free_over: Decimal | None = None

    def fee_for(self, subtotal: Decimal) -> Decimal:
        """هزینه ارسال برای مبلغ قابل پرداخت."""
        if self.free_over and subtotal >= self.free_over:
            return Decimal(0)
        return self.fee

    @property
    def is_free_threshold_reached(self) -> bool:
        return bool(self.free_over)


def get_shipping_options() -> list[ShippingOption]:
    """روش‌های ارسال فعال (از Settings / Environment)."""
    free_over = settings.FREE_SHIPPING_MIN_AMOUNT
    return [
        ShippingOption(
            code="standard",
            label=settings.SHIPPING_METHOD_NAME,
            fee=Decimal(settings.SHIPPING_FEE),
            free_over=Decimal(free_over) if free_over else None,
        )
    ]


def get_shipping_option(code: str) -> ShippingOption:
    """دریافت روش ارسال با کد (در صورت نامعتبر بودن، روش پیش‌فرض)."""
    options = get_shipping_options()
    for option in options:
        if option.code == code:
            return option
    return options[0]


def default_shipping_option() -> ShippingOption:
    return get_shipping_options()[0]
