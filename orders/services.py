"""
سرویس سفارش و مدیریت موجودی.

نکات امنیتی/دقت:
  • هیچ مقدار مالی از Client پذیرفته نمی‌شود؛ همه چیز از Database محاسبه می‌شود.
  • ثبت سفارش در یک تراکنش اتمیک و با قفل ردیف (SELECT ... FOR UPDATE) انجام می‌شود.
  • کاهش موجودی با شرط SQL (`stock >= quantity`) انجام می‌شود تا Overselling رخ ندهد.
  • Snapshot قیمت/نام/آدرس در لحظه ثبت سفارش ذخیره می‌شود.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from cart.models import Cart
from products.models import ProductVariant

from .choices import ALLOWED_ORDER_TRANSITIONS, OrderStatus, PaymentStatus
from .models import Order, OrderItem
from .shipping import ShippingOption, default_shipping_option

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


class OrderError(Exception):
    """خطای قابل نمایش به کاربر در فرآیند سفارش."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


class OrderNotPayable(OrderError):
    """سفارش در وضعیتی است که دیگر قابل پرداخت نیست (لغو یا منقضی‌شده).

    اگر Callbackِ موفقِ دیرهنگام برای چنین سفارشی برسد، پول در درگاه گرفته شده
    اما سفارش نباید «پرداخت‌شده» شود؛ باید برای بازگشت وجه دستی علامت بخورد.
    """


@transaction.atomic
def create_order(
    *,
    customer,
    cart: Cart,
    address,
    shipping_option: ShippingOption | None = None,
    customer_note: str = "",
) -> Order:
    """
    ثبت سفارش از سبد خرید به‌صورت اتمیک.

    مراحل:
      ۱) قفل ردیف سبد و واریانت‌ها
      ۲) بازبینی موجودی و وضعیت انتشار محصول
      ۳) محاسبه مجدد قیمت‌ها (تخفیف در همان لحظه)
      ۴) کاهش موجودی (رزرو)
      ۵) ساخت Order و OrderItem همراه Snapshot
    """
    shipping_option = shipping_option or default_shipping_option()

    cart = Cart.objects.select_for_update().get(pk=cart.pk)
    if cart.user_id != customer.pk or address.user_id != customer.pk:
        raise OrderError(_("سبد خرید و آدرس باید متعلق به مشتری سفارش باشند."))

    cart_items = list(
        cart.items.select_for_update()
        .select_related("variant", "variant__product", "variant__color", "variant__size")
        .order_by("id")
    )
    if not cart_items:
        raise OrderError(_("سبد خرید شما خالی است."))

    order_items: list[OrderItem] = []
    subtotal = ZERO
    discount_total = ZERO

    for item in cart_items:
        variant = (
            ProductVariant.objects.select_for_update()
            .select_related("product", "product__category", "product__discount", "color", "size")
            .prefetch_related("product__images")
            .filter(pk=item.variant_id, is_active=True)
            .first()
        )
        if variant is None:
            raise OrderError(_("یکی از محصولات سبد خرید دیگر موجود نیست."))

        product = variant.product
        if not product.is_live:
            raise OrderError(
                _("محصول «%(name)s» در حال حاضر قابل خرید نیست.")
                % {"name": product.name}
            )
        if item.quantity > variant.stock:
            raise OrderError(
                _("موجودی «%(name)s - %(variant)s» کافی نیست (موجودی: %(stock)s).")
                % {
                    "name": product.name,
                    "variant": variant.label,
                    "stock": variant.stock,
                }
            )
        if item.quantity > settings.MAX_QUANTITY_PER_ITEM:
            raise OrderError(
                _("حداکثر تعداد قابل سفارش برای هر کالا %(max)s عدد است.")
                % {"max": settings.MAX_QUANTITY_PER_ITEM}
            )

        # قیمت در همین لحظه و فقط در Backend محاسبه می‌شود
        price_info = product.price_info
        original = price_info.base_price
        final = price_info.final_price
        quantity = item.quantity
        line_discount = (original - final) * quantity

        # کاهش موجودی (رزرو) با شرط امنیتی
        if not variant.decrement_stock(quantity):
            raise OrderError(
                _("موجودی «%(name)s - %(variant)s» به پایان رسیده است.")
                % {"name": product.name, "variant": variant.label}
            )

        primary = product.primary_image
        order_items.append(
            OrderItem(
                product=product,
                variant=variant,
                product_name=product.name,
                color_name=variant.color.name,
                size_name=variant.size.name,
                sku=variant.sku or "",
                product_image=primary.image.url if primary else "",
                original_unit_price=original,
                unit_price=final,
                discount_amount=line_discount,
                quantity=quantity,
                line_total=final * quantity,
            )
        )
        subtotal += original * quantity
        discount_total += line_discount

    payable_before_shipping = subtotal - discount_total
    shipping_fee = shipping_option.fee_for(payable_before_shipping)
    total = payable_before_shipping + shipping_fee

    reservation_minutes = settings.ORDER_RESERVATION_MINUTES

    order = Order.objects.create(
        customer=customer,
        status=OrderStatus.PENDING_PAYMENT,
        payment_status=PaymentStatus.PENDING,
        subtotal=subtotal,
        discount_total=discount_total,
        shipping_fee=shipping_fee,
        total=total,
        receiver_name=address.receiver_name,
        receiver_phone=address.receiver_phone,
        province=address.province,
        city=address.city,
        address_line=address.address_line,
        postal_code=address.postal_code,
        shipping_method=shipping_option.label,
        customer_note=(customer_note or "").strip()[:500],
        expires_at=timezone.now() + timedelta(minutes=reservation_minutes)
        if reservation_minutes
        else None,
    )
    for item in order_items:
        item.order = order
    OrderItem.objects.bulk_create(order_items)

    cart.items.all().delete()

    logger.info(
        "سفارش %s برای کاربر %s ثبت شد (مبلغ: %s)",
        order.order_number,
        customer.pk,
        total,
    )
    return order


