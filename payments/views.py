"""View های پرداخت: انتقال به زیبال، Callback و نتایج."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET

from accounts.decorators import login_required_message
from orders.choices import PaymentStatus
from orders.models import Order

from .gateways import PaymentGatewayError, get_gateway
from .gateways.zibal import parse_callback
from .models import Payment
from .services import PaymentError, process_callback


def _user_can_see(user, payment: Payment) -> bool:
    return payment.order.customer_id == user.id or (
        user.is_staff and (user.has_perm("payments.view_payment") or user.has_perm("payments.change_payment"))
    )


# ---------------------------------------------------------------------
# انتقال به درگاه پرداخت
# ---------------------------------------------------------------------
@login_required_message
@require_GET
def payment_redirect(request, pk: int):
    """صفحه میانی: کاربر را به صفحه رسمی درگاه هدایت می‌کند."""
    payment = get_object_or_404(
        Payment.objects.select_related("order"), pk=pk
    )
    if not _user_can_see(request.user, payment):
        raise Http404(_("تراکنش موردنظر یافت نشد."))

    if not payment.is_pending:
        return _result_redirect(payment)

    try:
        gateway = get_gateway(payment.gateway)
        redirect_url = gateway.get_payment_url(payment)
    except PaymentGatewayError:
        redirect_url = ""

    if not redirect_url:
        messages.error(request, _("آدرس درگاه پرداخت در دسترس نیست."))
        return redirect("orders:detail", order_number=payment.order.order_number)

    response = render(
        request,
        "payments/redirect.html",
        {"payment": payment, "redirect_url": redirect_url},
    )
    # Start زیبال Referer می‌خواهد؛ فقط origin فروشگاه ارسال می‌شود.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---------------------------------------------------------------------
# Callback درگاه (بدون نیاز به Login)
# ---------------------------------------------------------------------
@require_GET
def payment_callback(request):
    """
    دریافت بازگشت کاربر از درگاه.

    نکته امنیتی: داده‌های Query به‌تنهایی پذیرفته نمی‌شوند؛
    نتیجه با Verify و تطبیق اطلاعات نزد زیبال تعیین می‌شود.
    """
    try:
        callback_data = parse_callback(request.GET)
    except PaymentGatewayError:
        messages.error(request, _("درخواست پرداخت نامعتبر است."))
        return redirect("store:home")

    payment = (
        Payment.objects.select_related("order").filter(
            gateway="zibal", authority=callback_data["trackId"]
        ).first()
    )
    if payment is None:
        messages.error(request, _("تراکنش موردنظر یافت نشد."))
        return redirect("store:home")
    if callback_data["orderId"] != payment.order.order_number:
        messages.error(request, _("اطلاعات بازگشت با سفارش مطابقت ندارد."))
        return redirect("store:home")

    try:
        payment = process_callback(
            payment,
            callback_data=callback_data,
        )
    except PaymentError as exc:
        messages.error(request, exc.message)
        return redirect("orders:detail", order_number=payment.order.order_number)

    if payment.needs_refund:
        messages.warning(request, payment.error_message)
    return _result_redirect(payment)


def _result_redirect(payment: Payment):
    order_number = payment.order.order_number
    if payment.needs_refund or payment.is_pending:
        return redirect("orders:detail", order_number=order_number)
    if payment.status == PaymentStatus.SUCCESSFUL:
        return redirect("payments:result_success", order_number=order_number)
    if payment.status == PaymentStatus.CANCELLED:
        return redirect("payments:result_cancelled", order_number=order_number)
    return redirect("payments:result_failed", order_number=order_number)


# ---------------------------------------------------------------------
# صفحات نتیجه پرداخت
# ---------------------------------------------------------------------
def _result_page(request, order_number: str, expected_status: str, template: str):
    order = get_object_or_404(
        Order.objects.select_related("customer"), order_number=order_number
    )
    if order.customer_id != request.user.id and not (
        request.user.is_staff and (request.user.has_perm("orders.view_order") or request.user.has_perm("orders.change_order"))
    ):
        raise Http404

    payments = list(order.payments.order_by("-created_at")[:5])
    context = {"order": order, "payments": payments, "successful_payment": order.payments.filter(status=PaymentStatus.SUCCESSFUL, needs_refund=False).first()}

    # اگر وضعیت واقعی سفارش با صفحه درخواستی نمی‌خواند، به صفحه درست هدایت شود
    actual = order.payment_status
    if expected_status == "success" and actual != PaymentStatus.SUCCESSFUL:
        return redirect("orders:detail", order_number=order.order_number)
    if expected_status in ("failed", "cancelled") and actual == PaymentStatus.SUCCESSFUL:
        return redirect("payments:result_success", order_number=order.order_number)
    if expected_status == "failed" and actual not in (PaymentStatus.FAILED, PaymentStatus.EXPIRED):
        return redirect("orders:detail", order_number=order.order_number)
    if expected_status == "cancelled" and actual != PaymentStatus.CANCELLED:
        return redirect("orders:detail", order_number=order.order_number)

    return render(request, template, context)


@login_required_message
@require_GET
def result_success(request, order_number: str):
    return _result_page(
        request, order_number, "success", "payments/result_success.html"
    )


@login_required_message
@require_GET
def result_failed(request, order_number: str):
    return _result_page(
        request, order_number, "failed", "payments/result_failed.html"
    )


@login_required_message
@require_GET
def result_cancelled(request, order_number: str):
    return _result_page(
        request, order_number, "cancelled", "payments/result_cancelled.html"
    )
