"""پیکربندی Django Admin برای محصولات، واریانت‌ها و تخفیف‌ها."""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import (
    Category,
    Color,
    Discount,
    Product,
    ProductImage,
    ProductVariant,
    Size,
)


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ("image", "preview", "alt_text", "is_primary", "sort_order")
    readonly_fields = ("preview",)
    ordering = ("-is_primary", "sort_order")

    @admin.display(description=_("پیش‌نمایش"))
    def preview(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="height:48px;width:auto;border-radius:6px;" />',
            obj.image.url,
        )


class DiscountInline(admin.StackedInline):
    model = Discount
    extra = 0
    max_num = 1
    fields = ("name", "discount_type", "value", "is_active", "starts_at", "ends_at")


class VariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1
    fields = ("color", "size", "sku", "stock", "is_active")
    autocomplete_fields = ("color", "size")
    ordering = ("color__sort_order", "size__sort_order")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "base_price",
        "final_price_display",
        "stock_display",
        "is_published",
        "is_featured",
        "is_active",
        "archived_at",
        "created_at",
    )
    list_filter = (
        "is_published",
        "is_featured",
        "is_active",
        "category",
        ("archived_at", admin.EmptyFieldListFilter),
        "created_at",
    )
    search_fields = ("name", "slug", "description", "category__name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("archived_at", "created_at", "updated_at", "final_price_readonly")
    inlines = [ProductImageInline, VariantInline, DiscountInline]
    actions = ["publish_products", "unpublish_products", "archive_products", "restore_products"]
    list_per_page = 25
    date_hierarchy = "created_at"
    fieldsets = (
        (
            _("اطلاعات اصلی"),
            {"fields": ("name", "slug", "category", "description")},
        ),
        (
            _("قیمت و تخفیف"),
            {"fields": ("base_price", "final_price_readonly")},
        ),
        (
            _("وضعیت نمایش"),
            {
                "fields": ("is_published", "is_active", "is_featured"),
                "description": _(
                    "محصول فقط زمانی در فروشگاه دیده می‌شود که «منتشر شود» فعال و غیرآرشیو باشد."
                ),
            },
        ),
        (
            _("تاریخ‌ها"),
            {"fields": ("archived_at", "created_at", "updated_at")},
        ),
    )

    @admin.display(description=_("قیمت نهایی"), ordering="base_price")
    def final_price_display(self, obj):
        info = obj.price_info
        if info.has_discount:
            return format_html(
                "<strong>{}</strong> <span style='color:#16a34a;'>({}٪)</span>",
                f"{info.final_price:,}",
                info.discount_percent,
            )
        return f"{info.final_price:,} تومان"

    @admin.display(description=_("قیمت نهایی"))
    def final_price_readonly(self, obj):
        return self.final_price_display(obj)

    @admin.display(description=_("موجودی"))
    def stock_display(self, obj):
        total = obj.stock_quantity
        if total == 0:
            return format_html('<span class="shop-pill is-alert">{}</span>', _("ناموجود"))
        return f"{total} عدد"

    @admin.action(description=_("انتشار محصولات انتخاب‌شده"))
    def publish_products(self, request, queryset):
        updated = queryset.update(is_published=True)
        self.message_user(request, _(f"{updated} محصول منتشر شد."))

    @admin.action(description=_("لغو انتشار محصولات انتخاب‌شده"))
    def unpublish_products(self, request, queryset):
        updated = queryset.update(is_published=False)
        self.message_user(request, _(f"{updated} محصول از انتشار خارج شد."))

    @admin.action(description=_("آرشیو محصولات انتخاب‌شده"))
    def archive_products(self, request, queryset):
        from django.utils import timezone

        now = timezone.now()
        updated = queryset.update(
            archived_at=now, is_published=False, is_active=False
        )
        self.message_user(request, _(f"{updated} محصول آرشیو شد."))

    @admin.action(description=_("خروج از آرشیو"))
    def restore_products(self, request, queryset):
        updated = queryset.update(archived_at=None, is_active=True)
        self.message_user(request, _(f"{updated} محصول از آرشیو خارج شد."))

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("category")
            .prefetch_related("variants", "images", "discount")
        )


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "parent", "is_active", "sort_order", "products_count")
    list_filter = ("is_active", "parent")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("sort_order", "is_active")
    ordering = ("sort_order", "name")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").with_live_product_count()

    @admin.display(description=_("تعداد محصولات"))
    def products_count(self, obj):
        return obj.product_count


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("name", "color_preview", "hex_code", "is_active", "sort_order")
    list_editable = ("hex_code", "is_active", "sort_order")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description=_("رنگ"))
    def color_preview(self, obj):
        return format_html(
            '<span style="display:inline-block;width:18px;height:18px;border-radius:50%;'
            'background:{};border:1px solid #d1d5db;"></span>',
            obj.hex_code,
        )


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "color",
        "size",
        "sku",
        "stock",
        "stock_status",
        "is_active",
    )
    list_filter = ("is_active", "color", "size", "product__category")
    search_fields = ("product__name", "sku", "color__name", "size__name")
    autocomplete_fields = ("product", "color", "size")
    list_editable = ("stock", "is_active")
    list_per_page = 30

    @admin.display(description=_("وضعیت موجودی"))
    def stock_status(self, obj):
        if obj.stock <= 0:
            return format_html('<span class="shop-pill is-alert">{}</span>', _("ناموجود"))
        if obj.stock <= 3:
            return format_html('<span class="shop-pill is-warn">{} {}</span>', obj.stock, _("عدد (کم)"))
        return format_html('<span class="shop-pill is-success">{} {}</span>', obj.stock, _("عدد"))

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("product", "color", "size")


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "product",
        "discount_type",
        "value",
        "is_active",
        "starts_at",
        "ends_at",
        "valid_now",
    )
    list_filter = ("discount_type", "is_active", "starts_at", "ends_at")
    search_fields = ("name", "product__name")
    autocomplete_fields = ("product",)

    @admin.display(description=_("فعال"), boolean=True)
    def valid_now(self, obj):
        return obj.is_valid_now
