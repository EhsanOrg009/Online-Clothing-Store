"""
Model های سفارش.

نکات مهم:
  • اطلاعات محصول، رنگ/سایز، قیمت و آدرس به‌صورت Snapshot ذخیره می‌شوند تا
    تغییرات بعدی محصول/پروفایل، سفارش‌های قدیمی را خراب نکند.
  • مبالغ با Decimal ذخیره می‌شوند؛ هیچ‌جا از float برای پول استفاده نشده است.
  • موجودی هنگام ثبت سفارش رزرو (کم) می‌شود و در صورت انقضا آزاد می‌گردد.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import User
from common.models import TimeStampedModel
from products.models import Product, ProductVariant

from .choices import OrderStatus, PaymentStatus

# حروف بدون ابهام برای شماره سفارش خوانا
_ORDER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_order_number() -> str:
    """شماره سفارش خوانا و یکتا (بدون نمایش ID خام دیتابیس)."""
    date_part = timezone.localdate().strftime("%Y%m%d")
    for _ in range(30):
        suffix = "".join(secrets.choice(_ORDER_ALPHABET) for _ in range(6))
        candidate = f"ORD-{date_part}-{suffix}"
        if not Order.objects.filter(order_number=candidate).exists():
            return candidate
    raise RuntimeError("تولید شماره سفارش یکتا ناموفق بود.")


class OrderQuerySet(models.QuerySet):
    def for_customer(self, user):
        return self.filter(customer=user)

    def paid(self):
        return self.filter(payment_status=PaymentStatus.SUCCESSFUL)

    def due_for_expiry(self, now=None):
        now = now or timezone.now()
        return self.filter(
            status__in=[OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED],
            expires_at__isnull=False,
            expires_at__lt=now,
        )


class Order(TimeStampedModel):
    """سفارش خرید مشتری."""

    order_number = models.CharField(
        _("شماره سفارش"), max_length=32, unique=True, db_index=True
    )
    customer = models.ForeignKey(
        User,
        verbose_name=_("مشتری"),
        on_delete=models.PROTECT,
        related_name="orders",
        db_index=True,
    )
    status = models.CharField(
        _("وضعیت سفارش"),
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING_PAYMENT,
        db_index=True,
    )
    payment_status = models.CharField(
        _("وضعیت پرداخت"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )

    # مبالغ (تومان)
    subtotal = models.DecimalField(_("جمع اقلام"), max_digits=14, decimal_places=0, default=Decimal(0))
    discount_total = models.DecimalField(
        _("تخفیف"), max_digits=14, decimal_places=0, default=Decimal(0)
    )
    shipping_fee = models.DecimalField(
        _("هزینه ارسال"), max_digits=14, decimal_places=0, default=Decimal(0)
    )
    total = models.DecimalField(
        _("مبلغ نهایی"), max_digits=14, decimal_places=0, default=Decimal(0), db_index=True
    )

    # Snapshot آدرس (تغییر بعدی آدرس کاربر روی سفارش اثری ندارد)
    receiver_name = models.CharField(_("نام گیرنده"), max_length=120)
    receiver_phone = models.CharField(_("تلفن گیرنده"), max_length=11)
    province = models.CharField(_("استان"), max_length=64)
    city = models.CharField(_("شهر"), max_length=64)
    address_line = models.TextField(_("آدرس"))
    postal_code = models.CharField(_("کد پستی"), max_length=10)

    shipping_method = models.CharField(_("روش ارسال"), max_length=100)
    tracking_number = models.CharField(
        _("کد رهگیری مرسوله"), max_length=64, blank=True, db_index=True
    )
    customer_note = models.TextField(_("یادداشت مشتری"), blank=True)

    expires_at = models.DateTimeField(
        _("مهلت پرداخت"), null=True, blank=True, db_index=True,
        help_text=_("پس از این زمان سفارش لغو و موجودی آزاد می‌شود."),
    )
    paid_at = models.DateTimeField(_("زمان پرداخت"), null=True, blank=True)
    shipped_at = models.DateTimeField(_("زمان ارسال"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("زمان تحویل"), null=True, blank=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        verbose_name = "سفارش"
        verbose_name_plural = "سفارش‌ها"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["status", "payment_status"]),
        ]

    def __str__(self) -> str:
        return self.order_number

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = generate_order_number()
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # وضعیت‌ها
    # ------------------------------------------------------------------
    @property
    def is_paid(self) -> bool:
        return self.payment_status == PaymentStatus.SUCCESSFUL

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and timezone.now() > self.expires_at)

    @property
    def can_pay(self) -> bool:
        """آیا امکان پرداخت (یا پرداخت مجدد) وجود دارد؟"""
        return self.status in (
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.PAYMENT_FAILED,
        ) and not self.is_expired

    @property
    def status_label(self) -> str:
        return OrderStatus.customer_label(self.status)

    @property
    def payment_status_label(self) -> str:
        return PaymentStatus.customer_label(self.payment_status)

    @property
    def address_snapshot(self) -> str:
        return (
            f"{self.province}، {self.city}، {self.address_line} "
            f"(کد پستی: {self.postal_code})"
        )

    @property
    def latest_payment(self):
        return self.payments.order_by("-created_at").first()


class OrderItem(models.Model):
    """یک ردیف سفارش با Snapshot کامل اطلاعات در زمان خرید."""

    order = models.ForeignKey(
        Order, verbose_name=_("سفارش"), related_name="items", on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        Product,
        verbose_name=_("محصول"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )
    variant = models.ForeignKey(
        ProductVariant,
        verbose_name=_("واریانت"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )

    # Snapshot ها
    product_name = models.CharField(_("نام محصول"), max_length=200)
    color_name = models.CharField(_("رنگ"), max_length=60)
    size_name = models.CharField(_("سایز"), max_length=30)
    sku = models.CharField(_("کد کالا"), max_length=64, blank=True)
    product_image = models.CharField(_("تصویر محصول"), max_length=300, blank=True)

    # مبالغ (تومان)
    original_unit_price = models.DecimalField(
        _("قیمت واحد قبل از تخفیف"), max_digits=14, decimal_places=0
    )
    unit_price = models.DecimalField(
        _("قیمت واحد پس از تخفیف"), max_digits=14, decimal_places=0
    )
    discount_amount = models.DecimalField(
        _("تخفیف این ردیف"), max_digits=14, decimal_places=0, default=Decimal(0)
    )
    quantity = models.PositiveIntegerField(_("تعداد"))
    line_total = models.DecimalField(
        _("جمع ردیف"), max_digits=14, decimal_places=0
    )

    class Meta:
        verbose_name = "قلم سفارش"
        verbose_name_plural = "اقلام سفارش"
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.product_name} × {self.quantity}"

    @property
    def variant_label(self) -> str:
        return f"{self.color_name} / {self.size_name}"

    def clean(self):
        super().clean()
        if self.quantity is not None and self.quantity <= 0:
            from django.core.exceptions import ValidationError

            raise ValidationError({"quantity": _("تعداد باید بزرگ‌تر از صفر باشد.")})
        if self.original_unit_price is not None and self.original_unit_price < 0:
            from django.core.exceptions import ValidationError

            raise ValidationError({"original_unit_price": _("قیمت نمی‌تواند منفی باشد.")})

    @staticmethod
    def compute_line_total(unit_price, quantity: int) -> Decimal:
        return Decimal(unit_price) * Decimal(quantity)
