"""View های پرداخت: انتقال به درگاه، Callback، صفحه Mock و نتایج."""

from __future__ import annotations

import hmac

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET

from accounts.decorators import login_required_message
from orders.choices import PaymentStatus
from orders.models import Order

from .gateways import PaymentGatewayError, get_gateway
from .gateways.mock import MockGateway
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

    return render(
        request,
        "payments/redirect.html",
        {"payment": payment, "redirect_url": redirect_url},
    )


# ---------------------------------------------------------------------
# Callback درگاه (بدون نیاز به Login)
# ---------------------------------------------------------------------
@require_GET
def payment_callback(request):
    """
    دریافت بازگشت کاربر از درگاه.

    نکته امنیتی: داده‌های Query به‌تنهایی پذیرفته نمی‌شوند؛
    نتیجه با Verify (بررسی امضا/وضعیت نزد درگاه) تعیین می‌شود.
    """
    authority = (request.GET.get("authority") or "").strip()
    if not authority:
        messages.error(request, _("درخواست پرداخت نامعتبر است."))
        return redirect("store:home")

    payment = (
        Payment.objects.select_related("order").filter(authority=authority).first()
    )
    if payment is None:
        messages.error(request, _("تراکنش موردنظر یافت نشد."))
        return redirect("store:home")

    try:
        payment = process_callback(
            payment,
            callback_data={
                "status": request.GET.get("status", ""),
                "sig": request.GET.get("sig", ""),
                "ref_id": request.GET.get("ref_id", ""),
            },
        )
    except PaymentError as exc:
        messages.error(request, exc.message)
        return redirect("orders:detail", order_number=payment.order.order_number)

    return _result_redirect(payment)


def _result_redirect(payment: Payment):
    order_number = payment.order.order_number
    if payment.status == PaymentStatus.SUCCESSFUL:
        return redirect("payments:result_success", order_number=order_number)
    if payment.status == PaymentStatus.CANCELLED:
        return redirect("payments:result_cancelled", order_number=order_number)
    return redirect("payments:result_failed", order_number=order_number)


# ---------------------------------------------------------------------
# صفحه Mock درگاه (فقط Development)
# ---------------------------------------------------------------------
@require_GET
def mock_gateway(request, authority: str):
    """صفحه جعلی «درگاه بانکی» برای توسعه؛ در Production غیرفعال است."""
    if not settings.DEBUG:
        raise Http404

    payment = get_object_or_404(
        Payment.objects.select_related("order"), authority=authority
    )
    if not payment.is_pending:
        return _result_redirect(payment)

    try:
        gateway = get_gateway()
    except PaymentGatewayError:
        raise Http404

    if not isinstance(gateway, MockGateway):  # pragma: no cover - ایمنی
        raise Http404

    start_payload = f"start|{payment.authority}|{request.GET.get('amount', '')}"
    if not hmac.compare_digest(gateway.sign(start_payload), request.GET.get("sig", "")):
        messages.error(request, _("لینک پرداخت نامعتبر است."))
        return redirect("orders:detail", order_number=payment.order.order_number)

    callback_urls = {
        status: reverse("payments:callback") + "?" + gateway.build_callback_query(payment, status)
        for status in ("successful", "failed", "cancelled")
    }
    return render(
        request,
        "payments/mock_gateway.html",
        {"payment": payment, "callback_urls": callback_urls},
    )


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
