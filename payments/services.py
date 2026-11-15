"""
سرویس پرداخت.

قوانین حیاتی:
  • Callback به‌تنهایی ملاک پرداخت موفق نیست؛ همیشه Verify انجام می‌شود.
  • پردازش Callback ایدمپوتنت است: ارسال دوباره، سفارش/پرداخت را دوباره
    پردازش نمی‌کند و موجودی دوبار کم نمی‌شود.
  • مبلغ تراکنش فقط از سفارش (Backend) خوانده می‌شود؛ هرگز از Client.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order
from orders.services import OrderError, OrderInventoryUnavailable, OrderNotPayable, mark_order_paid

from .gateways import PaymentGatewayError, get_gateway
from .gateways.zibal import parse_callback
from .models import Payment

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """خطای قابل نمایش به کاربر."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


@dataclass(frozen=True)
class ReceiptDecision:
    action: str
    reason: str


def classify_receipt(order, gateway_paid_at, timestamp_issue="", *, primary_exists=False,
                     other_manual_receipt=False) -> ReceiptDecision:
    """Shared preview/application rules; callback arrival never determines lateness."""
    if timestamp_issue or gateway_paid_at is None or timezone.is_naive(gateway_paid_at):
        return ReceiptDecision("manual_review", timestamp_issue or "timestamp_unavailable")
    if gateway_paid_at > timezone.now():
        return ReceiptDecision("manual_review", "future_timestamp")
    if order.status == OrderStatus.CANCELLED or order.inventory_released:
        return ReceiptDecision("refund", "order_cancelled_or_inventory_released")
    if order.is_paid:
        return ReceiptDecision("refund", "additional_successful_payment")
    if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED):
        return ReceiptDecision("manual_review", "inconsistent_order_state")
    if order.expires_at and gateway_paid_at > order.expires_at:
        return ReceiptDecision("refund", "paid_after_deadline")
    if primary_exists:
        return ReceiptDecision("manual_review", "inconsistent_primary_ledger")
    if other_manual_receipt:
        return ReceiptDecision("manual_review", "another_receipt_requires_review")
    return ReceiptDecision("accept", "paid_within_deadline")


@transaction.atomic
def create_payment_for_order(order: Order, request=None) -> Payment:
    """ایجاد تراکنش پرداخت برای سفارش و دریافت آدرس درگاه."""
    order = Order.objects.select_for_update().select_related("customer").get(pk=order.pk)

    if order.is_paid:
        raise PaymentError(_("این سفارش قبلاً پرداخت شده است."))
    if order.is_expired:
        raise PaymentError(_("مهلت پرداخت این سفارش به پایان رسیده است."))
    if not order.can_pay:
        raise PaymentError(_("این سفارش در حال حاضر قابل پرداخت نیست."))
    if not order.items.exists():
        raise PaymentError(_("اقلام این سفارش یافت نشد."))
    if order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).exists():
        raise PaymentError(_("پرداخت قبلی نیازمند بررسی پشتیبانی است؛ لطفاً پرداخت تازه انجام ندهید."))

    try:
        gateway = get_gateway()
        payment_request = gateway.create_payment(order, request)
    except PaymentGatewayError as exc:
        logger.error("خطای درگاه پرداخت: %s", exc)
        raise PaymentError(_("اتصال به درگاه پرداخت ممکن نشد. لطفاً بعداً تلاش کنید.")) from exc

    try:
        payment = Payment.objects.create(
            order=order,
            gateway=gateway.name,
            amount=order.total,  # مبلغ فقط از Backend؛ تبدیل ریال فقط در adapter
            status=PaymentStatus.PENDING,
            authority=payment_request.authority,
            metadata={"redirect_url": payment_request.redirect_url},
        )
    except IntegrityError as exc:
        logger.error("شناسه تکراری از درگاه برای سفارش %s", order.order_number)
        raise PaymentError(_("شناسه پرداخت نامعتبر است. لطفاً دوباره تلاش کنید.")) from exc

    if order.status == OrderStatus.PAYMENT_FAILED:
        order.status = OrderStatus.PENDING_PAYMENT
        order.payment_status = PaymentStatus.PENDING
        order.save(update_fields=["status", "payment_status", "updated_at"])

    logger.info("تراکنش %s برای سفارش %s ایجاد شد", payment.pk, order.order_number)
    return payment


