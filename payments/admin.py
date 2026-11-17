"""نمایش تراکنش‌های پرداخت در پنل مدیریت."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "gateway",
        "amount",
        "status_display",
        "creation_status",
        "needs_refund",
        "reference_id",
        "callback_count",
        "created_at",
        "verified_at",
        "gateway_paid_at",
    )
    list_filter = ("status", "creation_status", "needs_refund", "gateway", "created_at")
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
        "transaction_id",
        "reference_id",
        "error_message",
        "metadata",
        "needs_refund",
        "callback_count",
        "created_at",
        "verified_at",
        "paid_at",
        "gateway_paid_at",
    )
    date_hierarchy = "created_at"
    list_per_page = 25

    def has_add_permission(self, request):
        # تراکنش فقط از مسیر سرویس پرداخت ساخته می‌شود
        return False

    def has_delete_permission(self, request, obj=None):
        # حذف تراکنش ممنوع (حفظ سوابق مالی)
        return False

    @admin.display(description=_("شماره سفارش"))
    def order_number(self, obj):
        return obj.order.order_number

    @admin.display(description=_("وضعیت"), ordering="status")
    def status_display(self, obj):
        return obj.status_label

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("order")
