"""Offline Zibal responses for the guarded browser QA database, never application data."""

import secrets
from unittest.mock import patch
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from payments.models import Payment


def require_isolated_database():
    if not settings.DEBUG or str(settings.DATABASES["default"]["NAME"]) != "/tmp/shop-browser.sqlite3":
        raise RuntimeError("Zibal browser fixtures require the isolated /tmp QA database.")


def callback_data(payment, outcome="successful"):
    success, status = {
        "successful": ("1", "2"), "failed": ("0", "5"), "cancelled": ("0", "3"),
    }[outcome]
    return {
        "trackId": payment.authority, "orderId": payment.order.order_number,
        "success": success, "status": status,
    }


def record_result(payment, outcome):
    require_isolated_database()
    payment.metadata["qa_zibal_status"] = {"successful": 2, "failed": 5, "cancelled": 3}[outcome]
    payment.save(update_fields=["metadata"])


def api_response(gateway, path, payload):
    require_isolated_database()
    if path == "/v1/request":
        return {"result": 100, "trackId": secrets.randbelow(10**13) + 10**13}
    payment = Payment.objects.select_related("order").get(gateway="zibal", authority=str(payload["trackId"]))
    status = payment.metadata.get("qa_zibal_status", -1)
    if path == "/v1/verify":
        if status == 2:
            status = 1
            payment.metadata["qa_zibal_status"] = 1
            payment.save(update_fields=["metadata"])
        else:
            return {"result": 201 if status == 1 else 202}
    elif path != "/v1/inquiry":
        raise AssertionError(f"Unexpected Zibal operation: {path}")
    return {
        "result": 100, "status": status, "amount": int(payment.amount) * 10,
        "orderId": payment.order.order_number, "refNumber": int(payment.authority) + 1,
        "paidAt": timezone.now().isoformat(),
    }


def mocked_api():
    return patch("payments.gateways.zibal.ZibalGateway._post", new=api_response)


@require_GET
def gateway_return(request, track_id, outcome):
    require_isolated_database()
    if outcome not in ("successful", "failed", "cancelled"):
        from django.http import Http404
        raise Http404
    payment = get_object_or_404(Payment.objects.select_related("order"), gateway="zibal", authority=track_id)
    record_result(payment, outcome)
    return redirect(reverse("payments:callback") + "?" + urlencode(callback_data(payment, outcome)))