def _refresh_instance(target: Order, source: Order) -> None:
    """
    مقادیر دوباره‌خوانده‌شده از دیتابیس را روی Instance اصلی اعمال می‌کند.

    سرویس‌ها نسخه قفل‌شده را از DB می‌گیرند؛ بدون این تابع، نمونه‌ای که
    به تابع پاس شده وضعیت کهنه نگه می‌دارد و فراخواننده دچار باگ می‌شود.
    """
    for field in source._meta.concrete_fields:
        if field.primary_key or field.auto_created:
            continue
        setattr(target, field.attname, getattr(source, field.attname))


@transaction.atomic
def mark_order_paid(order: Order, paid_at=None) -> Order:
    """ثبت پرداخت موفق سفارش (Idempotent؛ فراخوانی مجدد اثری ندارد)."""
    original = order
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.payment_status == PaymentStatus.SUCCESSFUL:
        _refresh_instance(original, order)
        return original

    # دفاع نهایی: سفارش لغو/منقضی‌شده نباید «پرداخت‌شده» شود. اگر پول در درگاه
    # گرفته شده، فراخواننده (process_callback) این استثنا را گرفته و تراکنش را
    # برای بازگشت وجه علامت می‌زند؛ وضعیت سفارش دست‌نخورده می‌ماند.
    if order.status == OrderStatus.CANCELLED:
        raise OrderNotPayable(
            _("سفارش لغو یا منقضی‌شده قابل علامت‌گذاری به‌عنوان پرداخت‌شده نیست.")
        )

    order.payment_status = PaymentStatus.SUCCESSFUL
    if order.status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED):
        order.status = OrderStatus.PAID
    order.paid_at = paid_at or timezone.now()
    order.expires_at = None
    order.save(
        update_fields=[
            "payment_status",
            "status",
            "paid_at",
            "expires_at",
            "updated_at",
        ]
    )
    logger.info("سفارش %s پرداخت شد", order.order_number)
    _refresh_instance(original, order)
    return original


