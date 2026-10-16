"""
سرویس سبد خرید.

تمام محاسبات مالی در Backend انجام می‌شود و هیچ مقداری از Client پذیرفته نمی‌شود.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .models import Cart, CartItem


class CartError(Exception):
    """خطای قابل نمایش به کاربر."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


@dataclass(frozen=True)
class CartTotals:
    """جمع‌بندی مالی سبد خرید (محاسبه‌شده در سرور)."""

    items_count: int
    subtotal: Decimal  # قبل از تخفیف
    discount_total: Decimal
    shipping_fee: Decimal
    total: Decimal  # subtotal - discount + shipping

    @property
    def has_discount(self) -> bool:
        return self.discount_total > 0


def _ensure_session(request) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def get_cart(request, create: bool = True) -> Cart | None:
    """دریافت سبد خرید جاری (کاربر یا مهمان)."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        cart = (
            Cart.objects.filter(user=user).order_by("-updated_at").first()
        )
        if cart is None and create:
            cart = Cart.objects.create(user=user)
        return cart

    session_key = request.session.session_key
    if not session_key:
        if not create:
            return None
        session_key = _ensure_session(request)

    cart = Cart.objects.filter(user=None, session_key=session_key).first()
    if cart is None and create:
        cart = Cart.objects.create(user=None, session_key=session_key)
    return cart


def get_or_create_cart(request) -> Cart:
    cart = get_cart(request, create=True)
    if cart is None:  # pragma: no cover - فقط در حالت خاص Session
        session_key = _ensure_session(request)
        cart, _ = Cart.objects.get_or_create(user=None, session_key=session_key)
    return cart


def add_item(request, variant, quantity: int = 1) -> CartItem:
    """افزودن واریانت به سبد با اعتبارسنجی کامل موجودی و وضعیت محصول."""
    product = variant.product

    if not variant.is_active:
        raise CartError(_("این ترکیب رنگ/سایز غیرفعال است."))
    if not product.is_live:
        raise CartError(_("این محصول در حال حاضر قابل خرید نیست."))
    if quantity <= 0:
        raise CartError(_("تعداد واردشده معتبر نیست."))

    max_allowed = min(variant.stock, settings.MAX_QUANTITY_PER_ITEM)
    if max_allowed <= 0:
        raise CartError(_("این محصول ناموجود است."))
    if quantity > max_allowed:
        raise CartError(
            _("حداکثر تعداد قابل سفارش %(max)s عدد است.")
            % {"max": max_allowed}
        )

    with transaction.atomic():
        cart = get_or_create_cart(request)
        # Serialize mutations with checkout, including inserts into an empty cart.
        cart = Cart.objects.select_for_update().get(pk=cart.pk)

        # قفل ردیف موجود (در صورت وجود)؛ دقت: `select_for_update()` روی
        # Related Manager یک QuerySet برمی‌گرداند و `get_or_create` روی QuerySet
        # فیلد `cart` را ست نمی‌کند؛ به همین دلیل به‌صورت صریح مدیریت می‌شود.
        item = (
            CartItem.objects.select_for_update()
            .filter(cart=cart, variant=variant)
            .first()
        )

        if item is None:
            if quantity > max_allowed:
                raise CartError(
                    _("حداکثر تعداد قابل سفارش %(max)s عدد است.")
                    % {"max": max_allowed}
                )
            return CartItem.objects.create(
                cart=cart, variant=variant, quantity=quantity
            )

        new_quantity = item.quantity + quantity
        if new_quantity > max_allowed:
            raise CartError(
                _("حداکثر تعداد قابل سفارش %(max)s عدد است.")
                % {"max": max_allowed}
            )
        item.quantity = new_quantity
        item.save(update_fields=["quantity", "updated_at"])
    return item


def update_item(request, item_id: int, quantity: int) -> CartItem:
    """بروزرسانی تعداد یک ردیف سبد (فقط سبد خود کاربر)."""
    cart = get_cart(request, create=False)
    if cart is None:
        raise CartError(_("سبد خرید شما یافت نشد."))

    with transaction.atomic():
        cart = Cart.objects.select_for_update().get(pk=cart.pk)
        item = (
            CartItem.objects.select_for_update()
            .select_related("variant", "variant__product")
            .filter(pk=item_id, cart=cart)
            .first()
        )
        if item is None:
            raise CartError(_("این ردیف در سبد خرید شما موجود نیست."))

        if quantity <= 0:
            item.delete()
            return item

        max_allowed = min(item.variant.stock, settings.MAX_QUANTITY_PER_ITEM)
        if not item.variant.is_active or not item.variant.product.is_live:
            raise CartError(_("این محصول دیگر قابل خرید نیست."))
        if quantity > max_allowed:
            raise CartError(
                _("حداکثر تعداد قابل سفارش %(max)s عدد است.")
                % {"max": max_allowed}
            )
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    return item


def remove_item(request, item_id: int) -> None:
    cart = get_cart(request, create=False)
    if cart is None:
        return
    with transaction.atomic():
        cart = Cart.objects.select_for_update().get(pk=cart.pk)
        CartItem.objects.filter(pk=item_id, cart=cart).delete()


def clear_cart(request) -> None:
    cart = get_cart(request, create=False)
    if cart is not None:
        with transaction.atomic():
            cart = Cart.objects.select_for_update().get(pk=cart.pk)
            cart.items.all().delete()


def merge_session_cart(request, user, *, session_key=None) -> None:
    """ادغام سبد مهمان با سبد کاربر پس از ورود موفق."""
    session_key = session_key or request.session.session_key
    if not session_key:
        return
    guest_cart = Cart.objects.filter(user=None, session_key=session_key).first()
    if guest_cart is None:
        return

    with transaction.atomic():
        user_cart, _ = Cart.objects.get_or_create(user=user, session_key=None)
        # Use a stable lock order when two carts are merged.
        list(Cart.objects.select_for_update().filter(pk__in=[guest_cart.pk, user_cart.pk]).order_by("pk"))
        for item in guest_cart.items.select_related("variant"):
            existing = CartItem.objects.filter(cart=user_cart, variant=item.variant).first()
            if existing:
                existing.quantity += item.quantity
                existing.save(update_fields=["quantity", "updated_at"])
            else:
                item.cart = user_cart
                item.save(update_fields=["cart"])
        guest_cart.delete()

    # سبد کاربر باید به‌عنوان سبد فعال Session شناخته شود
    Cart.objects.filter(user=user).exclude(pk=user_cart.pk).delete()


def cart_totals(cart: Cart | None, *, items=None) -> CartTotals:
    """محاسبه جمع سبد خرید در Backend."""
    if cart is None:
        return CartTotals(0, Decimal(0), Decimal(0), Decimal(0), Decimal(0))

    items = items if items is not None else list(
        cart.items.select_related(
            "variant", "variant__product", "variant__product__discount"
        )
    )
    items_count = sum(item.quantity for item in items)
    subtotal = sum((item.line_total_before_discount for item in items), Decimal(0))
    discount_total = sum((item.line_discount for item in items), Decimal(0))

    payable_before_shipping = subtotal - discount_total
    if (
        settings.FREE_SHIPPING_MIN_AMOUNT
        and payable_before_shipping >= settings.FREE_SHIPPING_MIN_AMOUNT
    ):
        shipping_fee = Decimal(0)
    else:
        shipping_fee = Decimal(settings.SHIPPING_FEE) if items else Decimal(0)

    total = payable_before_shipping + shipping_fee
    return CartTotals(
        items_count=items_count,
        subtotal=subtotal,
        discount_total=discount_total,
        shipping_fee=shipping_fee,
        total=total,
    )
