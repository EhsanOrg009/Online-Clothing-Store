"""
Model تراکنش پرداخت.

نکات امنیتی:
  • هیچ اطلاعات کارت بانکی ذخیره نمی‌شود.
  • Callback به‌تنهایی ملاک پرداخت موفق نیست؛ همیشه Verify انجام می‌شود.
  • فیلد `callback_count` برای رصد Callback های تکراری استفاده می‌شود.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from orders.choices import PaymentStatus
from orders.models import Order


class Payment(models.Model):
    """یک تلاش پرداخت برای یک سفارش."""

    order = models.ForeignKey(
        Order,
        verbose_name=_("سفارش"),
        related_name="payments",
        on_delete=models.PROTECT,
    )
    gateway = models.CharField(_("درگاه پرداخت"), max_length=50, db_index=True)
    amount = models.DecimalField(
        _("مبلغ (تومان)"), max_digits=14, decimal_places=0, db_index=True
    )
    status = models.CharField(
        _("وضعیت"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )
    authority = models.CharField(
        _("کد مرجع درگاه (Authority)"),
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_("توکن یکتایی که درگاه هنگام ایجاد تراکنش برمی‌گرداند."),
    )
    transaction_id = models.CharField(
        _("شناسه تراکنش"), max_length=64, blank=True, db_index=True
    )
    reference_id = models.CharField(
        _("شماره پیگیری"), max_length=64, blank=True, db_index=True
    )
    error_message = models.CharField(_("پیام خطا"), max_length=255, blank=True)
    metadata = models.JSONField(_("داده‌های تکمیلی"), default=dict, blank=True)

    needs_refund = models.BooleanField(
        _("نیازمند بازگشت وجه"),
        default=False,
        db_index=True,
        help_text=_(
            "پرداخت موفق برای سفارشی که پیش از تأیید، لغو یا منقضی شده بود. "
            "باید به‌صورت دستی به مشتری بازگردانده شود."
        ),
    )

    callback_count = models.PositiveIntegerField(
        _("تعداد Callback دریافتی"), default=0
    )
    created_at = models.DateTimeField(_("تاریخ ایجاد"), auto_now_add=True, db_index=True)
    verified_at = models.DateTimeField(_("زمان Verify"), null=True, blank=True)
    paid_at = models.DateTimeField(_("زمان پرداخت"), null=True, blank=True)

    class Meta:
        verbose_name = "تراکنش پرداخت"
        verbose_name_plural = "تراکنش‌های پرداخت"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["order", "status"])]

    def __str__(self) -> str:
        return f"{self.order.order_number} - {self.amount} تومان ({self.status})"

    @property
    def status_label(self) -> str:
        return PaymentStatus.customer_label(self.status)

    @property
    def is_successful(self) -> bool:
        return self.status == PaymentStatus.SUCCESSFUL

    @property
    def is_pending(self) -> bool:
        return self.status == PaymentStatus.PENDING
