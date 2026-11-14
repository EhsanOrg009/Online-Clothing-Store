"""انتخاب درگاه پرداخت فعال بر اساس تنظیمات."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import (
    PaymentGateway,
    PaymentGatewayError,
    PaymentRequest,
    VerificationResult,
)
from .zibal import ZibalGateway

_BUILTIN_GATEWAYS: dict[str, type[PaymentGateway]] = {
    ZibalGateway.name: ZibalGateway,
}


def get_gateway(name: str | None = None) -> PaymentGateway:
    """
    درگاه زیبال را با نام کوتاه یا مسیر کلاس موجود برمی‌گرداند.
    سوابق سایر درگاه‌ها قابل نمایش‌اند اما عملیات جدید برای آن‌ها انجام نمی‌شود.
    """
    key = name or settings.PAYMENT_GATEWAY
    if key in _BUILTIN_GATEWAYS:
        gateway = _BUILTIN_GATEWAYS[key]()
    else:
        # یک مسیر نادرست در `PAYMENT_GATEWAY` نباید به خطای 500 تبدیل شود
        try:
            gateway = import_string(key)()
        except (ImportError, AttributeError) as exc:
            raise PaymentGatewayError(
                f"درگاه پرداخت «{key}» پیدا نشد. مقدار PAYMENT_GATEWAY را بررسی کنید."
            ) from exc

    if not isinstance(gateway, ZibalGateway) or gateway.name != ZibalGateway.name:
        raise PaymentGatewayError("تنها درگاه پرداخت زیبال پشتیبانی می‌شود.")
    return gateway


__all__ = [
    "PaymentGateway",
    "PaymentGatewayError",
    "PaymentRequest",
    "VerificationResult",
    "get_gateway",
]
