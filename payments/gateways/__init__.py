"""
لایه انتزاعی درگاه‌های پرداخت.

نحوه افزودن درگاه واقعی در آینده:
    ۱) کلاسی مانند `payments/gateways/zarinpal.py` با Interface پایه بسازید.
    ۲) در فایل `.env` مقدار `PAYMENT_GATEWAY` را به مسیر کلاس تنظیم کنید:
           PAYMENT_GATEWAY=payments.gateways.zarinpal.ZarinpalGateway
"""

from .base import (
    PaymentGateway,
    PaymentGatewayError,
    PaymentRequest,
    VerificationResult,
)
from .mock import MockGateway
from .registry import get_gateway

__all__ = [
    "PaymentGateway",
    "PaymentGatewayError",
    "PaymentRequest",
    "VerificationResult",
    "MockGateway",
    "get_gateway",
]
