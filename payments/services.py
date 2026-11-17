"""
سرویس پرداخت.

قوانین حیاتی:
  • Callback به‌تنهایی ملاک پرداخت موفق نیست؛ همیشه Verify انجام می‌شود.
  • پردازش Callback ایدمپوتنت است: ارسال دوباره، سفارش/پرداخت را دوباره
    پردازش نمی‌کند و موجودی دوبار کم نمی‌شود.
  • مبلغ تراکنش فقط از سفارش (Backend) خوانده می‌شود؛ هرگز از Client.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orders.choices import MANUAL_REVIEW_MESSAGE, OrderStatus, PaymentStatus
from orders.models import Order
from orders.services import OrderError, OrderInventoryUnavailable, OrderNotPayable, mark_order_paid

from .gateways import PaymentGatewayError, get_gateway
from .gateways.zibal import parse_callback
from .gateways.base import PaymentRequestRejected
from .models import Payment, PaymentCreationStatus

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """خطای قابل نمایش به کاربر."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


@dataclass(frozen=True)
class ReceiptDecision:
    action: str
    reason: str


def _sync_order_payment_review(order, *, resolved_receipt=False):
    """Synchronize under the caller's Order-first lock; never authorize payment.

    Resolving the last review as a refund must remove the review hold so normal
    expiration can release inventory. Acceptance uses mark_order_paid instead.
    """
    if order.is_paid:
        return
    has_review = order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).exists()
    status = order.payment_status
    if has_review and order.status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED):
        status = PaymentStatus.MANUAL_REVIEW
    elif resolved_receipt and not has_review and status == PaymentStatus.MANUAL_REVIEW:
        status = {
            OrderStatus.PENDING_PAYMENT: PaymentStatus.PENDING,
            OrderStatus.PAYMENT_FAILED: PaymentStatus.FAILED,
            OrderStatus.CANCELLED: PaymentStatus.CANCELLED,
        }.get(order.status, status)
    if status != order.payment_status:
        order.payment_status = status
        order.save(update_fields=["payment_status", "updated_at"])


def receipt_review_reasons(payment):
    """Keep earlier review evidence effective after a later response/decision."""
    metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
    decisions = [metadata.get("decision", {})]
    verifications = [metadata.get("verification", {})]
    for key, records in (("decision_history", decisions), ("verification_history", verifications)):
        history = metadata.get(key, [])
        if isinstance(history, list):
            records.extend(entry.get("data", {}) for entry in history if isinstance(entry, dict))
    decisions.extend(record.get("payment_time", {}) for record in verifications if isinstance(record, dict))
    return {record["reason"] for record in decisions if isinstance(record, dict) and isinstance(record.get("reason"), str)}


def _record_receipt_decision(payment, decision):
    """Append decisions and preserve legacy evidence before updating the latest."""
    history = list(payment.metadata.get("decision_history", []))
    previous = payment.metadata.get("decision")
    if isinstance(previous, dict) and previous and not any(
        isinstance(entry, dict) and entry.get("data") == previous for entry in history
    ):
        history.append({"source": "stored_decision", "data": previous})
    history.append({"data": decision})
    payment.metadata["decision_history"] = history
    payment.metadata["decision"] = decision


def classify_receipt(order, gateway_paid_at, timestamp_issue="", *, primary_exists=False,
                     other_manual_receipt=False) -> ReceiptDecision:
    """Shared preview/application rules; callback arrival never determines lateness."""
    if timestamp_issue or gateway_paid_at is None or timezone.is_naive(gateway_paid_at):
        return ReceiptDecision("manual_review", timestamp_issue or "timestamp_unavailable")
    if gateway_paid_at > timezone.now():
        return ReceiptDecision("manual_review", "future_timestamp")
    if order.status == OrderStatus.CANCELLED or order.inventory_released:
        return ReceiptDecision("refund", "order_cancelled_or_inventory_released")
    if order.is_paid:
        return ReceiptDecision("refund", "additional_successful_payment")
    if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED):
        return ReceiptDecision("manual_review", "inconsistent_order_state")
    if primary_exists:
        return ReceiptDecision("manual_review", "inconsistent_primary_ledger")
    if other_manual_receipt:
        return ReceiptDecision("manual_review", "another_receipt_requires_review")
    if order.expires_at and gateway_paid_at > order.expires_at:
        return ReceiptDecision("refund", "paid_after_deadline")
    return ReceiptDecision("accept", "paid_within_deadline")


