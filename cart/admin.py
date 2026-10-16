"""نمایش سبد خریدها در پنل مدیریت (برای پشتیبانی)."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    fields = ("variant", "quantity", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("owner", "items_count_display", "created_at", "updated_at")
    list_filter = ("created_at",)
    search_fields = ("user__phone_number", "user__first_name", "session_key")
    readonly_fields = ("user", "session_key", "created_at", "updated_at")
    inlines = [CartItemInline]
    ordering = ("-updated_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user").prefetch_related("items")

    @admin.display(description=_("کاربر"))
    def owner(self, obj):
        return str(obj.user) if obj.user_id else f"مهمان ({obj.session_key or '—'})"

    @admin.display(description=_("تعداد اقلام"))
    def items_count_display(self, obj):
        return obj.items_count
