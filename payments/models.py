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


class PaymentCreationStatus(models.TextChoices):
    REQUESTING = "requesting", _("در انتظار پاسخ ایجاد درگاه")
    READY = "ready", _("شناسهٔ درگاه ثبت شده")
    UNCERTAIN = "uncertain", _("نتیجهٔ ایجاد نامشخص؛ نیازمند بررسی")
    REJECTED = "rejected", _("درخواست ایجاد رد شده")
    REVIEW = "review", _("ایجاد نیازمند بررسی پشتیبانی")


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
        null=True,
        blank=True,
        help_text=_("توکن یکتایی که درگاه هنگام ایجاد تراکنش برمی‌گرداند."),
    )
    provider_token = models.TextField(
        _("شناسهٔ محرمانهٔ ارائه‌دهنده"), blank=True, default="", editable=False,
        help_text=_("فقط برای تماس سروری؛ در فهرست، گزارش و صفحات مشتری نمایش داده نمی‌شود."),
    )
    reservation_hold = models.BooleanField(
        _("رزرو منتظر تعیین وضعیت درگاه"), default=False, editable=False, db_index=True,
        help_text=_("انقضا و لغو تا تأیید وضعیت نهایی درگاه موجودی را آزاد نمی‌کنند."),
    )
    request_id = models.UUIDField(
        _("شناسهٔ درخواست محلی"), unique=True, null=True, blank=True, editable=False,
        help_text=_("برای اتصال پاسخ/بازگشت به تلاش ثبت‌شده؛ کلید idempotency زیبال نیست."),
    )
    creation_status = models.CharField(
        _("وضعیت ایجاد درگاه"), max_length=20, choices=PaymentCreationStatus.choices,
        default=PaymentCreationStatus.READY, db_index=True, editable=False,
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
            "دریافت موفق وجه برای سفارش لغوشده، پرداخت پس از مهلت یا پرداخت اضافه. "
            "باید به‌صورت دستی به مشتری بازگردانده شود."
        ),
    )

    callback_count = models.PositiveIntegerField(
        _("تعداد Callback دریافتی"), default=0
    )
    created_at = models.DateTimeField(_("تاریخ ایجاد"), auto_now_add=True, db_index=True)
    verified_at = models.DateTimeField(_("زمان Verify"), null=True, blank=True)
    paid_at = models.DateTimeField(_("زمان پرداخت"), null=True, blank=True)
    gateway_paid_at = models.DateTimeField(
        _("زمان پرداخت گزارش‌شده توسط درگاه"), null=True, blank=True,
        help_text=_(
            "paidAt درگاه با timezone مشخص؛ مستقل از زمان ثبت محلی paid_at و verified_at. "
            "زمان نامعتبر/مبهم در metadata برای بررسی دستی حفظ می‌شود."
        ),
    )
    settle_confirmed_at = models.DateTimeField(
        _("نخستین مشاهدهٔ SETTLE در سرور"), null=True, blank=True, editable=False,
        help_text=_("زمان دریافت و تطبیق پاسخ سروری؛ زمان واقعی پرداخت مشتری نیست و فقط یک بار ثبت می‌شود."),
    )
    reconciliation_lease_expires_at = models.DateTimeField(
        _("پایان مهلت پردازش درگاه"), null=True, blank=True, editable=False, db_index=True,
        help_text=_("مالک پردازش در metadata ثبت است؛ پس از این مهلت، استعلام تازه می‌تواند پردازش را بازیابی کند."),
    )
    reconciliation_attempted_at = models.DateTimeField(
        _("آخرین شروع بررسی درگاه"), null=True, blank=True, editable=False,
    )

    class Meta:
        verbose_name = "تراکنش پرداخت"
        verbose_name_plural = "تراکنش‌های پرداخت"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["order", "status"]), models.Index(
            fields=["gateway", "reconciliation_attempted_at", "created_at"], name="payment_reconcile_due",
        )]

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

    @property
    def requires_manual_review(self) -> bool:
        return self.status == PaymentStatus.MANUAL_REVIEW


class PaymentMaintenanceJob(models.Model):
    """One shared scheduler lease/run record; no payment or customer secrets."""

    name = models.CharField(_("نام وظیفه"), primary_key=True, max_length=64, editable=False)
    lease_id = models.UUIDField(null=True, editable=False)
    lease_expires_at = models.DateTimeField(_("پایان مهلت اجرای وظیفه"), null=True, editable=False)
    last_started_at = models.DateTimeField(_("آخرین شروع"), null=True, editable=False)
    last_finished_at = models.DateTimeField(_("آخرین پایان"), null=True, editable=False)
    last_success_at = models.DateTimeField(_("آخرین اجرای موفق"), null=True, editable=False)
    last_expiration_at = models.DateTimeField(_("آخرین اجرای موفق انقضا"), null=True, editable=False)
    last_error = models.CharField(_("آخرین خطای پالایش‌شده"), max_length=255, blank=True, editable=False)
    summary = models.JSONField(_("نتیجهٔ آخرین اجرا"), default=dict, editable=False)

    class Meta:
        verbose_name = "وضعیت وظیفهٔ نگهداری پرداخت"
        verbose_name_plural = "وضعیت وظایف نگهداری پرداخت"

    def __str__(self):
        return self.name