def create_payment_for_order(order: Order, request=None) -> Payment:
    """Persist -> unlocked HTTP -> authoritative Order/Payment application.

    Call directly, without an outer atomic transaction. Zibal's documented
    request API has no idempotency key: an uncertain attempt is never resent.
    """
    try:
        gateway = get_gateway()
        snapshot, payment, created = _prepare_payment_request(order, gateway)
    except PaymentGatewayError as exc:
        raise PaymentError(_("تنظیمات درگاه پرداخت معتبر نیست؛ با پشتیبانی تماس بگیرید.")) from exc
    if not created:
        return payment

    response, failure = None, None
    try:
        response = gateway.create_payment(snapshot, request, request_id=payment.request_id)
    except PaymentGatewayError as exc:
        failure = exc
    payment, error = _finish_payment_request(snapshot, payment, response, failure)
    if error:
        # Raise after committing the durable attempt/evidence, never roll it back.
        raise PaymentError(error) from failure
    return payment


@transaction.atomic
def _prepare_payment_request(order, gateway):
    order = Order.objects.select_for_update().select_related("customer").get(pk=order.pk)

    if order.is_paid:
        raise PaymentError(_("این سفارش قبلاً پرداخت شده است."))
    if order.manual_review_pending:
        raise PaymentError(str(order.payment_review_message))
    if order.is_expired:
        raise PaymentError(_("مهلت پرداخت این سفارش به پایان رسیده است."))
    if not order.can_pay:
        raise PaymentError(_("این سفارش در حال حاضر قابل پرداخت نیست."))
    if not order.items.exists():
        raise PaymentError(_("اقلام این سفارش یافت نشد."))
    history = list(order.payments.order_by("pk"))
    if any(p.creation_status in (
        PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN, PaymentCreationStatus.REVIEW,
    ) or potentially_successful_receipt(p) for p in history):
        raise PaymentError(_("نتیجهٔ تلاش قبلی هنوز مشخص نیست؛ برای جلوگیری از پرداخت تکراری با پشتیبانی تماس بگیرید."))
    pending = [p for p in history if p.status in (PaymentStatus.PENDING, PaymentStatus.EXPIRED)
               and p.creation_status == PaymentCreationStatus.READY]
    if len(pending) > 1:
        raise PaymentError(_("چند تلاش پرداخت حل‌نشده وجود دارد؛ با پشتیبانی تماس بگیرید."))
    if pending:
        payment = Payment.objects.select_for_update().get(pk=pending[0].pk)
        if payment.gateway != gateway.name or payment.amount != order.total or not payment.authority:
            raise PaymentError(_("اطلاعات تلاش قبلی نیازمند بررسی پشتیبانی است."))
        gateway.get_payment_url(payment)  # Validate the existing ID, never issue another request.
        if payment.status == PaymentStatus.EXPIRED:
            payment.status = PaymentStatus.PENDING
            payment.error_message = ""
            payment.save(update_fields=["status", "error_message"])
        return order, payment, False
    request_id = uuid4()
    gateway.payment_request_payload(order, request_id=request_id)
    payment = Payment.objects.create(
        order=order, gateway=gateway.name, amount=order.total,
        request_id=request_id, creation_status=PaymentCreationStatus.REQUESTING,
        metadata={"creation": {
            "request_id": str(request_id), "order_number": order.order_number,
            "amount_toman": str(order.total), "requested_at": timezone.now().isoformat(),
        }},
    )
    return order, payment, True


