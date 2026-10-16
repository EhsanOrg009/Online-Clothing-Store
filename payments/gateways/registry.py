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
from .mock import MockGateway

_BUILTIN_GATEWAYS: dict[str, type[PaymentGateway]] = {
    MockGateway.name: MockGateway,
}


def get_gateway(name: str | None = None) -> PaymentGateway:
    """
    نمونه درگاه فعال را برمی‌گرداند.

    برای افزودن درگاه واقعی:
        کلاس جدید را با همین Interface بسازید و در `.env` قرار دهید:
            PAYMENT_GATEWAY=payments.gateways.ZarinpalGateway
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

    if not isinstance(gateway, PaymentGateway):
        raise PaymentGatewayError("درگاه پرداخت باید از PaymentGateway ارث‌بری کند.")
    return gateway


__all__ = [
    "PaymentGateway",
    "PaymentGatewayError",
    "PaymentRequest",
    "VerificationResult",
    "get_gateway",
]