@transaction.atomic
def process_callback(payment: Payment, callback_data: dict | None = None, *,
                     gateway_timezone=None, allow_fulfillment=True, recovery_audit=None) -> Payment:
    """
    پردازش Callback درگاه به‌صورت ایدمپوتنت.

    خروجی: تراکنش با وضعیت نهایی.
    """
    # ترتیب قفل با ایجاد پرداخت یکسان است. تلاش‌های مختلف پرداخت یک سفارش
    # نیز نمی‌توانند همزمان آن سفارش را موفق پردازش کنند.
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=payment.pk)
    order = Order.objects.select_for_update().get(pk=order_id)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    payment.order = order
    if payment.gateway == "zibal":
        try:
            callback = parse_callback(callback_data or {})
            if callback["trackId"] != payment.authority or callback["orderId"] != order.order_number:
                raise PaymentGatewayError("اطلاعات بازگشت با تراکنش و سفارش مطابقت ندارد.")
        except PaymentGatewayError as exc:
            raise PaymentError(_("اطلاعات بازگشت از درگاه نامعتبر است.")) from exc
    payment.callback_count += 1
    payment.save(update_fields=["callback_count"])

    # Idempotency: پرداخت قطعی شده دوباره پردازش نمی‌شود
    if payment.status == PaymentStatus.SUCCESSFUL or (
        payment.status == PaymentStatus.CANCELLED and callback_data
        and callback_data.get("success") == "0" and recovery_audit is None
    ):
        logger.info(
            "Callback تکراری برای تراکنش %s نادیده گرفته شد (وضعیت: %s)",
            payment.authority,
            payment.status,
        )
        return payment

    try:
        gateway = get_gateway(payment.gateway)
        if gateway_timezone is not None:
            gateway.paid_at_timezone = gateway_timezone
    except PaymentGatewayError as exc:
        logger.error("درگاه پرداخت %s در دسترس نیست: %s", payment.gateway, exc)
        raise PaymentError(_("درگاه پرداخت در دسترس نیست.")) from exc

    try:
        result = gateway.verify_payment(payment, callback_data)
    except PaymentGatewayError as exc:
        logger.error("خطا در Verify تراکنش %s: %s", payment.authority, exc)
        raise PaymentError(_("تأیید پرداخت ممکن نشد. لطفاً با پشتیبانی تماس بگیرید.")) from exc

    observed_at = timezone.now()
    history = list(payment.metadata.get("verification_history", []))
    if result.raw != payment.metadata.get("verification"):
        history.append({"observed_at": observed_at.isoformat(), "data": result.raw})
    payment.metadata = {
        **payment.metadata, "verification": result.raw, "verification_history": history,
    }
    if recovery_audit:
        payment.metadata["recovery_history"] = [
            *payment.metadata.get("recovery_history", []),
            {**recovery_audit, "observed_at": observed_at.isoformat()},
        ]
    if result.status != PaymentStatus.PENDING:
        payment.verified_at = observed_at

    if result.successful:
        decision = classify_receipt(
            order, result.gateway_paid_at, result.timestamp_issue,
            primary_exists=order.payments.exclude(pk=payment.pk).filter(
                status=PaymentStatus.SUCCESSFUL, needs_refund=False,
            ).exists(),
        )
        if payment.reference_id and (
            payment.reference_id != result.reference_id
            or payment.transaction_id != result.transaction_id
        ):
            decision = ReceiptDecision("manual_review", "receipt_identifiers_changed")
        else:
            payment.transaction_id = result.transaction_id
            payment.reference_id = result.reference_id
        # A previously impossible timestamp needs a human audit even if time has
        # since caught up. A confirmed timezone can resolve timezone_unconfirmed.
        if payment.metadata.get("decision", {}).get("reason") in (
            "future_timestamp", "previous_future_timestamp",
        ):
            decision = ReceiptDecision("manual_review", "previous_future_timestamp")
        if decision.action == "accept" and not allow_fulfillment:
            decision = ReceiptDecision("manual_review", "recovery_primary_not_selected")
        payment.gateway_paid_at = result.gateway_paid_at
        payment.paid_at = payment.paid_at or observed_at
        deadline = order.expires_at
        if decision.action == "accept":
            try:
                mark_order_paid(order, paid_at=observed_at, gateway_paid_at=result.gateway_paid_at)
            except OrderNotPayable:
                decision = ReceiptDecision("refund", "order_no_longer_payable")
            except OrderInventoryUnavailable:
                decision = ReceiptDecision("manual_review", "inventory_reservation_unavailable")
            except OrderError as exc:  # pragma: no cover - unexpected service failure
                raise PaymentError(str(exc)) from exc
        payment.needs_refund = decision.action == "refund"
        payment.status = (
            PaymentStatus.MANUAL_REVIEW if decision.action == "manual_review" else PaymentStatus.SUCCESSFUL
        )
        payment.error_message = {
            "accept": "",
            "refund": "وجه دریافت شده اما سفارش قابل پرداخت نیست یا قبلاً پرداخت شده؛ نیازمند بازگشت وجه دستی.",
            "manual_review": "وجه در زیبال تأیید شده؛ زمان پرداخت یا وضعیت سفارش نیازمند بررسی پشتیبانی است.",
        }[decision.action]
        payment.metadata["decision"] = {
            "action": decision.action, "reason": decision.reason,
            "order_expires_at": deadline.isoformat() if deadline else None,
            "inventory_released": order.inventory_released,
            "observed_at": observed_at.isoformat(),
        }
        payment.save()
        logger.log(
            logging.INFO if decision.action == "accept" else logging.WARNING,
            "Verified receipt %s: %s (%s)", payment.authority, decision.action, decision.reason,
        )
        return payment

    if payment.paid_at is not None:
        # A later conflicting gateway response cannot erase a previously verified
        # receipt awaiting review. Both sanitized responses remain in the history.
        payment.status = PaymentStatus.MANUAL_REVIEW
        payment.needs_refund = False
        payment.error_message = "گزارش تازهٔ درگاه با دریافت وجه قبلی مغایر است؛ نیازمند بررسی دستی."
        payment.metadata["decision"] = {"action": "manual_review", "reason": "gateway_receipt_conflict"}
        payment.save()
        return payment

    payment.status = result.status or PaymentStatus.FAILED
    payment.error_message = (result.message or _("پرداخت ناموفق بود."))[:255]
    payment.save()

    if payment.status == PaymentStatus.PENDING:
        logger.info("تراکنش %s همچنان در انتظار پرداخت است", payment.authority)
        return payment

    order = payment.order
    if order.status == OrderStatus.PENDING_PAYMENT:
        if payment.status == PaymentStatus.FAILED:
            order.status = OrderStatus.PAYMENT_FAILED
            order.payment_status = PaymentStatus.FAILED
            order.save(update_fields=["status", "payment_status", "updated_at"])
        elif payment.status == PaymentStatus.CANCELLED:
            order.payment_status = PaymentStatus.CANCELLED
            order.save(update_fields=["payment_status", "updated_at"])

    logger.info(
        "پرداخت سفارش %s ناموفق/لغو شد: %s",
        order.order_number,
        payment.error_message,
    )
    return payment


def expire_stale_payments(older_than_minutes: int = 60) -> int:
    """تبدیل تراکنش‌های معلق قدیمی به «منقضی» (برای گزارش‌گیری)."""
    threshold = timezone.now() - timedelta(minutes=older_than_minutes)
    return Payment.objects.filter(
        status=PaymentStatus.PENDING, created_at__lt=threshold
    ).update(status=PaymentStatus.EXPIRED, error_message="منقضی شده")