@transaction.atomic
def _finish_payment_request(snapshot, attempt, response, failure):
    order = Order.objects.select_for_update().get(pk=attempt.order_id)
    payment = Payment.objects.select_for_update().get(pk=attempt.pk)
    payment.order = order
    creation = dict(payment.metadata.get("creation", {}))
    observed_at = timezone.now().isoformat()
    outcome = {"observed_at": observed_at}
    error = ""
    immutable_changed = (
        payment.order_id != snapshot.pk or payment.amount != snapshot.total
        or order.total != snapshot.total or order.order_number != snapshot.order_number
        or payment.gateway != attempt.gateway or payment.request_id != attempt.request_id
    )
    if response is not None:
        outcome.update(result=100, track_id=response.authority)
        if payment.authority and payment.authority != response.authority:
            error = "شناسهٔ پاسخ ایجاد با تراکنش ثبت‌شده مغایر است؛ نیازمند بررسی پشتیبانی."
        elif payment.authority is None:
            try:
                with transaction.atomic():
                    Payment.objects.filter(pk=payment.pk).update(authority=response.authority)
                payment.authority = response.authority
            except IntegrityError:
                error = "شناسهٔ درگاه قبلاً ثبت شده؛ شواهد حفظ شد و نیازمند بررسی پشتیبانی است."
        if not error:
            payment.creation_status = PaymentCreationStatus.READY
            payment.metadata["redirect_url"] = response.redirect_url
        else:
            payment.creation_status = PaymentCreationStatus.REVIEW
    else:
        rejected = isinstance(failure, PaymentRequestRejected)
        outcome["outcome"] = "rejected" if rejected else "uncertain"
        if rejected:
            outcome["result"] = failure.code
        # A callback may have authoritatively bound/finalized the attempt while
        # the creation HTTP response was missing. Never undo that evidence.
        if payment.authority is None and not potentially_successful_receipt(payment):
            payment.creation_status = PaymentCreationStatus.REJECTED if rejected else PaymentCreationStatus.UNCERTAIN
            if rejected:
                payment.status = PaymentStatus.FAILED
            error = (
                "درخواست ایجاد پرداخت رد شد. لطفاً بعداً تلاش کنید."
                if rejected else "نتیجهٔ ایجاد پرداخت مشخص نیست؛ پرداخت تازه انجام ندهید و با پشتیبانی تماس بگیرید."
            )
    if immutable_changed:
        payment.creation_status = PaymentCreationStatus.REVIEW
        error = "اطلاعات سفارش هنگام ایجاد پرداخت تغییر کرده است؛ نیازمند بررسی پشتیبانی."
    if not error and not potentially_successful_receipt(payment) and (
        not order.can_pay or not order.items.exists()
        or order.payments.exclude(pk=payment.pk).filter(status=PaymentStatus.MANUAL_REVIEW).exists()
    ):
        payment.creation_status = PaymentCreationStatus.REVIEW
        error = "سفارش هنگام ایجاد پرداخت دیگر قابل پرداخت نیست؛ شناسهٔ درگاه برای بررسی حفظ شد."
    if error:
        outcome["reason"] = "creation_requires_review" if response else outcome["outcome"]
        if not potentially_successful_receipt(payment):
            payment.error_message = error
    creation["responses"] = [*creation.get("responses", []), outcome]
    payment.metadata["creation"] = creation
    payment.save(update_fields=["authority", "creation_status", "metadata", "status", "error_message"])
    if (
        not error and payment.status == PaymentStatus.PENDING
        and not potentially_successful_receipt(payment) and order.status == OrderStatus.PAYMENT_FAILED
        and order.payment_status != PaymentStatus.MANUAL_REVIEW
    ):
        order.status, order.payment_status = OrderStatus.PENDING_PAYMENT, PaymentStatus.PENDING
        order.save(update_fields=["status", "payment_status", "updated_at"])
    logger.log(logging.ERROR if error and failure else logging.WARNING if error else logging.INFO,
               "Gateway creation attempt %s completed: %s", payment.request_id, payment.creation_status)
    return payment, error


def potentially_successful_receipt(payment):
    """A local status alone cannot rule out money already received by a gateway."""
    metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
    evidence = [metadata.get("verification")]
    history = metadata.get("verification_history", [])
    if isinstance(history, list):
        evidence.extend(entry.get("data") for entry in history if isinstance(entry, dict))
    return bool(
        payment.status in (PaymentStatus.SUCCESSFUL, PaymentStatus.MANUAL_REVIEW)
        or payment.paid_at or payment.gateway_paid_at
        or any(isinstance(record, dict) and (
            record.get("status") == 1 or record.get("successful") is True
        ) for record in evidence)
    )


