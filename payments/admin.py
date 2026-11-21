"""نمایش تراکنش‌های پرداخت در پنل مدیریت."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from copy import deepcopy
import json

from .models import Payment, PaymentMaintenanceJob


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "gateway",
        "amount",
        "status_display",
        "creation_status",
        "reservation_hold",
        "needs_refund",
        "reference_id",
        "callback_count",
        "created_at",
        "verified_at",
        "gateway_paid_at",
        "settle_confirmed_at",
    )
    list_filter = ("status", "creation_status", "reservation_hold", "needs_refund", "gateway", "created_at")
    exclude = ("provider_token", "metadata")
    search_fields = (
        "authority",
        "request_id",
        "transaction_id",
        "reference_id",
        "order__order_number",
        "order__customer__phone_number",
    )
    readonly_fields = (
        "order",
        "gateway",
        "amount",
        "status",
        "authority",
        "request_id",
        "creation_status",
        "reservation_hold",
        "transaction_id",
        "reference_id",
        "error_message",
        "metadata_display",
        "needs_refund",
        "callback_count",
        "created_at",
        "verified_at",
        "paid_at",
        "gateway_paid_at",
        "settle_confirmed_at",
        "reconciliation_attempted_at",
        "reconciliation_lease_expires_at",
        "review_reason",
    )
    date_hierarchy = "created_at"
    list_per_page = 25

    def has_add_permission(self, request):
        # تراکنش فقط از مسیر سرویس پرداخت ساخته می‌شود
        return False

    def has_delete_permission(self, request, obj=None):
        # حذف تراکنش ممنوع (حفظ سوابق مالی)
        return False

    def has_change_permission(self, request, obj=None):
        # Even an empty readonly form can save a stale model over a new receipt
        # or lease. Financial writes belong exclusively to locked services.
        return False

    @admin.display(description=_("شماره سفارش"))
    def order_number(self, obj):
        return obj.order.order_number

    @admin.display(description=_("وضعیت"), ordering="status")
    def status_display(self, obj):
        return obj.status_label

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("order")

    @admin.display(description=_("داده‌های تکمیلی پالایش‌شده"))
    def metadata_display(self, obj):
        data = deepcopy(obj.metadata) if isinstance(obj.metadata, dict) else {}
        # Hosted URLs can carry a confidential token in the path or query.
        if "redirect_url" in data:
            data["redirect_url"] = "نشانی محرمانهٔ درگاه؛ فقط در صفحهٔ انتقال استفاده می‌شود."
        return format_html("<pre>{}</pre>", json.dumps(data, ensure_ascii=False, indent=2))

    @admin.display(description=_("دلیل بررسی پشتیبانی"))
    def review_reason(self, obj):
        reason = obj.metadata.get("torobpay", {}).get("manual_block", "") if isinstance(obj.metadata, dict) else ""
        return {"confirmation_after_deadline": "تأیید سروری در مهلت رزرو دریافت نشد",
                "settlement_evidence_unavailable": "زمان یا شواهد قطعی مشاهدهٔ اول در دسترس نیست",
                "remote_amount_mismatch": "مبلغ گزارش‌شده با مبلغ ثبت‌شده مطابقت ندارد",
                "remote_transaction_mismatch": "شناسهٔ تراکنش مطابقت ندارد",
                "stored_token_mismatch": "شناسهٔ محرمانه به تلاش پرداخت مطابقت ندارد",
                "order_cancelled_or_inventory_released": "سفارش لغو یا رزرو آزاد شده است",
                "identity_changed_during_http": "اطلاعات سفارش یا پرداخت هنگام استعلام تغییر کرده است",
                "gateway_receipt_conflict": "گزارش تازه با رسید تأییدشدهٔ قبلی مغایر است"}.get(reason, reason or "—")


@admin.register(PaymentMaintenanceJob)
class PaymentMaintenanceJobAdmin(admin.ModelAdmin):
    list_display = ("name", "last_started_at", "last_success_at", "last_expiration_at", "lease_expires_at", "last_error")
    readonly_fields = ("name", "lease_expires_at", "last_started_at", "last_finished_at", "last_success_at",
                       "last_expiration_at", "last_error", "summary")
    exclude = ("lease_id",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
