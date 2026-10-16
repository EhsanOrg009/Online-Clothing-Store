"""View های Checkout و سفارش‌های کاربر."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods, require_POST

from accounts.decorators import login_required_message
from accounts.forms import AddressForm
from accounts.models import Address
from cart.services import cart_totals, get_cart

from .forms import CheckoutNoteForm
from .models import Order
from .services import OrderError, create_order
from .shipping import get_shipping_options

ADDRESS_SESSION_KEY = "checkout_address_id"


# ---------------------------------------------------------------------
# ابزارها
# ---------------------------------------------------------------------
def _active_cart(request):
    cart = get_cart(request, create=False)
    if cart is None or not cart.items.exists():
        return None
    return cart


def _selected_address(request) -> Address | None:
    """آدرس انتخاب‌شده در Checkout (فقط از آدرس‌های خود کاربر)."""
    address_id = request.session.get(ADDRESS_SESSION_KEY)
    address = None
    if address_id:
        address = Address.objects.filter(pk=address_id, user=request.user).first()
    if address is None:
        address = (
            request.user.addresses.filter(is_default=True).first()
            or request.user.addresses.first()
        )
        if address is not None:
            request.session[ADDRESS_SESSION_KEY] = address.pk
    return address


def _require_cart_or_redirect(request):
    cart = _active_cart(request)
    if cart is None:
        messages.info(request, _("سبد خرید شما خالی است."))
        return None, redirect("cart:detail")
    return cart, None


# ---------------------------------------------------------------------
# مرحله ۱: انتخاب آدرس
# ---------------------------------------------------------------------
@login_required_message
@require_http_methods(["GET", "POST"])
def checkout_address(request):
    cart, redirect_response = _require_cart_or_redirect(request)
    if redirect_response:
        return redirect_response

    addresses = request.user.addresses.all()
    selected = _selected_address(request)
    address_form = AddressForm((request.POST if request.method == "POST" else None))

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "select":
            try:
                address_id = int(request.POST.get("address_id", ""))
            except (TypeError, ValueError):
                address_id = None
            address = Address.objects.filter(pk=address_id, user=request.user).first()
            if address is None:
                messages.error(request, _("آدرس انتخاب‌شده معتبر نیست."))
            else:
                request.session[ADDRESS_SESSION_KEY] = address.pk
                return redirect("orders:checkout_review")

        elif action == "add_address" and address_form.is_valid():
            address = address_form.save(commit=False)
            address.user = request.user
            if not addresses.exists():
                address.is_default = True
            address.save()
            request.session[ADDRESS_SESSION_KEY] = address.pk
            messages.success(request, _("آدرس ذخیره شد."))
            return redirect("orders:checkout_review")

    totals = cart_totals(cart)
    return render(
        request,
        "orders/delivery.html",
        {
            "addresses": addresses,
            "selected": selected,
            "address_form": address_form,
            "totals": totals,
            "shipping_options": get_shipping_options(),
            "step": 1,
        },
    )


# ---------------------------------------------------------------------
# مرحله ۲: خلاصه سفارش + ثبت
# ---------------------------------------------------------------------
@login_required_message
@require_http_methods(["GET", "POST"])
def checkout_review(request):
    cart, redirect_response = _require_cart_or_redirect(request)
    if redirect_response:
        return redirect_response

    address = _selected_address(request)
    if address is None:
        messages.warning(request, _("ابتدا آدرس تحویل را انتخاب کنید."))
        return redirect("orders:checkout")

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

    totals = cart_totals(cart, items=items)
    note_form = CheckoutNoteForm(request.POST if request.method == "POST" else None)

    if request.method == "POST":
        if not note_form.is_valid():
            messages.error(request, _("یادداشت واردشده معتبر نیست."))
        else:
            try:
                order = create_order(
                    customer=request.user,
                    cart=cart,
                    address=address,
                    customer_note=note_form.cleaned_data["note"],
                )
            except OrderError as exc:
                messages.error(request, exc.message)
                return redirect("orders:checkout_review")

            request.session.pop(ADDRESS_SESSION_KEY, None)
            return _start_payment(order, request)

    return render(
        request,
        "orders/review.html",
        {
            "items": items,
            "totals": totals,
            "address": address,
            "note_form": note_form,
            "shipping_options": get_shipping_options(),
            "step": 2,
        },
    )


def _start_payment(order: Order, request):
    """ایجاد درخواست پرداخت و هدایت کاربر به درگاه."""
    from payments.services import PaymentError, create_payment_for_order

    try:
        payment = create_payment_for_order(order, request)
    except PaymentError as exc:
        messages.error(request, exc.message)
        return redirect("orders:detail", order_number=order.order_number)

    request.session["payment_redirect_from"] = order.order_number
    return redirect("payments:redirect", pk=payment.pk)


@require_POST
@login_required_message
def order_pay(request, order_number: str):
    """پرداخت مجدد یک سفارش پرداخت‌نشده."""
    order = get_object_or_404(Order, order_number=order_number, customer=request.user)
    if not order.can_pay:
        messages.error(request, _("این سفارش در حال حاضر قابل پرداخت نیست."))
        return redirect("orders:detail", order_number=order.order_number)
    return _start_payment(order, request)


# ---------------------------------------------------------------------
# سفارش‌های من
# ---------------------------------------------------------------------
@login_required_message
def order_list(request):
    orders = (
        Order.objects.for_customer(request.user)
        .select_related("customer")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    paginator = Paginator(orders, 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "orders/history.html", {"page_obj": page_obj})


@login_required_message
def order_detail(request, order_number: str):
    """
    جزئیات سفارش.

    کاربر A هرگز نباید به سفارش کاربر B دسترسی داشته باشد.
    """
    order = (
        Order.objects.filter(order_number=order_number)
        .select_related("customer")
        .prefetch_related("items")
        .first()
    )
    if order is None:
        from django.http import Http404

        raise Http404(_("سفارش موردنظر یافت نشد."))
    if order.customer_id != request.user.id and not (
        request.user.is_staff and (request.user.has_perm("orders.view_order") or request.user.has_perm("orders.change_order"))
    ):
        raise PermissionDenied(_("شما دسترسی به این سفارش ندارید."))

    payments = list(order.payments.order_by("-created_at")[:5])
    return render(
        request,
        "orders/receipt.html",
        {"order": order, "payments": payments, "refund_pending": any(payment.needs_refund for payment in payments)},
    )