def receipt_history_fingerprint(payments):
    fields = ("pk", "order_id", "gateway", "authority", "request_id", "creation_status", "amount", "status", "needs_refund",
              "transaction_id", "reference_id", "paid_at", "gateway_paid_at", "metadata")
    records = [{key: getattr(payment, key) for key in fields}
               for payment in sorted(payments, key=lambda item: item.pk)]
    return hashlib.sha256(json.dumps(records, default=str, sort_keys=True).encode()).hexdigest()


_RECOVERY_SEAL = object()


@dataclass(frozen=True)
class _RecoveryPrimaryAuthorization:
    order_id: int
    payment_id: int
    history_fingerprint: str
    plan: dict
    seal: object

    def matches(self, order, payment, history, audit, result):
        return (
            self.seal is _RECOVERY_SEAL and self.order_id == order.pk
            and self.payment_id == payment.pk
            and self.history_fingerprint == receipt_history_fingerprint(history)
            and audit and audit.get("recovery_plan") == self.plan
            and self.plan["receipts"].get(payment.authority) == recovery_receipt_snapshot(result)
        )


def recovery_receipt_snapshot(result):
    return {
        "transaction_id": result.transaction_id, "reference_id": result.reference_id,
        "gateway_paid_at": result.gateway_paid_at.isoformat() if result.gateway_paid_at else None,
        "amount": result.raw.get("amount"), "order_id": result.raw.get("orderId"),
    }


def _authorize_recovery_primary(*, order, payments, verified, selected_track_ids,
                                primary, backup, plan):
    """Internal permit issued only by reconcile_zibal_payments after full preflight.

    A boolean cannot authorize fulfillment. The sealed permit binds one primary
    to the complete inspected ledger, verified receipts, backup and recorded plan.
    Locked application rejects it if any payment history changes during HTTP.
    """
    selected = set(selected_track_ids)
    potential = {p.authority for p in payments if potentially_successful_receipt(p)} | set(verified)
    if (
        not primary or primary.pk not in {p.pk for p in payments}
        or any(p.creation_status in (PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN,
                                     PaymentCreationStatus.REVIEW) or (
            not p.authority and p.creation_status != PaymentCreationStatus.REJECTED
        ) for p in payments)
        or primary.order_id != order.pk or primary.authority not in selected or potential - selected
        or selected - set(verified) or potential - set(verified)
        or any(result.timestamp_issue or result.gateway_paid_at is None
               or timezone.is_naive(result.gateway_paid_at) or result.gateway_paid_at > timezone.now()
               for result in verified.values())
        or any(p.gateway != "zibal" for p in payments if p.authority in potential)
        or not backup or backup.get("sha256_sidecar_check") != "passed"
        or not isinstance(backup.get("sha256"), str) or len(backup["sha256"]) != 64
        or plan.get("history_fingerprint") != receipt_history_fingerprint(payments)
        or plan.get("primary") != primary.authority or plan.get("backup_sha256") != backup["sha256"]
        or plan.get("selected_track_ids") != list(selected_track_ids)
        or plan.get("inspected_payment_ids") != sorted(p.pk for p in payments)
        or plan.get("receipts") != {key: recovery_receipt_snapshot(result) for key, result in verified.items()}
        or any(not result.successful or result.transaction_id != p.authority
               or p.amount != order.total or result.raw.get("amount") != int(p.amount) * 10
               or result.raw.get("orderId") != order.order_number
               or (p.transaction_id and p.transaction_id != result.transaction_id)
               or (p.reference_id and (p.reference_id != result.reference_id
                                       or p.transaction_id != result.transaction_id))
               for p in payments if (result := verified.get(p.authority)) is not None)
        or classify_receipt(order, verified[primary.authority].gateway_paid_at).action != "accept"
    ):
        raise PaymentError("Recovery primary authorization requires a complete validated plan.")
    candidates = [p for p in payments if p.authority in selected and classify_receipt(
        order, verified[p.authority].gateway_paid_at,
    ).action == "accept"]
    earliest = min(verified[p.authority].gateway_paid_at for p in candidates)
    earliest_receipts = [p.pk for p in candidates if verified[p.authority].gateway_paid_at == earliest]
    if earliest_receipts != [primary.pk]:
        raise PaymentError("Recovery primary authorization requires a unique earliest eligible receipt.")
    return _RecoveryPrimaryAuthorization(
        order.pk, primary.pk, plan["history_fingerprint"], json.loads(json.dumps(plan)), _RECOVERY_SEAL,
    )


