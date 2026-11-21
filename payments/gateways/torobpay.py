"""TorobPay adapter using the existing three-method PaymentGateway contract.

Optional checkout/callback/cancellation capabilities belong only to this
adapter. Zibal does not implement irrelevant credit-payment operations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from threading import Lock

from django.utils import timezone

from orders.choices import PaymentStatus

from .base import PaymentGateway, PaymentGatewayError, PaymentRequest, PaymentRequestRejected, VerificationResult
from .torobpay_client import TorobPayClient, TorobPayError
from .torobpay_data import (
    Eligibility, create_payload, customer_fingerprint, eligible_amount, order_items,
    toman_to_rial, token_fingerprint, validate_payment_url,
)


@dataclass
class CheckoutAuthorization:
    customer_id: int
    amount_toman: Decimal
    customer_digest: str
    checked_at: datetime
    eligibility: Eligibility
    bound_order_id: int | None = None
    _binding_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def matches(self, customer, amount, address):
        return (
            self.eligibility.eligible is True and self.customer_id == customer.pk
            and (not hasattr(address, "items") or self.bound_order_id in (None, address.pk))
            and self.amount_toman == amount
            and timedelta(0) <= timezone.now() - self.checked_at <= timedelta(seconds=60)
            and self.customer_digest == customer_fingerprint(customer, address)
        )

    def bind_order(self, order):
        with self._binding_lock:
            if not self.matches(order.customer, order.total, order):
                return False
            self.bound_order_id = order.pk
            return True


class TorobPayGateway(PaymentGateway):
    name = "torobpay"
    checkout_title = "خرید اعتباری با ترب‌پی"
    redirect_referrer_policy = "no-referrer"
    requires_reconciliation_before_release = True
    requires_remote_cancellation = True

    def __init__(self):
        self.client = TorobPayClient()

    def checkout_offer(self, *, customer, amount, address):
        if hasattr(address, "items"):
            from .torobpay_data import cart_payload
            cart_payload(address)
        digest = customer_fingerprint(customer, address)
        result = self.client.eligible(eligible_amount(amount))
        return CheckoutAuthorization(customer.pk, amount, digest, timezone.now(), result,
                                     bound_order_id=address.pk if hasattr(address, "items") else None)

    def authorize_order(self, order, authorization=None):
        if isinstance(authorization, CheckoutAuthorization) and authorization.bind_order(order):
            return authorization
        authorization = self.checkout_offer(customer=order.customer, amount=order.total, address=order)
        if not authorization.eligibility.eligible:
            raise PaymentGatewayError("پرداخت اعتباری ترب‌پی برای این سفارش در دسترس نیست؛ روش دیگری انتخاب کنید.")
        return authorization

    def request_snapshot_digest(self, order, *, request_id):
        return hashlib.sha256(json.dumps(self.payment_request_payload(order, request_id=request_id),
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def snapshot_for_request(self, order):
        order._torobpay_items = tuple(order_items(order))
        return order

    def payment_request_payload(self, order, *, request_id=None):
        return create_payload(order, request_id=request_id)

    def create_payment(self, order, request=None, *, request_id=None):
        payload = self.payment_request_payload(order, request_id=request_id)
        client = TorobPayClient(order_id=order.pk, payment_id=getattr(order, "_payment_attempt_id", None), correlation_id=request_id)
        try:
            result = client.create_token(payload)
        except TorobPayError as error:
            if error.operation == "oauth":
                # The payment-token request was never sent. Authentication/cache
                # failure before that request is a definitive local no-create.
                raise PaymentRequestRejected("احراز هویت ترب‌پی در دسترس نیست؛ روش دیگری انتخاب کنید.", code=error.code) from None
            if error.outcome == "rejected" and error.code in (1000, 1003, 1011, 1099):
                raise PaymentRequestRejected("درخواست پرداخت ترب‌پی پذیرفته نشد؛ روش دیگری انتخاب کنید.", code=error.code) from None
            raise
        return PaymentRequest(
            authority=token_fingerprint(result.token), redirect_url=result.payment_url,
            provider_token=result.token,
            audit={"torobpay": {
                "amount_rial": payload["amount"], "amount_toman": str(order.total),
                "transaction_id": str(request_id), "last_known_order_state": "NEW",
                "api_status": "PENDING", "receipt_seen": False,
            }},
            review_reason="نشانی درگاه تأیید نشد؛ شناسهٔ پرداخت برای پیگیری پشتیبانی حفظ شده است." if not result.redirect_valid else "",
        )

    def get_payment_url(self, payment):
        if payment.metadata.get("torobpay", {}).get("active_operation", {}).get("status") in ("requesting", "uncertain", "review"):
            raise PaymentGatewayError("نتیجهٔ عملیات پرداخت ترب‌پی هنوز روشن نیست؛ با پشتیبانی پیگیری کنید.")
        if token_fingerprint(payment.provider_token) != payment.authority:
            raise PaymentGatewayError("شناسهٔ پرداخت ترب‌پی نیازمند بررسی است.")
        return validate_payment_url(payment.metadata.get("redirect_url"))

    def verify_payment(self, payment, callback_data=None):
        # Verify is an intermediate receipt, never sufficient for fulfillment.
        client = self.client_for(payment)
        transaction_id = client.operation("verify", payment.provider_token)
        return VerificationResult(successful=False, status=PaymentStatus.PENDING,
            transaction_id=transaction_id, message="پرداخت در انتظار تأیید نهایی است.",
            raw={"api_status": "VERIFY", "receipt_seen": True})

    @staticmethod
    def client_for(payment):
        if token_fingerprint(payment.provider_token) != payment.authority:
            raise PaymentGatewayError("شناسهٔ پرداخت ترب‌پی نیازمند بررسی است.")
        return TorobPayClient(order_id=payment.order_id, payment_id=payment.pk, correlation_id=payment.request_id)

    def process_callback(self, payment, callback_data):
        from payments.torobpay_services import process_torobpay_callback
        return process_torobpay_callback(payment, callback_data)

    def reconcile_before_release(self, payment, *, heartbeat=None):
        from payments.torobpay_services import reconcile_payment
        return reconcile_payment(payment, heartbeat=heartbeat)

    def cancel_order_payment(self, payment, *, actor, evidence=None):
        from payments.torobpay_services import cancel_payment
        return cancel_payment(payment, actor=actor, evidence=evidence)

    def settle(self, payment):
        return self.client_for(payment).operation("settle", payment.provider_token)

    def status(self, payment):
        return self.client_for(payment).status(payment.provider_token)

    def revert(self, payment, *, remote_state, paid_at, now=None):
        now = now or timezone.now()
        if (remote_state != "W_FOR_VERIFY" or not isinstance(paid_at, datetime) or timezone.is_naive(paid_at)
                or not timedelta(0) <= now - paid_at < timedelta(minutes=30)):
            raise PaymentGatewayError("بازگشت فوری فقط با وضعیت و زمان قطعی مجاز ترب‌پی انجام می‌شود.")
        return self.client_for(payment).operation("revert", payment.provider_token)

    def cancel(self, payment, *, remote_state):
        if remote_state not in ("W_FOR_VERIFY", "W_FOR_SETTLE", "ONGOING"):
            raise PaymentGatewayError("وضعیت ترب‌پی برای لغو تأیید نشده است.")
        return self.client_for(payment).operation("cancel", payment.provider_token)

    def update(self, payment, revision, *, remote_state):
        from .torobpay_data import cart_payload
        current = Decimal(payment.metadata.get("torobpay", {}).get("amount_toman", str(payment.amount)))
        if remote_state != "ONGOING":
            raise PaymentGatewayError("فقط سفارش فعال ترب‌پی قابل ویرایش است.")
        if revision.discount_total or payment.order.discount_total:
            raise PaymentGatewayError("سفارش تخفیف‌دار در ترب‌پی قابل ویرایش نیست.")
        if revision.total <= 0 or revision.total >= current:
            raise PaymentGatewayError("مبلغ جدید باید کمتر باشد؛ برای افزایش مبلغ، لغو تأییدشده و سفارش تازه لازم است.")
        return self.client_for(payment).operation("update", payment.provider_token, payload={
            "amount": toman_to_rial(revision.total), "paymentMethodTypeDto": "ONLINE_CREDIT",
            "cartList": [cart_payload(revision)],
        })
