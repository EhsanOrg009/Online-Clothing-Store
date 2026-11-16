"""پیکربندی Django Admin برای سفارش‌ها."""

from django.contrib import admin, messages
from django import forms
from django.db.models import Count, Q
from django.db import transaction
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .choices import ALLOWED_ORDER_TRANSITIONS, OrderStatus, PaymentStatus
from .models import Order, OrderItem
from .services import OrderError, transition_status


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

    def clean_status(self):
        status = self.cleaned_data["status"]
        if self.instance.pk:
            current = Order.objects.get(pk=self.instance.pk)
            if status != current.status:
                if status not in ALLOWED_ORDER_TRANSITIONS.get(current.status, set()):
                    raise forms.ValidationError(_("این تغییر وضعیت مجاز نیست."))
                if status == OrderStatus.PAID and not current.is_paid:
                    raise forms.ValidationError(_("پرداخت فقط از طریق تأیید درگاه ثبت می‌شود."))
        return status


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = (
        "product_name",
        "variant_label_display",
        "original_unit_price",
        "unit_price",
        "discount_amount",
        "quantity",
        "line_total",
    )
    readonly_fields = fields

    @admin.display(description=_("رنگ / سایز"))
    def variant_label_display(self, obj):
        return obj.variant_label


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    form = OrderAdminForm
    list_display = (
        "order_number",
        "customer_display",
        "status",
        "payment_status",
        "manual_review_receipts",
        "total",
        "items_count",
        "tracking_number",
        "created_at",
    )
    list_filter = ("status", "payment_status", "created_at", "province")
    search_fields = (
        "order_number",
        "receiver_name",
        "receiver_phone",
        "postal_code",
        "tracking_number",
        "customer__phone_number",
        "customer__first_name",
        "customer__last_name",
    )
    readonly_fields = (
        "payment_status",
        "order_number",
        "customer",
        "subtotal",
        "discount_total",
        "shipping_fee",
        "total",
        "receiver_name",
        "receiver_phone",
        "province",
        "city",
        "address_line",
        "postal_code",
        "shipping_method",
        "paid_at",
        "shipped_at",
        "delivered_at",
        "created_at",
        "updated_at",
        "inventory_released",
        "manual_review_receipts",
    )
    inlines = [OrderItemInline]
    date_hierarchy = "created_at"
    list_per_page = 25
    actions = ["mark_ready_to_ship", "mark_shipped", "mark_delivered"]

    fieldsets = (
        (
            _("سفارش"),
            {
                "fields": (
                    "order_number",
                    "customer",
                    "status",
                    "payment_status",
                    "manual_review_receipts",
                    "tracking_number",
                    "customer_note",
                )
            },
        ),
        (
            _("مبالغ (تومان)"),
            {"fields": ("subtotal", "discount_total", "shipping_fee", "total")},
        ),
        (
            _("آدرس ارسال (Snapshot)"),
            {
                "fields": (
                    "receiver_name",
                    "receiver_phone",
                    "province",
                    "city",
                    "address_line",
                    "postal_code",
                    "shipping_method",
                )
            },
        ),
        (
            _("زمان‌ها"),
            {
                "fields": (
                    "expires_at",
                    "inventory_released",
                    "paid_at",
                    "shipped_at",
                    "delivered_at",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    # حذف فیزیکی سفارش ممنوع (حفظ سوابق تاریخی)
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("مشتری"))
    def customer_display(self, obj):
        name = obj.customer.full_name
        return f"{name} ({obj.customer.phone_number})" if name else obj.customer.phone_number

    @admin.display(description=_("اقلام"))
    def items_count(self, obj):
        return sum(item.quantity for item in obj.items.all())

    @admin.display(description=_("رسیدهای نیازمند بررسی دستی"), ordering="manual_review_count")
    def manual_review_receipts(self, obj):
        return obj.manual_review_count

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        """Write only intentional edits to authoritative Order-first locked state."""
        if not change:
            return super().save_model(request, obj, form, change)
        current = Order.objects.select_for_update().get(pk=obj.pk)
        changed = set(form.changed_data)
        if "status" in changed and obj.status != current.status:
            try:
                transition_status(current, obj.status)
            except OrderError as exc:
                obj.refresh_from_db()
                messages.error(request, exc.message)
                return
            messages.success(request, _("وضعیت سفارش %(order)s به «%(status)s» تغییر کرد.") % {
                "order": current.order_number, "status": current.status_label,
            })
        fields = changed & {"tracking_number", "customer_note", "expires_at"}
        if "expires_at" in fields and (
            current.is_paid or current.inventory_released or current.status == OrderStatus.CANCELLED
        ):
            fields.remove("expires_at")
            messages.warning(request, _("مهلت سفارش پرداخت‌شده یا لغوشده تغییر داده نشد."))
        for field in fields:
            setattr(current, field, getattr(obj, field))
        if fields:
            current.save(update_fields=[*sorted(fields), "updated_at"])
        obj.refresh_from_db()

    @admin.action(description=_("علامت‌گذاری به‌عنوان آماده ارسال"))
    def mark_ready_to_ship(self, request, queryset):
        self._bulk_transition(request, queryset, OrderStatus.READY_TO_SHIP)

    @admin.action(description=_("علامت‌گذاری به‌عنوان ارسال‌شده"))
    def mark_shipped(self, request, queryset):
        self._bulk_transition(request, queryset, OrderStatus.SHIPPED)

    @admin.action(description=_("علامت‌گذاری به‌عنوان تحویل‌داده‌شده"))
    def mark_delivered(self, request, queryset):
        self._bulk_transition(request, queryset, OrderStatus.DELIVERED)

    def _bulk_transition(self, request, queryset, new_status):
        succeeded, failed = 0, 0
        for order in queryset:
            try:
                transition_status(order, new_status)
                succeeded += 1
            except OrderError:
                failed += 1
        if succeeded:
            messages.success(request, _(f"{succeeded} سفارش بروزرسانی شد."))
        if failed:
            messages.warning(
                request, _(f"{failed} سفارش به دلیل وضعیت نامعتبر تغییر نکرد.")
            )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            manual_review_count=Count("payments", filter=Q(payments__status=PaymentStatus.MANUAL_REVIEW)),
        ).select_related("customer").prefetch_related("items")


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "product_name",
        "variant_display",
        "unit_price",
        "quantity",
        "line_total",
    )
    list_filter = ("color_name", "size_name")
    search_fields = ("order__order_number", "product_name", "color_name", "sku")
    ordering = ("-id",)
    readonly_fields = [f.name for f in OrderItem._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("رنگ / سایز"))
    def variant_display(self, obj):
        return format_html(
            "<span style='direction:ltr;display:inline-block;'>{}</span>",
            obj.variant_label,
        )