def _validate_callback(payment, callback_data):
    if payment.gateway == "zibal":
        try:
            callback = parse_callback(callback_data or {})
            supplied_request = (callback_data or {}).get("request_id")
            if supplied_request is not None and (
                not payment.request_id or UUID(str(supplied_request)) != payment.request_id
            ):
                raise PaymentGatewayError("Callback request does not match.")
            if (
                (payment.authority is not None and callback["trackId"] != payment.authority)
                or (payment.authority is None and supplied_request is None)
                or callback["orderId"] != payment.order.order_number
            ):
                raise PaymentGatewayError("Callback identifiers do not match.")
        except (PaymentGatewayError, ValueError, TypeError) as exc:
            raise PaymentError(_("اطلاعات بازگشت از درگاه نامعتبر است.")) from exc


def _callback_finalized(payment, callback_data, recovery_audit):
    return payment.status == PaymentStatus.SUCCESSFUL or (
        payment.status == PaymentStatus.CANCELLED and callback_data
        and callback_data.get("success") == "0" and recovery_audit is None
    )


def _verification_identity(payment):
    return (payment.order_id, payment.gateway, payment.authority, payment.amount,
            payment.order.order_number, payment.order.total)


def process_callback(payment: Payment, callback_data: dict | None = None, *,
                     gateway_timezone=None, allow_fulfillment=True, recovery_audit=None,
                     recovery_primary_authorized=False) -> Payment:
    """Verify without row locks, then apply to fresh locked state atomically.

    Callers must not wrap gateway HTTP in an outer database transaction. The
    production view and reconciliation command call this service directly.
    """
    if recovery_primary_authorized and not (
        isinstance(recovery_primary_authorized, _RecoveryPrimaryAuthorization)
        and recovery_primary_authorized.seal is _RECOVERY_SEAL
        and recovery_primary_authorized.payment_id == payment.pk and recovery_audit
    ):
        raise PaymentError("Invalid internal recovery primary authorization.")
    snapshot = Payment.objects.select_related("order").only(
        "id", "order_id", "gateway", "authority", "request_id", "amount", "status",
        "order__id", "order__order_number", "order__total",
    ).get(pk=payment.pk)
    _validate_callback(snapshot, callback_data)
    expected_identity = _verification_identity(snapshot)
    if snapshot.authority is None:
        # Only an in-memory candidate until Verify/Inquiry validates the order,
        # amount and track ID. An untrusted callback must not bind a DB record.
        snapshot.authority = parse_callback(callback_data)["trackId"]
    result = None
    if not _callback_finalized(snapshot, callback_data, recovery_audit):
        try:
            gateway = get_gateway(snapshot.gateway)
            if gateway_timezone is not None:
                gateway.paid_at_timezone = gateway_timezone
            result = gateway.verify_payment(snapshot, callback_data)
        except PaymentGatewayError as exc:
            logger.error("Gateway verification failed for transaction %s", snapshot.authority)
            raise PaymentError(_("تأیید پرداخت ممکن نشد. لطفاً با پشتیبانی تماس بگیرید.")) from exc
    return _apply_verified_callback(
        snapshot, callback_data, result, allow_fulfillment=allow_fulfillment,
        recovery_audit=recovery_audit, recovery_primary_authorized=recovery_primary_authorized,
        expected_identity=expected_identity,
    )


