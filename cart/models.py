"""Model های سبد خرید.

سبد خرید مهمان با Session کاربر و سبد کاربر واردشده با شناسه کاربر گره می‌خورد.
قیمت هیچ‌وقت در این Model ذخیره نمی‌شود؛ همه مقادیر مالی در لحظه نمایش محاسبه می‌شوند.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.models import User
from products.models import ProductVariant


class Cart(models.Model):
    """سبد خرید یک کاربر یا یک بازدیدکننده مهمان."""

    user = models.ForeignKey(
        User,
        verbose_name=_("کاربر"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="carts",
    )
    session_key = models.CharField(
        _("کلید Session"),
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(_("تاریخ ایجاد"), auto_now_add=True)
    updated_at = models.DateTimeField(_("تاریخ بروزرسانی"), auto_now=True)

    class Meta:
        verbose_name = "سبد خرید"
        verbose_name_plural = "سبد خریدها"
        ordering = ("-updated_at",)

    def __str__(self) -> str:
        if self.user:
            return f"سبد {self.user}"
        return f"سبد مهمان {self.session_key or self.pk}"

    @property
    def items_count(self) -> int:
        return sum(item.quantity for item in self.items.all())

    @property
    def is_empty(self) -> bool:
        return not self.items.exists()


class CartItem(models.Model):
    """یک ردیف سبد خرید: واریانت (رنگ + سایز) با تعداد."""

    cart = models.ForeignKey(
        Cart,
        verbose_name=_("سبد خرید"),
        related_name="items",
        on_delete=models.CASCADE,
    )
    variant = models.ForeignKey(
        ProductVariant,
        verbose_name=_("واریانت"),
        related_name="cart_items",
        on_delete=models.PROTECT,
    )
    quantity = models.PositiveIntegerField(_("تعداد"), default=1)
    created_at = models.DateTimeField(_("تاریخ ایجاد"), auto_now_add=True)
    updated_at = models.DateTimeField(_("تاریخ بروزرسانی"), auto_now=True)

    class Meta:
        verbose_name = "قلم سبد خرید"
        verbose_name_plural = "اقلام سبد خرید"
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "variant"], name="unique_variant_in_cart"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="cart_item_quantity_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.variant} × {self.quantity}"

    # ------------------------------------------------------------------
    # محاسبات مالی (کاملاً در Backend)
    # ------------------------------------------------------------------
    @property
    def unit_price(self):
        """قیمت لحظه‌ای واحد (بدون تخفیف)."""
        return self.variant.product.price_info.base_price

    @property
    def final_unit_price(self):
        """قیمت نهایی واحد پس از اعمال تخفیف معتبر."""
        return self.variant.product.price_info.final_price

    @property
    def line_total(self):
        """جمع قیمت این ردیف پس از تخفیف."""
        return self.final_unit_price * self.quantity

    @property
    def line_total_before_discount(self):
        return self.unit_price * self.quantity

    @property
    def line_discount(self):
        return self.line_total_before_discount - self.line_total

    @property
    def available_stock(self) -> int:
        return self.variant.stock
