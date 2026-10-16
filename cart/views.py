"""View های سبد خرید."""

from django.contrib import messages
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from products.models import ProductVariant

from .services import (
    CartError,
    add_item,
    cart_totals,
    get_cart,
    remove_item,
    update_item,
)


def _back_url(request, default: str = "store:home"):
    target = request.POST.get("next") or request.GET.get("next") or request.META.get(
        "HTTP_REFERER", ""
    )
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(default)


def _is_hx(request) -> bool:
    """آیا درخواست از سوی HTMX آمده است؟"""
    return request.headers.get("HX-Request", "").lower() == "true"


def _cart_context(request) -> dict:
    """Context صفحه سبد خرید (برای Full Page و Partial هر دو استفاده می‌شود)."""
    cart = get_cart(request, create=False)
    items = []
    if cart is not None:
        items = list(
            cart.items.select_related(
                "variant",
                "variant__product",
                "variant__product__category",
                "variant__product__discount",
                "variant__color",
                "variant__size",
            ).prefetch_related("variant__product__images")
        )
    for item in items:
        item.max_quantity = min(item.available_stock, settings.MAX_QUANTITY_PER_ITEM)
    return {"items": items, "totals": cart_totals(cart, items=items), "cart": cart, "is_cart_fragment": _is_hx(request)}


def cart_detail(request):
    """صفحه سبد خرید."""
    return render(request, "cart/cart.html", _cart_context(request))


@require_POST
def cart_add(request, variant_id: int):
    """افزودن محصول به سبد (فقط POST؛ هیچ داده‌ای از Client برای قیمت پذیرفته نمی‌شود)."""
    variant = get_object_or_404(
        ProductVariant.objects.select_related("product"),
        pk=variant_id,
        is_active=True,
    )
    try:
        quantity = int(request.POST.get("quantity", 1))
    except (TypeError, ValueError):
        messages.error(request, _("تعداد باید یک عدد صحیح باشد."))
        return _back_url(request)

    try:
        add_item(request, variant, quantity)
    except CartError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            _("«%(name)s» به سبد خرید اضافه شد.")
            % {"name": variant.product.name},
        )
    return _back_url(request)


@require_POST
def cart_add_variant(request):
    """
    افزودن به سبد با انتخاب واریانت از طریق فیلد فرم (صفحه جزئیات محصول).

    `variant_id` حتماً باید مقدار عددی معتبر داشته باشد؛ در غیر این صورت
    درخواست رد و پیام خطای مناسب نمایش داده می‌شود.
    """
    raw_id = (request.POST.get("variant_id") or "").strip()
    try:
        variant_id = int(raw_id)
    except (TypeError, ValueError):
        messages.error(request, _("لطفاً ابتدا رنگ و سایز محصول را انتخاب کنید."))
        return _back_url(request)
    return cart_add(request, variant_id)


@require_POST
def cart_update(request, item_id: int):
    try:
        quantity = int(request.POST.get("quantity", 1))
    except (TypeError, ValueError):
        messages.error(request, _("تعداد باید یک عدد صحیح باشد."))
        if _is_hx(request):
            return render(request, "partials/cart_content.html", _cart_context(request))
        return _back_url(request, default="cart:detail")
    try:
        update_item(request, item_id, quantity)
    except CartError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, _("سبد خرید بروزرسانی شد."))

    # با HTMX فقط بلوک محتوای سبد بروزرسانی می‌شود (بدون Reload صفحه)
    if _is_hx(request):
        return render(request, "partials/cart_content.html", _cart_context(request))
    return _back_url(request, default="cart:detail")


@require_POST
def cart_remove(request, item_id: int):
    remove_item(request, item_id)
    messages.success(request, _("کالا از سبد خرید حذف شد."))
    if _is_hx(request):
        return render(request, "partials/cart_content.html", _cart_context(request))
    return _back_url(request, default="cart:detail")