@transaction.atomic
def _apply_verified_callback(snapshot, callback_data, result, *, allow_fulfillment,
                             recovery_audit, recovery_primary_authorized, expected_identity=None):
    # Short transaction: Order -> Payment -> ProductVariant (ascending PK).
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=snapshot.pk)
    order = Order.objects.select_for_update().get(pk=order_id)
    payment = Payment.objects.select_for_update().get(pk=snapshot.pk)
    payment.order = order
    _validate_callback(payment, callback_data)
    payment.callback_count += 1
    payment.save(update_fields=["callback_count"])
    if _callback_finalized(payment, callback_data, recovery_audit):
        return payment  # Another request may have committed while HTTP was waiting.
    if result is None:
        raise PaymentError("Payment state changed; retry verification.")
    identity_changed = _verification_identity(payment) != (expected_identity or _verification_identity(snapshot))
    # Binding by another verified callback during HTTP is compatible only with
    # the same immutable identity and track ID.
    if expected_identity and expected_identity[2] is None and payment.authority == snapshot.authority:
        identity_changed = _verification_identity(payment) != (
            *expected_identity[:2], snapshot.authority, *expected_identity[3:],
        )
    if not identity_changed and payment.authority is None:
        try:
            with transaction.atomic():
                Payment.objects.filter(pk=payment.pk).update(authority=snapshot.authority)
            payment.authority = snapshot.authority
        except IntegrityError:
            identity_changed = True
    if not identity_changed:
        payment.creation_status = PaymentCreationStatus.READY
    elif payment.request_id:
        payment.creation_status = PaymentCreationStatus.REVIEW
    ledger = list(order.payments.order_by("pk"))
    other_receipts = [p for p in ledger if p.pk != payment.pk]
    other_manual_receipt = any(
        p.status == PaymentStatus.MANUAL_REVIEW
        or (p.status != PaymentStatus.SUCCESSFUL and potentially_successful_receipt(p))
        for p in other_receipts
    )
    authorization_matches = bool(recovery_primary_authorized and recovery_primary_authorized.matches(
        order, payment, ledger, recovery_audit, result,
    ))

    observed_at = timezone.now()
    previously_successful = potentially_successful_receipt(payment)
    previous_review_reasons = receipt_review_reasons(payment)
    history = list(payment.metadata.get("verification_history", []))
    previous_verification = payment.metadata.get("verification")
    if isinstance(previous_verification, dict) and previous_verification and not any(
        isinstance(entry, dict) and entry.get("data") == previous_verification for entry in history
    ):
        history.append({"source": "stored_verification", "data": previous_verification})
    if result.raw != payment.metadata.get("verification"):
        history.append({"observed_at": observed_at.isoformat(), "data": result.raw})
    payment.metadata = {
        **payment.metadata, "verification": result.raw, "verification_history": history,
    }
    if recovery_audit:
        payment.metadata["recovery_history"] = [
            *payment.metadata.get("recovery_history", []),
            {**recovery_audit, "observed_at": observed_at.isoformat()},
        ]
    if result.status != PaymentStatus.PENDING:
        payment.verified_at = observed_at

    if result.successful:
        decision = classify_receipt(
            order, result.gateway_paid_at, result.timestamp_issue,
            primary_exists=any(p.status == PaymentStatus.SUCCESSFUL and not p.needs_refund for p in other_receipts),
            other_manual_receipt=other_manual_receipt and not authorization_matches,
        )
        if identity_changed:
            decision = ReceiptDecision("manual_review", "payment_identity_changed_during_verification")
        if recovery_primary_authorized and not authorization_matches and decision.action == "accept":
            decision = ReceiptDecision("manual_review", "recovery_plan_changed")
        if (payment.transaction_id and payment.transaction_id != result.transaction_id) or (
            payment.reference_id and (payment.reference_id != result.reference_id
                                     or payment.transaction_id != result.transaction_id)
        ):
            decision = ReceiptDecision("manual_review", "receipt_identifiers_changed")
        else:
            payment.transaction_id = result.transaction_id
            payment.reference_id = result.reference_id
        # A previously impossible timestamp needs a human audit even if time has
        # since caught up. A confirmed timezone can resolve timezone_unconfirmed.
        if previous_review_reasons & {"future_timestamp", "previous_future_timestamp"}:
            decision = ReceiptDecision("manual_review", "previous_future_timestamp")
        elif previous_review_reasons & {
            "gateway_receipt_conflict", "receipt_identifiers_changed", "payment_identity_changed_during_verification",
            "previous_receipt_conflict",
        }:
            decision = ReceiptDecision("manual_review", "previous_receipt_conflict")
        if decision.action == "accept" and not allow_fulfillment:
            decision = ReceiptDecision("manual_review", "recovery_primary_not_selected")
        payment.gateway_paid_at = result.gateway_paid_at
        payment.paid_at = payment.paid_at or observed_at
        deadline = order.expires_at
        if decision.action == "accept":
            try:
                mark_order_paid(order, paid_at=observed_at, gateway_paid_at=result.gateway_paid_at)
            except OrderNotPayable:
                decision = ReceiptDecision("refund", "order_no_longer_payable")
            except OrderInventoryUnavailable:
                decision = ReceiptDecision("manual_review", "inventory_reservation_unavailable")
            except OrderError as exc:  # pragma: no cover - unexpected service failure
                raise PaymentError(str(exc)) from exc
        payment.needs_refund = decision.action == "refund"
        payment.status = (
            PaymentStatus.MANUAL_REVIEW if decision.action == "manual_review" else PaymentStatus.SUCCESSFUL
        )
        payment.error_message = {
            "accept": "",
            "refund": "وجه دریافت شده اما سفارش قابل پرداخت نیست یا قبلاً پرداخت شده؛ نیازمند بازگشت وجه دستی.",
            "manual_review": str(MANUAL_REVIEW_MESSAGE),
        }[decision.action]
        _record_receipt_decision(payment, {
            "action": decision.action, "reason": decision.reason,
            "order_expires_at": deadline.isoformat() if deadline else None,
            "inventory_released": order.inventory_released,
            "observed_at": observed_at.isoformat(),
        })
        payment.save()
        _sync_order_payment_review(order, resolved_receipt=decision.action == "refund")
        logger.log(
            logging.INFO if decision.action == "accept" else logging.WARNING,
            "Verified receipt %s: %s (%s)", payment.authority, decision.action, decision.reason,
        )
        return payment

    if previously_successful:
        # A later conflicting gateway response cannot erase a previously verified
        # receipt awaiting review. Both sanitized responses remain in the history.
        payment.status = PaymentStatus.MANUAL_REVIEW
        payment.needs_refund = False
        payment.error_message = "گزارش تازهٔ درگاه با دریافت وجه قبلی مغایر است؛ نیازمند بررسی دستی."
        _record_receipt_decision(payment, {
            "action": "manual_review", "reason": "gateway_receipt_conflict", "observed_at": observed_at.isoformat(),
        })
        payment.save()
        _sync_order_payment_review(order)
        return payment

    payment.status = result.status or PaymentStatus.FAILED
    payment.error_message = (result.message or _("پرداخت ناموفق بود."))[:255]
    payment.save()

    if payment.status == PaymentStatus.PENDING:
        logger.info("تراکنش %s همچنان در انتظار پرداخت است", payment.authority)
        return payment

    order = payment.order
    _sync_order_payment_review(order)
    if order.status == OrderStatus.PENDING_PAYMENT and order.payment_status != PaymentStatus.MANUAL_REVIEW and not order.payments.exclude(
        pk=payment.pk,
    ).filter(status=PaymentStatus.MANUAL_REVIEW).exists():
        # A failed legacy attempt must not present an order holding an
        # unresolved verified receipt as an ordinary failed payment.
        if payment.status == PaymentStatus.FAILED:
            order.status = OrderStatus.PAYMENT_FAILED
            order.payment_status = PaymentStatus.FAILED
            order.save(update_fields=["status", "payment_status", "updated_at"])
        elif payment.status == PaymentStatus.CANCELLED:
            order.payment_status = PaymentStatus.CANCELLED
            order.save(update_fields=["payment_status", "updated_at"])

    logger.info(
        "پرداخت سفارش %s ناموفق/لغو شد: %s",
        order.order_number,
        payment.error_message,
    )
    return payment


def expire_stale_payments(older_than_minutes: int = 60) -> int:
    """تبدیل تراکنش‌های معلق قدیمی به «منقضی» (برای گزارش‌گیری)."""
    threshold = timezone.now() - timedelta(minutes=older_than_minutes)
    return Payment.objects.filter(
        status=PaymentStatus.PENDING, created_at__lt=threshold
    ).update(status=PaymentStatus.EXPIRED, error_message="منقضی شده")
