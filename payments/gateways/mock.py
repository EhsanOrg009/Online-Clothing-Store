"""
درگاه پرداخت Mock (فقط برای Development و تست).

این درگاه رفتار یک درگاه میزبانی‌شده واقعی را شبیه‌سازی می‌کند:
  ۱) یک Authority صادر می‌کند و کاربر را به «صفحه درگاه» می‌فرستد.
  ۲) در پایان، داده نتیجه را با امضای HMAC به Callback برمی‌گرداند.
  ۳) سرور فقط با Verify (بررسی امضا و وضعیت تراکنش) نتیجه را می‌پذیرد.

برای امنیت:
  • در Production غیرفعال است.
  • نتیجه Callback بدون امضای معتبر پذیرفته نمی‌شود.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.urls import reverse

from orders.choices import PaymentStatus

from .base import PaymentGateway, PaymentGatewayError, PaymentRequest, VerificationResult


class MockGateway(PaymentGateway):
    name = "mock"

    def __init__(self):
        if not settings.DEBUG:
            raise PaymentGatewayError(
                "درگاه Mock فقط در محیط Development فعال است. "
                "در Production یک درگاه واقعی تنظیم کنید."
            )
        if not settings.PAYMENT_SECRET:
            raise PaymentGatewayError("PAYMENT_SECRET تنظیم نشده است.")

    # ------------------------------------------------------------------
    # امضا (شبیه‌سازی امضای درگاه واقعی)
    # ------------------------------------------------------------------
    def sign(self, payload: str) -> str:
        secret = settings.PAYMENT_SECRET.encode()
        return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

    def _result_payload(self, authority: str, status: str) -> str:
        return f"result|{authority}|{status}"

    # ------------------------------------------------------------------
    # PaymentGateway
    # ------------------------------------------------------------------
    def create_payment(self, order, request=None) -> PaymentRequest:
        authority = f"MOCK-{secrets.token_urlsafe(18)}"
        query_amount = order.total
        start_signature = self.sign(f"start|{authority}|{query_amount}")
        url = (
            reverse("payments:mock_gateway", args=[authority])
            + f"?amount={query_amount}&sig={start_signature}"
        )
        return PaymentRequest(authority=authority, redirect_url=url)

    def verify_payment(self, payment, callback_data: dict | None = None) -> VerificationResult:
        callback_data = callback_data or {}
        status = callback_data.get("status", "")
        signature = callback_data.get("sig", "")

        expected = self.sign(self._result_payload(payment.authority, status))
        if not hmac.compare_digest(str(signature), expected):
            return VerificationResult(
                successful=False,
                status=PaymentStatus.FAILED,
                message="امضای Callback نامعتبر است.",
            )

        # جلوگیری از دستکاری مبلغ یا سفارش
        if payment.amount != payment.order.total:
            return VerificationResult(
                successful=False,
                status=PaymentStatus.FAILED,
                message="مبلغ تراکنش با مبلغ سفارش یکسان نیست.",
            )

        if status == "successful":
            return VerificationResult(
                successful=True,
                status=PaymentStatus.SUCCESSFUL,
                transaction_id=str(secrets.randbelow(9_000_000) + 1_000_000),
                reference_id=str(secrets.randbelow(10**13)),
                message="پرداخت با موفقیت انجام شد.",
            )

        if status == "cancelled":
            return VerificationResult(
                successful=False,
                status=PaymentStatus.CANCELLED,
                message="پرداخت توسط کاربر لغو شد.",
            )

        return VerificationResult(
            successful=False,
            status=PaymentStatus.FAILED,
            message="پرداخت ناموفق بود.",
        )

    # ------------------------------------------------------------------
    # کمکی برای صفحه Mock درگاه
    # ------------------------------------------------------------------
    def build_callback_query(self, payment, status: str) -> str:
        """پارامترهای امضاشده Callback که صفحه Mock درگاه باید بفرستد."""
        signature = self.sign(self._result_payload(payment.authority, status))
        return f"authority={payment.authority}&status={status}&sig={signature}"
