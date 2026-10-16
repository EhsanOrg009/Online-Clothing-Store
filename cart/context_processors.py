"""تعداد اقلام سبد خرید برای نمایش در Header."""

from django.db.models import Sum


def cart_summary(request):
    """تعداد کل اقلام سبد خرید جاری (فقط یک Query)."""
    cached = getattr(request, "_cart_items_count", None)
    if cached is not None:
        return {"cart_items_count": cached}

    from cart.services import get_cart

    count = 0
    cart = get_cart(request, create=False)
    if cart is not None:
        count = cart.items.aggregate(total=Sum("quantity"))["total"] or 0

    request._cart_items_count = count
    return {"cart_items_count": count}
