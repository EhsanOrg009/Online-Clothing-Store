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

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order
from orders.services import OrderError, OrderNotPayable, mark_order_paid

from .gateways import PaymentGatewayError, get_gateway
from .models import Payment

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """خطای قابل نمایش به کاربر."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


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
def process_callback(payment: Payment, callback_data: dict | None = None) -> Payment:
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
    payment.callback_count += 1
    payment.save(update_fields=["callback_count"])

    # Idempotency: پرداخت قطعی شده دوباره پردازش نمی‌شود
    if payment.status in (
        PaymentStatus.SUCCESSFUL,
        PaymentStatus.CANCELLED,
    ):
        logger.info(
            "Callback تکراری برای تراکنش %s نادیده گرفته شد (وضعیت: %s)",
            payment.authority,
            payment.status,
        )
        return payment

    try:
        gateway = get_gateway(payment.gateway)
    except PaymentGatewayError as exc:
        logger.error("درگاه پرداخت %s در دسترس نیست: %s", payment.gateway, exc)
        raise PaymentError(_("درگاه پرداخت در دسترس نیست.")) from exc

    try:
        result = gateway.verify_payment(payment, callback_data)
    except PaymentGatewayError as exc:
        logger.error("خطا در Verify تراکنش %s: %s", payment.authority, exc)
        raise PaymentError(_("تأیید پرداخت ممکن نشد. لطفاً با پشتیبانی تماس بگیرید.")) from exc

    payment.metadata = {**payment.metadata, "verification": result.raw}
    if result.status != PaymentStatus.PENDING:
        payment.verified_at = timezone.now()

    if result.successful:
        payment.status = PaymentStatus.SUCCESSFUL
        payment.transaction_id = result.transaction_id
        payment.reference_id = result.reference_id
        payment.paid_at = timezone.now()
        payment.error_message = ""
        payment.save()
        if order.is_paid:
            # دریافت واقعی وجه برای تلاش دوم ثبت می‌شود، ولی سفارش دوباره پرداخت نمی‌شود.
            payment.needs_refund = True
            payment.error_message = _("سفارش قبلاً با تراکنش دیگری پرداخت شده؛ نیازمند بازگشت وجه.")
            payment.save(update_fields=["needs_refund", "error_message"])
            logger.critical("پرداخت اضافه برای سفارش %s؛ نیازمند بازگشت وجه", order.order_number)
            return payment
        try:
            mark_order_paid(payment.order, paid_at=payment.paid_at)
        except OrderNotPayable:
            # پول در درگاه دریافت شده اما سفارش دیگر معتبر نیست (لغو/منقضی‌شده).
            # سفارش احیا نمی‌شود؛ تراکنش برای بازگشت وجه دستی علامت می‌خورد تا
            # هیچ‌گاه حالتِ «سفارش لغو + پرداخت موفق + موجودی آزادشده» رخ ندهد.
            payment.needs_refund = True
            payment.error_message = _(
                "پرداخت پس از لغو/انقضای سفارش انجام شد؛ نیازمند بازگشت وجه."
            )[:255]
            payment.save(update_fields=["needs_refund", "error_message"])
            logger.critical(
                "پرداخت موفق برای سفارش لغو/منقضی‌شدهٔ %s؛ نیازمند بازگشت وجه (trackId=%s)",
                payment.order.order_number,
                payment.authority,
            )
            return payment
        except OrderError as exc:  # pragma: no cover - غیرمنتظره
            logger.exception("خطا در بروزرسانی سفارش %s", payment.order.order_number)
            raise PaymentError(str(exc)) from exc
        logger.info(
            "پرداخت سفارش %s موفق اعلام شد (مرجع: %s)",
            payment.order.order_number,
            payment.reference_id or "-",
        )
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
