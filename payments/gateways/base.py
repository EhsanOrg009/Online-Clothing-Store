"""
Interface مشترک درگاه‌های پرداخت (Hosted / Redirect-based).

نکته مهم: سایت ما هرگز فرم ورود اطلاعات کارت (شماره کارت، CVV2، رمز پویا و ...)
را پیاده‌سازی نمی‌کند؛ پرداخت فقط روی صفحه رسمی درگاه انجام می‌شود.

زیبال این Interface را پیاده‌سازی می‌کند؛ هسته سفارش به جزئیات API وابسته نیست.
فیلد authority برای سازگاری با سوابق موجود حفظ شده و trackId زیبال را نگه می‌دارد.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from orders.choices import PaymentStatus


class PaymentGatewayError(Exception):
    """خطای سطح درگاه پرداخت."""


@dataclass(frozen=True)
class PaymentRequest:
    """نتیجه ایجاد تراکنش: توکن و آدرس صفحه پرداخت درگاه."""

    authority: str
    redirect_url: str


@dataclass(frozen=True)
class VerificationResult:
    """نتیجه Verify تراکنش نزد درگاه."""

    successful: bool
    status: str = PaymentStatus.FAILED
    transaction_id: str = ""
    reference_id: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(ABC):
    """پایه همه درگاه‌های پرداخت."""

    name: str = "base"

    @abstractmethod
    def create_payment(self, order, request=None) -> PaymentRequest:
        """ایجاد تراکنش نزد درگاه و دریافت شناسه + آدرس پرداخت."""

    @abstractmethod
    def verify_payment(self, payment, callback_data: dict | None = None) -> VerificationResult:
        """Verify تراکنش نزد درگاه (Callback به‌تنهایی کافی نیست)."""

    def get_payment_url(self, payment) -> str:
        """آدرس صفحه پرداخت برای Redirect کاربر."""
        return payment.metadata.get("redirect_url", "")
