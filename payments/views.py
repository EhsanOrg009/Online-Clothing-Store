"""View های پرداخت: انتقال به زیبال، Callback و نتایج."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponseBadRequest, HttpResponseNotFound, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from urllib.parse import parse_qsl

from accounts.decorators import login_required_message
from orders.choices import PaymentStatus
from orders.models import Order

from .gateways import PaymentGatewayError, get_gateway
from .gateways.zibal import parse_callback
from .models import Payment, PaymentCreationStatus
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

    if (
        not payment.order.can_pay or payment.creation_status != PaymentCreationStatus.READY
        or payment.order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).exists()
        or payment.order.payments.exclude(pk=payment.pk).filter(creation_status__in=[
            PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN, PaymentCreationStatus.REVIEW,
        ]).exists()
    ):
        messages.warning(request, _("پرداخت این سفارش اکنون مجاز نیست؛ در صورت ابهام با پشتیبانی تماس بگیرید."))
        return redirect("orders:detail", order_number=payment.order.order_number)

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
        {"payment": payment, "redirect_url": redirect_url, "gateway_referrer_policy": gateway.redirect_referrer_policy},
    )
    # Start زیبال Referer می‌خواهد؛ فقط origin فروشگاه ارسال می‌شود.
    response.headers["Referrer-Policy"] = gateway.redirect_referrer_policy
    response.headers["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@require_POST
def torobpay_callback(request, request_id):
    """External form POST only; no session writes on a cross-site return.

SameSite=Lax can omit the existing login cookie on POST. Creating a message
session here could replace it. Customer-safe outcomes are persisted by the
service and displayed on the following GET instead.
    """
    from .gateways.torobpay_data import CALLBACK_MAX_BYTES
    from .torobpay_services import handle_callback

    try:
        length = request.META.get("CONTENT_LENGTH", "")
        if (request.content_type != "application/x-www-form-urlencoded" or request.GET
                or (length and (not length.isascii() or not length.isdecimal() or int(length) > CALLBACK_MAX_BYTES))):
            raise ValueError
        body = request.body
        if len(body) > CALLBACK_MAX_BYTES or request.encoding not in (None, "utf-8", "UTF-8"):
            raise ValueError
        pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=3)
        callback = QueryDict("", mutable=True)
        for key, value in pairs:
            callback.appendlist(key, value)
    except (ValueError, UnicodeError):
        return HttpResponseBadRequest("درخواست بازگشت پرداخت معتبر نیست.")
    try:
        payment = handle_callback(request_id, callback)
    except PaymentError:
        return HttpResponseBadRequest("اطلاعات بازگشت پرداخت معتبر یا قابل تطبیق نیست.")
    if payment is None:
        return HttpResponseNotFound("تلاش پرداخت یافت نشد.")
    response = _result_redirect(payment)
    response.status_code = 303
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------
# Callback درگاه (بدون نیاز به Login)
# ---------------------------------------------------------------------
@require_GET
def payment_callback(request, request_id=None):
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

    if request_id is not None:
        callback_data["request_id"] = str(request_id)

    payment = (
        Payment.objects.select_related("order").filter(
            gateway="zibal", authority=callback_data["trackId"]
        ).first()
    )
    if payment is None and request_id is not None:
        payment = Payment.objects.select_related("order").filter(
            gateway="zibal", request_id=request_id, authority__isnull=True,
        ).first()
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

    if payment.needs_refund or payment.requires_manual_review:
        messages.warning(request, payment.error_message)
    return _result_redirect(payment)


def _result_redirect(payment: Payment):
    order_number = payment.order.order_number
    if payment.needs_refund or payment.is_pending or payment.requires_manual_review or (
        not payment.order.is_paid and payment.order.manual_review_pending
    ):
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
        Order.objects.with_payment_review().select_related("customer"), order_number=order_number
    )
    if order.customer_id != request.user.id and not (
        request.user.is_staff and (request.user.has_perm("orders.view_order") or request.user.has_perm("orders.change_order"))
    ):
        raise Http404

    payments = list(order.payments.order_by("-created_at")[:5])
    context = {"order": order, "payments": payments, "successful_payment": order.payments.filter(status=PaymentStatus.SUCCESSFUL, needs_refund=False).first()}

    # اگر وضعیت واقعی سفارش با صفحه درخواستی نمی‌خواند، به صفحه درست هدایت شود
    if not order.is_paid and order.manual_review_pending:
        return redirect("orders:detail", order_number=order.order_number)
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
