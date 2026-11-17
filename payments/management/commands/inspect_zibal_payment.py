"""Sanitized stored receipt evidence only: no gateway calls and no DB writes."""

import json
import re

from django.core.management.base import BaseCommand, CommandError

from orders.choices import OrderStatus, PaymentStatus
from payments.gateways.base import PaymentGatewayError
from payments.gateways.zibal import _reference_id, _track_id
from payments.gateways.zibal_time import _ISO_DATE, confirmed_zone
from payments.models import Payment, PaymentCreationStatus
from payments.services import receipt_review_reasons


_TIME_REASONS = {
    "", "invalid_timestamp", "unknown_offset", "timezone_unconfirmed", "invalid_timezone_configuration",
    "ambiguous_local_time", "nonexistent_local_time", "future_timestamp", "timestamp_unavailable",
}
_DECISION_REASONS = _TIME_REASONS | {
    "order_cancelled_or_inventory_released", "additional_successful_payment", "inconsistent_order_state",
    "inconsistent_primary_ledger", "another_receipt_requires_review", "paid_after_deadline", "paid_within_deadline",
    "payment_identity_changed_during_verification", "recovery_plan_changed", "receipt_identifiers_changed",
    "previous_future_timestamp", "previous_receipt_conflict", "recovery_primary_not_selected",
    "order_no_longer_payable", "inventory_reservation_unavailable", "gateway_receipt_conflict",
}
_REDACTED = "مقدار نامعتبر یا ناشناخته؛ نیازمند بررسی خصوصی"


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else _REDACTED


def _timestamp(value):
    if value is None:
        return None
    return value if isinstance(value, str) and _ISO_DATE.fullmatch(value) else _REDACTED


def _timezone_name(value):
    if value == "":
        return ""
    if isinstance(value, str) and re.fullmatch(r"UTC(?:[+-][0-9]{2}:[0-9]{2})?", value):
        return value
    try:
        confirmed_zone(value)
        return value
    except ValueError:
        return _REDACTED


def _track_comparison(value, authority):
    try:
        return "matched" if _track_id(value) == _track_id(authority) else "mismatch"
    except PaymentGatewayError:
        return "invalid"


def _code(value):
    return value if type(value) is int and -2 <= value <= 999 else None


class Command(BaseCommand):
    help = "خواندن شواهد پالایش‌شدهٔ یک پرداخت زیبال؛ بدون تماس با درگاه یا تغییر دیتابیس."

    def add_arguments(self, parser):
        parser.add_argument("--payment-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            payment = Payment.objects.select_related("order").get(pk=options["payment_id"], gateway="zibal")
        except Payment.DoesNotExist as exc:
            raise CommandError("پرداخت زیبال با این شناسه یافت نشد؛ هیچ تغییری انجام نشد.") from exc
        order = payment.order
        metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
        verification = metadata.get("verification", {})
        verification = verification if isinstance(verification, dict) else {}
        payment_time = verification.get("payment_time", metadata.get("payment_time", {}))
        payment_time = payment_time if isinstance(payment_time, dict) else {}
        decision = metadata.get("decision", {})
        decision = decision if isinstance(decision, dict) else {}
        has_track_id = "trackId" in verification
        reference_comparison = "not_recorded"
        if payment.reference_id:
            try:
                reference_comparison = "matched" if _reference_id(verification.get("refNumber")) == payment.reference_id else "mismatch"
            except PaymentGatewayError:
                reference_comparison = "invalid"
        report = {
            "payment_id": payment.pk, "order_id": order.pk,
            "payment": {
                "status": _choice(payment.status, PaymentStatus.values),
                "creation_status": _choice(payment.creation_status, PaymentCreationStatus.values),
                "authority_present": bool(payment.authority), "request_id_present": bool(payment.request_id),
                "amount_toman": str(payment.amount), "amount_matches_order": payment.amount == order.total,
                "needs_refund": payment.needs_refund, "paid_at": payment.paid_at,
                "verified_at": payment.verified_at, "gateway_paid_at": payment.gateway_paid_at,
            },
            "order": {
                "status": _choice(order.status, OrderStatus.values),
                "payment_status": _choice(order.payment_status, PaymentStatus.values),
                "is_paid": order.is_paid, "inventory_released": order.inventory_released,
                "expires_at": order.expires_at,
            },
            "verification": {
                "result": _code(verification.get("result")), "verify_result": _code(verification.get("verify_result")),
                "status": _code(verification.get("status")), "paidAt": _timestamp(verification.get("paidAt")),
                "amount_matches": type(verification.get("amount")) is int and verification["amount"] == int(payment.amount) * 10,
                "order_id_matches": verification.get("orderId") == order.order_number,
                "track_id_present": has_track_id,
                "track_id_type": type(verification["trackId"]).__name__ if has_track_id else None,
                "authority_type": type(payment.authority).__name__,
                "track_id_comparison": _track_comparison(verification["trackId"], payment.authority) if has_track_id else "not_returned",
                "transaction_id_comparison": _track_comparison(payment.transaction_id, payment.authority) if payment.transaction_id else "not_recorded",
                "reference_comparison": reference_comparison,
                "payment_time": {
                    "state": _choice(payment_time.get("state"), {"valid", "manual_review"}),
                    "reason": _choice(payment_time.get("reason"), _TIME_REASONS),
                    "source": _choice(payment_time.get("source"), {"", "offset", "configuration"}),
                    "timezone": _timezone_name(payment_time.get("timezone")),
                    "normalized_paid_at": _timestamp(payment_time.get("normalized_paid_at")),
                },
            },
            "decision": {
                "action": _choice(decision.get("action"), {"accept", "manual_review", "refund"}),
                "reason": _choice(decision.get("reason"), _DECISION_REASONS),
            },
            "historical_review_reasons": sorted(receipt_review_reasons(payment) & (_DECISION_REASONS - {""})) if isinstance(payment.metadata, dict) else [],
            "history_counts": {key: len(metadata[key]) if isinstance(metadata.get(key), list) else 0 for key in (
                "verification_history", "decision_history", "recovery_history",
            )},
            "notes": [
                "این خروجی فقط شواهد ذخیره‌شده را می‌خواند؛ استعلام تازه، تأیید تازه یا تغییر مالی انجام نمی‌دهد.",
                "نبود trackId در پاسخ Verify/Inquiry اختلاف شناسه نیست؛ در صورت وجود، عدد معتبر با authority مقایسه می‌شود.",
                "شناسهٔ پذیرنده، اطلاعات کارت و مشتری، شناسه‌های تراکنش و payload کامل در این گزارش چاپ نمی‌شوند.",
            ],
        }
        self.stdout.write(json.dumps(report, default=str, ensure_ascii=False, indent=2))