def _restore_stock(order: Order) -> None:
    """آزادسازی موجودی رزروشده یک سفارش (فقط یک بار اجرا می‌شود)."""
    for item in order.items.select_related("variant"):
        if item.variant_id and item.quantity:
            item.variant.restore_stock(item.quantity)


def _apply_cancellation(order: Order, *, mark_payment_cancelled: bool) -> None:
    """سفارشِ از قبل قفل‌شده را لغو می‌کند و موجودی رزروشده را آزاد می‌کند.

    فقط Instance را در حافظه تغییر می‌دهد؛ ذخیره‌سازی بر عهده فراخواننده است تا
    هر مسیر (لغو مستقیم یا گذار وضعیت) بتواند `update_fields` خودش را انتخاب کند.
    """
    _restore_stock(order)
    order.status = OrderStatus.CANCELLED
    if mark_payment_cancelled and order.payment_status == PaymentStatus.PENDING:
        order.payment_status = PaymentStatus.CANCELLED
    order.expires_at = None


@transaction.atomic
def cancel_order(order: Order, *, mark_payment_cancelled: bool = False) -> Order:
    """
    لغو سفارش و آزادسازی موجودی.

    فقط برای سفارش‌هایی که هنوز ارسال نشده‌اند مجاز است.
    """
    original = order
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
        raise OrderError(_("سفارش ارسال‌شده قابل لغو نیست."))
    if order.status == OrderStatus.CANCELLED:
        _refresh_instance(original, order)
        return original  # Idempotent

    _apply_cancellation(order, mark_payment_cancelled=mark_payment_cancelled)
    order.save(update_fields=["status", "payment_status", "expires_at", "updated_at"])
    logger.info("سفارش %s لغو شد", order.order_number)
    _refresh_instance(original, order)
    return original


def expire_due_orders() -> int:
    """
    انقضای سفارش‌های پرداخت‌نشده: لغو + آزادسازی موجودی.

    این تابع باید به‌صورت دوره‌ای (مثلاً هر ۵ دقیقه با cron) اجرا شود:
        uv run python manage.py expire_pending_orders
    """
    expired = 0
    now = timezone.now()
    for order in Order.objects.due_for_expiry(now):
        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order.pk)
                if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED) or not order.is_expired:
                    continue
                _restore_stock(order)
                order.status = OrderStatus.CANCELLED
                order.payment_status = PaymentStatus.EXPIRED
                order.save(
                    update_fields=[
                        "status",
                        "payment_status",
                        "updated_at",
                    ]
                )
            expired += 1
            logger.info("سفارش %s به دلیل عدم پرداخت منقضی شد", order.order_number)
        except Exception:  # pragma: no cover - ایمنی حلقه
            logger.exception("خطا در انقضای سفارش %s", order.order_number)
    return expired


@transaction.atomic
def transition_status(order: Order, new_status: str) -> Order:
    """تغییر کنترل‌شدهٔ وضعیت سفارش توسط ادمین (اتمیک + قفل ردیف).

    گذار به «لغو» موجودی رزروشده را نیز آزاد می‌کند (در گذشته آزاد نمی‌شد و
    باعث نشت موجودی می‌شد).
    """
    original = order
    order = Order.objects.select_for_update().get(pk=order.pk)
    if new_status == order.status:
        _refresh_instance(original, order)
        return original
    allowed = ALLOWED_ORDER_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise OrderError(
            _("تغییر وضعیت از «%(current)s» به «%(new)s» مجاز نیست.")
            % {"current": order.status_label, "new": OrderStatus.customer_label(new_status)}
        )
    if new_status == OrderStatus.CANCELLED:
        _apply_cancellation(order, mark_payment_cancelled=True)
    else:
        order.status = new_status
        if new_status == OrderStatus.SHIPPED and not order.shipped_at:
            order.shipped_at = timezone.now()
        if new_status == OrderStatus.DELIVERED and not order.delivered_at:
            order.delivered_at = timezone.now()
    order.save()
    logger.info("وضعیت سفارش %s به %s تغییر کرد", order.order_number, new_status)
    _refresh_instance(original, order)
    return original
