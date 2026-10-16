"""وضعیت‌های سفارش و پرداخت (منبع مشترک وضعیت‌ها برای orders و payments)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class OrderStatus(models.TextChoices):
    """وضعیت چرخه عمر سفارش (تغییر فقط از مسیر کنترل‌شده)."""

    PENDING_PAYMENT = "pending_payment", _("در انتظار پرداخت")
    PAID = "paid", _("پرداخت شده")
    PREPARING = "preparing", _("در حال آماده‌سازی")
    READY_TO_SHIP = "ready_to_ship", _("آماده ارسال")
    SHIPPED = "shipped", _("ارسال شده")
    DELIVERED = "delivered", _("تحویل داده شده")
    CANCELLED = "cancelled", _("لغو شده")
    PAYMENT_FAILED = "payment_failed", _("پرداخت ناموفق")

    @classmethod
    def customer_label(cls, status: str) -> str:
        return dict(cls.choices).get(status, status)


class PaymentStatus(models.TextChoices):
    """وضعیت تراکنش پرداخت."""

    PENDING = "pending", _("در انتظار پرداخت")
    SUCCESSFUL = "successful", _("موفق")
    FAILED = "failed", _("ناموفق")
    CANCELLED = "cancelled", _("لغو شده توسط کاربر")
    EXPIRED = "expired", _("منقضی شده")

    @classmethod
    def customer_label(cls, status: str) -> str:
        return dict(cls.choices).get(status, status)


# گذارهای مجاز وضعیت سفارش (از وضعیت فعلی → وضعیت‌های بعدی مجاز)
ALLOWED_ORDER_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.PENDING_PAYMENT: {
        OrderStatus.PAID,
        OrderStatus.CANCELLED,
        OrderStatus.PAYMENT_FAILED,
    },
    OrderStatus.PAYMENT_FAILED: {OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED, OrderStatus.PAID},
    OrderStatus.PAID: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY_TO_SHIP, OrderStatus.CANCELLED},
    OrderStatus.READY_TO_SHIP: {OrderStatus.SHIPPED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}
