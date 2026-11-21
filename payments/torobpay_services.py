"""Durable CPG orchestration: Order -> Payment -> variants, unlocked HTTP.

The four status families do not identify every exact remote order state. In
particular PENDING is not evidence of W_FOR_VERIFY, and SETTLE alone does not
prove ONGOING for refunds/updates. Exact state/time attestations are staff-only.
No browser assertion authorizes fulfillment or a refund.
"""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
import hashlib
import json
import logging
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from orders.choices import ALLOWED_ORDER_TRANSITIONS, OrderStatus, PaymentStatus
from orders.models import Order, OrderItem
from orders.services import OrderError, cancel_order, mark_order_paid, transition_status
from products.models import ProductVariant

from .gateways.base import PaymentGatewayError
from .gateways.torobpay import TorobPayGateway
from .gateways.torobpay_client import TorobPayError
from .gateways.torobpay_data import RemoteStatus, parse_callback, token_fingerprint, toman_to_rial
from .models import Payment, PaymentCreationStatus
from .services import PaymentError, _record_receipt_decision, _sync_order_payment_review, potentially_successful_receipt


logger = logging.getLogger(__name__)
REVIEW_MESSAGE = (
    "نتیجهٔ پرداخت ترب‌پی نیازمند بررسی پشتیبانی است. لطفاً پرداخت تازه انجام ندهید. "
    "آماده‌سازی سفارش پس از پذیرش نهایی پرداخت انجام می‌شود."
)
_ITEM_FIELDS = ("pk", "product_id", "variant_id", "product_name", "category_name", "color_name", "size_name",
                "sku", "product_image", "original_unit_price", "unit_price", "discount_amount", "quantity", "line_total")
_ORDER_FIELDS = ("pk", "customer_id", "order_number", "subtotal", "discount_total", "shipping_fee", "total",
                 "receiver_name", "receiver_phone", "province", "city", "address_line", "postal_code", "expires_at",
                 "status", "payment_status", "inventory_released")


class ReconciliationConflict(PaymentError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(REVIEW_MESSAGE)


@dataclass
class OperationLease:
    payment: Payment = field(repr=False)
    operation_id: str
    identity: str
    previous_hold: bool = False
    previous_status: str = PaymentStatus.PENDING
    receipt_identity: tuple = ()
    heartbeat: Callable[[], None] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RemoteEvidence:
    """Independent merchant-support evidence, never accepted from storefront."""
    state: str
    support_reference: str
    paid_at: datetime | None = None


def _unlocked():
    # TestCase wraps mocked tests in a synthetic transaction. Real application
    # transactions, including admin's change form, may never contain CPG HTTP.
    if connection.in_atomic_block and any(not getattr(block, "_from_testcase", False) for block in connection.atomic_blocks):
        raise PaymentError("عملیات درگاه باید بیرون تراکنش پایگاه داده اجرا شود.")


def _data(payment):
    if not isinstance(payment.metadata, dict) or not isinstance(payment.metadata.get("torobpay", {}), dict):
        raise ReconciliationConflict("invalid_local_metadata")
    return payment.metadata.setdefault("torobpay", {})


def _load(payment):
    pk = payment.pk if isinstance(payment, Payment) else payment
    try:
        return Payment.objects.select_related("order__customer").get(pk=pk, gateway="torobpay")
    except Payment.DoesNotExist as exc:
        raise PaymentError("پرداخت ترب‌پی یافت نشد.") from exc


def _locked(pk):
    order_id = Payment.objects.values_list("order_id", flat=True).get(pk=pk)
    order = Order.objects.select_for_update().select_related("customer").get(pk=order_id)
    payment = Payment.objects.select_for_update().get(pk=pk)
    if payment.order_id != order_id or payment.gateway != "torobpay":
        raise ReconciliationConflict("payment_identity_changed")
    payment.order = order
    return payment


def _identity(payment):
    data = _data(payment)
    record = {
        "payment": [payment.pk, payment.order_id, payment.gateway, str(payment.request_id), payment.authority,
                    token_fingerprint(payment.provider_token) if payment.provider_token else None, str(payment.amount),
                    data.get("amount_toman"), data.get("transaction_id")],
        "order": {key: getattr(payment.order, key) for key in _ORDER_FIELDS},
        "items": [{key: getattr(item, key) for key in _ITEM_FIELDS} for item in payment.order.items.order_by("pk")],
    }
    return hashlib.sha256(json.dumps(record, default=str, sort_keys=True).encode()).hexdigest()


def _receipt_identity(payment):
    """Provider association, independent of mutable fulfillment state."""
    data = _data(payment)
    return (payment.pk, payment.order_id, payment.gateway, payment.request_id,
            payment.authority, token_fingerprint(payment.provider_token), payment.amount,
            data.get("amount_toman"), data.get("transaction_id"))


def reconciliation_lease_duration():
    # Covers one bounded authenticated HTTP step plus DB application margin.
    # Renew before/after each step; a crashed worker cannot keep ownership.
    return timedelta(seconds=12 * settings.TOROBPAY_TIMEOUT_SECONDS + 60)


def confirmation_before_reservation_deadline(order, confirmed_at):
    """CPG policy only. Zibal's independently documented boundary is unchanged."""
    deadline = order.expires_at
    return bool(isinstance(confirmed_at, datetime) and timezone.is_aware(confirmed_at)
                and isinstance(deadline, datetime) and timezone.is_aware(deadline)
                and confirmed_at < deadline)


def _amount(payment):
    data = _data(payment)
    amount = toman_to_rial(data.get("amount_toman", str(payment.amount)))
    if type(data.get("amount_rial", amount)) is not int or data.get("amount_rial", amount) != amount:
        raise ReconciliationConflict("local_amount_units_conflict")
    return amount


def _transaction_id(payment):
    return _data(payment).get("transaction_id") or str(payment.request_id)


def _validate_status(payment, status, *, update=False):
    expected_amount, expected_id = _amount(payment), _transaction_id(payment)
    proposal = _data(payment).get("pending_update", {}) if update else {}
    if proposal and status.amount_rial == proposal.get("amount_rial"):
        expected_amount = proposal["amount_rial"]
        expected_id = proposal.get("response_transaction_id", expected_id)
    if status.amount_rial != expected_amount:
        raise ReconciliationConflict("remote_amount_mismatch")
    if status.transaction_id != expected_id:
        raise ReconciliationConflict("remote_transaction_mismatch")
    # The original receipt ID remains immutable. Updated-copy IDs are stored
    # separately in the provider audit and validated against that update intent.
    if payment.transaction_id and payment.transaction_id != str(payment.request_id):
        raise ReconciliationConflict("stored_transaction_mismatch")
    return status


@transaction.atomic
def _claim(payment, kind, callback=None, *, heartbeat=None):
    payment = _locked(payment.pk)
    previous_hold, previous_status = payment.reservation_hold, payment.status
    data = _data(payment)
    if callback is not None:
        if (not payment.request_id or callback["transactionId"] != str(payment.request_id)
                or int(callback["amount"]) != toman_to_rial(payment.amount)):
            raise PaymentError("شناسه یا مبلغ بازگشت با تلاش پرداخت مطابقت ندارد.")
        payment.callback_count += 1
        data["last_callback"] = {"state": callback["state"], "amount_rial": int(callback["amount"]),
                                 "received_at": timezone.now().isoformat()}
    if callback is not None and payment.status in (PaymentStatus.SUCCESSFUL, PaymentStatus.CANCELLED, PaymentStatus.FAILED):
        payment.save(update_fields=["callback_count", "metadata"])
        return payment, None
    active = data.get("active_operation", {})
    now = timezone.now()
    payment.reconciliation_attempted_at = now
    if active.get("status") == "requesting":
        expires = payment.reconciliation_lease_expires_at
        if expires is None:
            # Upgrade only a bounded, valid legacy lease. A malformed timestamp
            # used to remain busy forever; reclaim it with status, never retry.
            try:
                started = datetime.fromisoformat(active["started_at"])
                expires = started + reconciliation_lease_duration() if timezone.is_aware(started) and started <= now else None
            except (ValueError, KeyError, TypeError):
                expires = None
        busy = bool(expires and timezone.is_aware(expires) and now < expires)
        if busy:
            payment.reservation_hold = True
            payment.reconciliation_lease_expires_at = expires
            payment.save(update_fields=["callback_count", "metadata", "reservation_hold", "reconciliation_lease_expires_at"])
            return payment, None
        mutation = active.get("last_step", {})
        if mutation.get("operation") not in (None, "status") and mutation.get("outcome") == "requesting":
            data["uncertain_mutation"] = {"operation": mutation["operation"], "operation_id": active.get("id")}
    if not payment.provider_token:
        payment.error_message = "شناسهٔ پرداخت ترب‌پی هنوز ثبت نشده است؛ نتیجه باید توسط پشتیبانی پیگیری شود."
        payment.reservation_hold = payment.creation_status != PaymentCreationStatus.REJECTED
        payment.reconciliation_lease_expires_at = None
        payment.save(update_fields=["callback_count", "metadata", "error_message", "reservation_hold",
                                   "reconciliation_lease_expires_at", "reconciliation_attempted_at"])
        return payment, None
    if token_fingerprint(payment.provider_token) != payment.authority:
        data["manual_block"] = "stored_token_mismatch"
        payment.status = PaymentStatus.MANUAL_REVIEW
        payment.error_message = REVIEW_MESSAGE
        payment.reservation_hold = True
        payment.reconciliation_lease_expires_at = None
        payment.save(update_fields=["callback_count", "metadata", "status", "error_message", "reservation_hold",
                                   "reconciliation_lease_expires_at", "reconciliation_attempted_at"])
        _sync_order_payment_review(payment.order)
        return payment, None
    if active:
        data.setdefault("operation_history", []).append(deepcopy(active))
    operation_id = str(uuid4())
    payment.reconciliation_lease_expires_at = now + reconciliation_lease_duration()
    data["active_operation"] = {"id": operation_id, "kind": kind, "status": "requesting",
                                 "started_at": now.isoformat(), "steps": [],
                                 "lease_expires_at": payment.reconciliation_lease_expires_at.isoformat()}
    # Persist protection under Order -> Payment locks BEFORE any remote HTTP.
    # Financial uncertainty can outlive this bounded processing lease; only a
    # matching final status or authorized review releases that separate hold.
    payment.reservation_hold = True
    payment.save(update_fields=["callback_count", "metadata", "reservation_hold",
                               "reconciliation_lease_expires_at", "reconciliation_attempted_at"])
    return payment, OperationLease(payment, operation_id, _identity(payment), previous_hold, previous_status,
                                   _receipt_identity(payment), heartbeat)


def _check_lease(payment, lease):
    if _data(payment).get("active_operation", {}).get("id") != lease.operation_id:
        raise ReconciliationConflict("operation_lease_changed")
    if _identity(payment) != lease.identity:
        raise ReconciliationConflict("identity_changed_during_http")
    if not payment.reconciliation_lease_expires_at or timezone.now() >= payment.reconciliation_lease_expires_at:
        raise ReconciliationConflict("operation_lease_expired")


@transaction.atomic
def _begin_step(lease, operation):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    now = timezone.now()
    payment.reconciliation_lease_expires_at = now + reconciliation_lease_duration()
    step = {"operation": operation, "started_at": now.isoformat(), "outcome": "requesting"}
    _data(payment)["active_operation"]["last_step"] = step
    _data(payment)["active_operation"]["lease_expires_at"] = payment.reconciliation_lease_expires_at.isoformat()
    payment.save(update_fields=["metadata", "reconciliation_lease_expires_at"])
    lease.payment = payment


@transaction.atomic
def _end_step(lease, operation, *, observed_at, outcome="confirmed", status=None, transaction_id=None, error=None):
    payment = _locked(lease.payment.pk)
    data = _data(payment)
    if data.get("active_operation", {}).get("id") != lease.operation_id:
        # A stale HTTP response may arrive after another worker reconciled the
        # expired lease. Preserve evidence without changing its newer decision.
        audit = {"operation_id": lease.operation_id, "operation": operation, "outcome": outcome,
                 "observed_at": observed_at.isoformat(), "recorded_at": timezone.now().isoformat(),
                 "code": getattr(error, "code", None)}
        if status:
            audit.update(api_status=status.status, amount_rial=status.amount_rial)
            if outcome == "confirmed":
                audit["transaction_id"] = status.transaction_id
            else:
                audit["transaction_id_fingerprint"] = hashlib.sha256(status.transaction_id.encode()).hexdigest()
        data.setdefault("stale_responses", []).append(audit)
        payment.save(update_fields=["metadata"])
        lease.payment = payment
        return False
    recorded_at = timezone.now()
    try:
        receipt_matches = _receipt_identity(payment) == lease.receipt_identity
        identity_matches = _identity(payment) == lease.identity
    except (PaymentGatewayError, ReconciliationConflict):
        receipt_matches = identity_matches = False
    step = {**data["active_operation"]["last_step"], "outcome": outcome,
            "observed_at": observed_at.isoformat(), "recorded_at": recorded_at.isoformat(),
            "receipt_identity_matches": receipt_matches}
    if lease.receipt_identity:
        step.update(expected_payment_id=lease.receipt_identity[0], expected_order_id=lease.receipt_identity[1])
    payment.reconciliation_lease_expires_at = recorded_at + reconciliation_lease_duration()
    data["active_operation"]["lease_expires_at"] = payment.reconciliation_lease_expires_at.isoformat()
    if error is not None:
        step.update(code=getattr(error, "code", None), http_status=getattr(error, "http_status", None))
    if outcome == "unknown" and operation != "status":
        data["uncertain_mutation"] = {"operation": operation, "operation_id": lease.operation_id}
    if status:
        status_matches = outcome == "confirmed" and receipt_matches
        if status_matches:
            try:
                _validate_status(payment, status, update=bool(data.get("pending_update")))
            except ReconciliationConflict:
                status_matches = False
        step.update(api_status=status.status, amount_rial=status.amount_rial, identity_validated=status_matches)
        if status_matches:
            step["transaction_id"] = status.transaction_id
        if not status_matches:
            # An untrusted identifier can echo a credential. Preserve only its
            # digest; validated merchant transaction IDs remain in real evidence.
            fingerprint = hashlib.sha256(status.transaction_id.encode()).hexdigest()
            step["transaction_id_fingerprint"] = fingerprint
            data["last_unvalidated_response"] = {"api_status": status.status, "amount_rial": status.amount_rial,
                "transaction_id_fingerprint": fingerprint, "observed_at": observed_at.isoformat()}
    if status and status_matches:
        data["api_status"] = status.status
        if status.status == "VERIFY":
            data.update(last_known_order_state="W_FOR_SETTLE", receipt_seen=True)
            payment.verified_at = payment.verified_at or timezone.now()
        elif status.status == "SETTLE":
            data["receipt_seen"] = True
            # Observation was captured after response validation, before waiting
            # for DB locks. Revalidate association under the lock and write once.
            # Legacy JSON records only DB write time; do not invent a historical
            # observation or silently promote an incomplete legacy receipt.
            if (not data.get("pending_update") and payment.settle_confirmed_at is None
                    and not data.get("settlement_evidence") and receipt_matches):
                payment.settle_confirmed_at = observed_at
                data["settlement_evidence"] = {"source": "server_api_response", "observed_at": observed_at.isoformat(),
                    "recorded_at": recorded_at.isoformat(), "amount_rial": status.amount_rial,
                    "transaction_id": status.transaction_id, "order_id": payment.order_id, "payment_id": payment.pk,
                    "request_id": str(payment.request_id), "token_fingerprint": payment.authority,
                    "reservation_expires_at": payment.order.expires_at.isoformat() if payment.order.expires_at else None}
            if not data.get("pending_update"):
                payment.paid_at = payment.paid_at or timezone.now()
        uncertain = data.get("uncertain_mutation", {}).get("operation")
        if (uncertain == "verify" and status.status in ("VERIFY", "SETTLE", "REVERT")) or (
            uncertain == "settle" and status.status == "SETTLE"
        ):
            data.pop("uncertain_mutation", None)
    if operation == "verify" and outcome == "confirmed":
        data.update(last_known_order_state="W_FOR_SETTLE", receipt_seen=True)
        payment.verified_at = payment.verified_at or timezone.now()
        payment.metadata["verification"] = {"api_status": "VERIFY", "receipt_seen": True}
    if operation == "settle" and outcome == "confirmed":
        data.update(last_known_order_state="ONGOING", receipt_seen=True, explicit_settle=True)
    if transaction_id is not None:
        step["transaction_id"] = transaction_id
        if operation == "update":
            data["pending_update"]["response_transaction_id"] = transaction_id
    data["active_operation"]["last_step"] = step
    data["active_operation"]["steps"].append(step)
    payment.save(update_fields=["metadata", "verified_at", "paid_at", "settle_confirmed_at", "reconciliation_lease_expires_at"])
    lease.payment = payment
    # Return the conflict after committing the audit, never raise inside this
    # transaction and accidentally roll back a remotely successful operation.
    return identity_matches and (status is None or status_matches)


def _perform(lease, operation, action, *, update=False):
    if lease.heartbeat:
        lease.heartbeat()
    _begin_step(lease, operation)
    try:
        result = action(lease.payment)
        if isinstance(result, RemoteStatus):
            try:
                _validate_status(lease.payment, result, update=update)
            except ReconciliationConflict:
                # Preserve sanitized conflicting evidence, without creating an
                # authoritative confirmation timestamp or advancing a receipt.
                _end_step(lease, operation, outcome="mismatch", status=result, observed_at=timezone.now())
                raise
            observed_at = timezone.now()
            identity_matches = _end_step(lease, operation, status=result, observed_at=observed_at)
        else:
            transaction_id = result.transaction_id if hasattr(result, "transaction_id") else result
            if operation != "update" and transaction_id != _transaction_id(lease.payment):
                raise ReconciliationConflict("remote_transaction_mismatch")
            identity_matches = _end_step(lease, operation, transaction_id=transaction_id, observed_at=timezone.now())
        if not identity_matches:
            raise ReconciliationConflict("identity_changed_during_http")
        return result
    except TorobPayError as error:
        if not _end_step(lease, operation, outcome="rejected" if error.outcome == "rejected" else "unknown",
                         error=error, observed_at=timezone.now()):
            raise ReconciliationConflict("identity_changed_during_http") from None
        raise


def _status(lease, gateway, *, update=False):
    return _perform(lease, "status", gateway.status, update=update)


def _other_blockers(payment):
    return any(
        other.status == PaymentStatus.MANUAL_REVIEW or potentially_successful_receipt(other)
        or other.reservation_hold or other.creation_status in (PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN, PaymentCreationStatus.REVIEW)
        for other in payment.order.payments.exclude(pk=payment.pk).order_by("pk")
    )


def _fulfillment_checks(payment, *, confirmation_at=None):
    order = payment.order
    if _data(payment).get("manual_block"):
        raise ReconciliationConflict("historical_receipt_conflict")
    if order.status == OrderStatus.CANCELLED or order.inventory_released:
        raise ReconciliationConflict("order_cancelled_or_inventory_released")
    if order.is_paid or order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED):
        raise ReconciliationConflict("order_not_payable")
    if toman_to_rial(order.total) != _amount(payment):
        raise ReconciliationConflict("local_order_amount_mismatch")
    if _other_blockers(payment):
        raise ReconciliationConflict("another_receipt_requires_review")
    effective = confirmation_at or timezone.now()
    if (timezone.is_naive(effective) or effective > timezone.now() or effective < payment.created_at
            or not confirmation_before_reservation_deadline(order, effective)):
        raise ReconciliationConflict("confirmation_after_deadline")
    items = list(order.items.order_by("variant_id", "pk"))
    variants = list(ProductVariant.objects.select_for_update().filter(
        pk__in={item.variant_id for item in items if item.variant_id}).order_by("pk"))
    if (not items or any(not item.variant_id or item.quantity <= 0 for item in items)
            or len(variants) != len({item.variant_id for item in items})):
        raise ReconciliationConflict("inventory_reservation_unavailable")


@transaction.atomic
def _check_before_settle(lease):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    _fulfillment_checks(payment)


@transaction.atomic
def _finish(lease, *, remote, pending=False):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    data = _data(payment)
    if remote.status == "SETTLE":
        if payment.status != PaymentStatus.SUCCESSFUL:
            evidence = data.get("settlement_evidence", {})
            try:
                confirmed_at = payment.settle_confirmed_at
                if (confirmed_at is None or evidence.get("source") != "server_api_response"
                        or datetime.fromisoformat(evidence["observed_at"]) != confirmed_at
                        or evidence.get("amount_rial") != _amount(payment)
                        or evidence.get("transaction_id") != _transaction_id(payment)
                        or evidence.get("order_id") != payment.order_id or evidence.get("payment_id") != payment.pk
                        or evidence.get("request_id") != str(payment.request_id)
                        or evidence.get("token_fingerprint") != payment.authority):
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ReconciliationConflict("settlement_evidence_unavailable") from exc
            _fulfillment_checks(payment, confirmation_at=confirmed_at)
            observed = timezone.now()
            mark_order_paid(payment.order, paid_at=payment.paid_at or observed, confirmation_at=confirmed_at)
            payment.status = PaymentStatus.SUCCESSFUL
            payment.transaction_id = payment.transaction_id or remote.transaction_id
            payment.paid_at = payment.paid_at or observed
            payment.verified_at = payment.verified_at or observed
            payment.needs_refund = False
            payment.reservation_hold = False
            payment.error_message = ""
            payment.metadata["verification"] = {"api_status": "SETTLE", "receipt_seen": True,
                "amount_rial": remote.amount_rial, "transaction_id": remote.transaction_id,
                "payment_time": {"source": "server_confirmation_upper_bound", "upper_bound": confirmed_at.isoformat(),
                                 "gateway_paid_at": None}}
            _record_receipt_decision(payment, {"action": "accept", "reason": "server_confirmed_within_reservation",
                                             "observed_at": observed.isoformat(),
                                             "settle_confirmed_at": confirmed_at.isoformat(),
                                             "order_expires_at": evidence.get("reservation_expires_at")})
        # Matching repeated SETTLE is read-only fulfillment-wise, including
        # accepted legacy records whose precise first observation is unknown.
        payment.reservation_hold = False
        payment.error_message = ""
    elif remote.status == "REVERT":
        if data.get("receipt_seen") or payment.settle_confirmed_at or payment.paid_at or payment.gateway_paid_at or data.get("manual_block") not in (
            None, "confirmation_after_deadline", "order_cancelled_or_inventory_released", "inventory_reservation_unavailable",
        ):
            raise ReconciliationConflict("gateway_receipt_conflict")
        payment.status = PaymentStatus.FAILED
        payment.reservation_hold = False
        payment.error_message = "پرداخت در ترب‌پی انجام نشد؛ می‌توانید روش دیگری انتخاب کنید."
        data.pop("manual_block", None)
        _sync_order_payment_review(payment.order, resolved_receipt=True)
    else:
        payment.reservation_hold = True
        if not data.get("receipt_seen") and not data.get("manual_block"):
            payment.status = PaymentStatus.PENDING
        payment.error_message = "پرداخت هنوز در انتظار تعیین وضعیت ترب‌پی است."
    data["active_operation"].update(status="confirmed", finished_at=timezone.now().isoformat())
    payment.reconciliation_lease_expires_at = None
    if remote.status in ("SETTLE", "REVERT"):
        payment.creation_status = PaymentCreationStatus.READY
    payment.save()
    if remote.status == "REVERT":
        _sync_order_payment_review(payment.order, resolved_receipt=True)
        if payment.order.status == OrderStatus.PENDING_PAYMENT and not payment.order.manual_review_pending:
            transition_status(payment.order, OrderStatus.PAYMENT_FAILED)
            payment.order.payment_status = PaymentStatus.FAILED
            payment.order.save(update_fields=["payment_status", "updated_at"])
    return payment


@transaction.atomic
def _review(lease, reason, *, critical=True):
    payment = _locked(lease.payment.pk)
    data = _data(payment)
    if data.get("active_operation", {}).get("id") != lease.operation_id:
        return payment
    payment.reservation_hold = True
    payment.error_message = REVIEW_MESSAGE
    payment.status = PaymentStatus.MANUAL_REVIEW
    payment.reconciliation_lease_expires_at = None
    if critical:
        data["manual_block"] = reason
    active = data.get("active_operation", {})
    if active.get("id") == lease.operation_id:
        active.update(status="review", reason=reason, finished_at=timezone.now().isoformat())
    _record_receipt_decision(payment, {"action": "manual_review", "reason": reason, "observed_at": timezone.now().isoformat()})
    payment.save()
    _sync_order_payment_review(payment.order)
    logger.warning("TorobPay review order_id=%s payment_id=%s correlation_id=%s reason=%s",
                   payment.order_id, payment.pk, payment.request_id, reason)
    return payment


@transaction.atomic
def _unknown(lease):
    payment = _locked(lease.payment.pk)
    data = _data(payment)
    if data.get("active_operation", {}).get("id") != lease.operation_id:
        return payment
    payment.reservation_hold = True
    payment.error_message = "نتیجهٔ عملیات ترب‌پی روشن نیست؛ پرداخت یا بازپرداخت تازه انجام ندهید و با پشتیبانی تماس بگیرید."
    read_only_known_receipt = (lease.previous_status == PaymentStatus.SUCCESSFUL
        and not data.get("cancellation_intent") and not data.get("pending_update") and not data.get("uncertain_mutation")
        and data.get("active_operation", {}).get("last_step", {}).get("operation") == "status")
    if data.get("receipt_seen") and not read_only_known_receipt:
        payment.status = PaymentStatus.MANUAL_REVIEW
    payment.reconciliation_lease_expires_at = None
    active = data.get("active_operation", {})
    if active.get("id") == lease.operation_id:
        active.update(status="uncertain", finished_at=timezone.now().isoformat())
    payment.save()
    _sync_order_payment_review(payment.order)
    return payment


@transaction.atomic
def _rejected_operation(lease, error):
    """Definitive rejection preserves the receipt; no refund/stock effects."""
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    data = _data(payment)
    intent = data.pop("cancellation_intent", None) or data.pop("pending_update", None)
    if intent:
        data.setdefault("rejected_intents", []).append({**intent, "code": getattr(error, "code", None),
                                                        "rejected_at": timezone.now().isoformat()})
    if getattr(error, "code", None) == 1016:
        data["provider_discounted"] = True
    data["active_operation"].update(status="rejected", code=getattr(error, "code", None), finished_at=timezone.now().isoformat())
    payment.status, payment.reservation_hold = lease.previous_status, lease.previous_hold
    payment.reconciliation_lease_expires_at = None
    payment.error_message = str(error)[:255]
    payment.save()
    return payment


def _advance(lease, gateway, remote):
    data = _data(lease.payment)
    if data.get("cancellation_intent"):
        if remote.status == "REVERT":
            return _finish_cancellation(lease, remote)
        return _unknown(lease)
    if data.get("pending_update"):
        if remote.status == "SETTLE" and remote.amount_rial == data["pending_update"]["amount_rial"]:
            return _finish_update(lease, remote)
        return _unknown(lease)
    if remote.status in ("SETTLE", "REVERT"):
        return _finish(lease, remote=remote)
    if remote.status == "PENDING":
        if data.get("uncertain_mutation"):
            return _unknown(lease)
        # Never promote an already unfulfillable NEW/IPG/W_FOR_VERIFY order
        # into the automatic-settlement path. The provider may expire/refund it;
        # retain inventory until a matching terminal status is confirmed.
        _check_before_settle(lease)
        try:
            _perform(lease, "verify", gateway.verify_payment)
        except TorobPayError as error:
            if error.code in (1005, 1048, 1007):
                raise ReconciliationConflict("provider_token_mismatch") from None
            remote = _status(lease, gateway)
            if remote.status == "PENDING":
                return _finish(lease, remote=remote) if error.code == 1053 else _unknown(lease)
        else:
            remote = _status(lease, gateway)
        if remote.status in ("SETTLE", "REVERT"):
            return _finish(lease, remote=remote)
        if remote.status != "VERIFY":
            raise ReconciliationConflict("verification_state_conflict")
    if _data(lease.payment).get("uncertain_mutation", {}).get("operation") == "settle":
        return _unknown(lease)
    _check_before_settle(lease)
    try:
        _perform(lease, "settle", gateway.settle)
    except TorobPayError as error:
        if error.code in (1005, 1048, 1007):
            raise ReconciliationConflict("provider_token_mismatch") from None
        remote = _status(lease, gateway)
        if remote.status != "SETTLE":
            return _unknown(lease)
    else:
        remote = _status(lease, gateway)
        if remote.status != "SETTLE":
            raise ReconciliationConflict("settlement_state_conflict")
    return _finish(lease, remote=remote)


def _run(lease):
    try:
        gateway = TorobPayGateway()
        remote = _status(lease, gateway, update=bool(_data(lease.payment).get("pending_update")))
        return _advance(lease, gateway, remote)
    except ReconciliationConflict as error:
        return _review(lease, error.reason, critical=error.reason != "operation_lease_expired")
    except (TorobPayError, PaymentGatewayError):
        return _unknown(lease)
    except OrderError:
        return _review(lease, "local_fulfillment_rejected")
    except PaymentError:
        _unknown(lease)
        raise


def process_torobpay_callback(payment, callback_data):
    _unlocked()
    try:
        callback = parse_callback(callback_data or {})
    except PaymentGatewayError as error:
        raise PaymentError("اطلاعات بازگشت از ترب‌پی معتبر نیست.") from error
    current, lease = _claim(_load(payment), "callback", callback)
    return _run(lease) if lease else current


def handle_callback(request_id, callback_data):
    payment = Payment.objects.filter(gateway="torobpay", request_id=request_id).first()
    return process_torobpay_callback(payment, callback_data) if payment else None


def reconcile_payment(payment, *, heartbeat=None):
    _unlocked()
    current, lease = _claim(_load(payment), "reconcile", heartbeat=heartbeat)
    return _run(lease) if lease else current


def _authorize(payment, actor, *, staff_only=False):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PaymentError("دسترسی به عملیات پرداخت مجاز نیست.")
    staff = actor.is_staff and actor.has_perm("orders.change_order") and actor.has_perm("payments.change_payment")
    if not staff and (staff_only or actor.pk != payment.order.customer_id):
        raise PaymentError("دسترسی به عملیات این سفارش مجاز نیست.")
    return staff


def refund_route(paid_at, *, now=None):
    """Expected route only. CPG chooses/executes SEP/PAYA, not this store."""
    now = now or timezone.now()
    if paid_at is None:
        return "provider_determined"
    if timezone.is_naive(paid_at) or paid_at > now:
        raise PaymentError("زمان قطعی پرداخت معتبر نیست.")
    return "sep" if now - paid_at <= timedelta(minutes=30) else "paya"


def _cancel_state(payment, remote, evidence, *, staff):
    if evidence:
        from .gateways.torobpay_data import identifier
        if not staff or not isinstance(evidence, RemoteEvidence):
            raise PaymentError("شواهد مستقل وضعیت فقط توسط پشتیبانی مجاز ثبت می‌شوند.")
        identifier(evidence.support_reference)
        family = {"W_FOR_VERIFY": "PENDING", "W_FOR_SETTLE": "VERIFY", "ONGOING": "SETTLE"}
        if family.get(evidence.state) != remote.status:
            raise ReconciliationConflict("attested_state_conflicts_with_status")
        if evidence.paid_at is not None and (
            timezone.is_naive(evidence.paid_at) or evidence.paid_at < payment.created_at or evidence.paid_at > timezone.now()
        ):
            raise PaymentError("زمان تأییدشدهٔ پرداخت معتبر نیست.")
        return evidence.state
    if remote.status == "VERIFY":
        return "W_FOR_SETTLE"
    if remote.status == "SETTLE" and _data(payment).get("explicit_settle") and _data(payment).get("last_known_order_state") == "ONGOING":
        # The final mutation endpoint still enforces current exact state. A
        # later PAID/UPDATED state is rejected; status SETTLE is not relabelled.
        return "ONGOING"
    raise PaymentError("وضعیت دقیق ترب‌پی برای لغو معلوم نیست؛ تأیید پشتیبانی لازم است.")


@transaction.atomic
def _cancel_intent(lease, actor, operation, state, evidence):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    _authorize(payment, actor)
    if payment.order.status != OrderStatus.CANCELLED and OrderStatus.CANCELLED not in ALLOWED_ORDER_TRANSITIONS.get(payment.order.status, set()):
        raise PaymentError("سفارش در این مرحله قابل لغو نیست.")
    block = _data(payment).get("manual_block")
    supported_review = bool(evidence and actor.is_staff and actor.has_perm("orders.change_order")
                            and actor.has_perm("payments.change_payment") and block in (
                                "confirmation_after_deadline", "order_cancelled_or_inventory_released", "inventory_reservation_unavailable",
                            ))
    if (block and not supported_review) or _other_blockers(payment):
        raise PaymentError("شواهد پرداخت‌های سفارش باید پیش از بازپرداخت بررسی شوند.")
    _data(payment)["cancellation_intent"] = {
        "actor_id": actor.pk, "operation": operation, "attested_state": state,
        "support_reference": evidence.support_reference if evidence else "",
        "gateway_paid_at": evidence.paid_at.isoformat() if evidence and evidence.paid_at else None,
        "expected_refund_route": refund_route(evidence.paid_at if evidence else None),
        "created_at": timezone.now().isoformat(),
    }
    payment.save(update_fields=["metadata"])
    lease.payment = payment


def cancel_payment(payment, *, actor, evidence=None):
    """Explicit merchant/customer service action; never used by expiration."""
    _unlocked()
    current = _load(payment)
    staff = _authorize(current, actor)
    if evidence is not None and (not staff or not isinstance(evidence, RemoteEvidence)):
        raise PaymentError("شاهد مستقل وضعیت فقط توسط پشتیبانی مجاز پذیرفته می‌شود.")
    if _data(current).get("pending_update") or _data(current).get("uncertain_mutation", {}).get("operation") in ("verify", "settle", "update"):
        raise PaymentError("پیش از لغو، نتیجهٔ عملیات قبلی ترب‌پی باید با استعلام تعیین تکلیف شود.")
    if current.status == PaymentStatus.CANCELLED and _data(current).get("cancellation_confirmed"):
        return current
    if current.order.status != OrderStatus.CANCELLED and OrderStatus.CANCELLED not in ALLOWED_ORDER_TRANSITIONS.get(current.order.status, set()):
        raise PaymentError("سفارش ارسال‌شده یا آمادهٔ ارسال قابل لغو نیست.")
    current, lease = _claim(current, "cancel")
    if not lease:
        raise PaymentError("عملیات قبلی پرداخت هنوز تعیین تکلیف نشده است.")
    try:
        gateway = TorobPayGateway()
        remote = _status(lease, gateway)
        if _data(lease.payment).get("cancellation_intent"):
            return _finish_cancellation(lease, remote) if remote.status == "REVERT" else _unknown(lease)
        if remote.status == "REVERT" and not _data(lease.payment).get("receipt_seen"):
            _cancel_intent(lease, actor, "none", "REVERT", None)
            return _finish_cancellation(lease, remote)
        state = _cancel_state(lease.payment, remote, evidence, staff=staff)
        paid_at = evidence.paid_at if evidence else None
        immediate = state == "W_FOR_VERIFY" and paid_at is not None and timezone.now() - paid_at < timedelta(minutes=30)
        operation = "revert" if immediate else "cancel"
        _cancel_intent(lease, actor, operation, state, evidence)
        try:
            if immediate:
                _perform(lease, "revert", lambda p: gateway.revert(p, remote_state=state, paid_at=paid_at))
            else:
                _perform(lease, "cancel", lambda p: gateway.cancel(p, remote_state=state))
        except TorobPayError as error:
            if error.code in (1005, 1048, 1007):
                raise ReconciliationConflict("provider_token_mismatch") from None
            remote = _status(lease, gateway)
            if error.code == 1065:
                # 1065 is a definitive rejection, not a refund. Refresh status
                # and require a legal state before the different cancel action.
                if remote.status == "REVERT":
                    return _finish_cancellation(lease, remote)
                state = _cancel_state(lease.payment, remote,
                    evidence if evidence and remote.status == "PENDING" else None, staff=staff)
                _perform(lease, "cancel", lambda p: gateway.cancel(p, remote_state=state))
                remote = _status(lease, gateway)
            elif remote.status != "REVERT":
                return _rejected_operation(lease, error) if error.outcome == "rejected" else _unknown(lease)
        else:
            remote = _status(lease, gateway)
        return _finish_cancellation(lease, remote) if remote.status == "REVERT" else _unknown(lease)
    except ReconciliationConflict as error:
        return _review(lease, error.reason)
    except (TorobPayError, PaymentGatewayError):
        return _unknown(lease)
    except PaymentError:
        _unknown(lease)
        raise
    except OrderError:
        return _review(lease, "local_cancellation_rejected")


@transaction.atomic
def _finish_cancellation(lease, remote):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    _validate_status(payment, remote)
    data = _data(payment)
    if remote.status != "REVERT" or not data.get("cancellation_intent"):
        raise ReconciliationConflict("cancellation_not_confirmed")
    if payment.order.status != OrderStatus.CANCELLED and OrderStatus.CANCELLED not in ALLOWED_ORDER_TRANSITIONS.get(payment.order.status, set()):
        raise ReconciliationConflict("refund_confirmed_after_fulfillment")
    payment.status, payment.reservation_hold, payment.needs_refund = PaymentStatus.CANCELLED, False, False
    payment.reconciliation_lease_expires_at = None
    payment.error_message = "لغو پرداخت توسط ترب‌پی تأیید شد؛ زمان واریز بازپرداخت تابع درگاه است."
    data.update(cancellation_confirmed=True, last_known_order_state="CANCELED", api_status="REVERT")
    payment.metadata["remote_cancellation_confirmed"] = True
    payment.creation_status = PaymentCreationStatus.READY
    data.pop("uncertain_mutation", None)
    data["active_operation"].update(status="confirmed", finished_at=timezone.now().isoformat())
    payment.save()
    cancel_order(payment.order, mark_payment_cancelled=True)
    if payment.order.payment_status in (PaymentStatus.SUCCESSFUL, PaymentStatus.MANUAL_REVIEW):
        payment.order.payment_status = PaymentStatus.CANCELLED
        payment.order.save(update_fields=["payment_status", "updated_at"])
    _sync_order_payment_review(payment.order, resolved_receipt=True)
    return payment


def _revision(payment, quantities):
    if not isinstance(quantities, dict) or not quantities or any(type(key) is not int or type(value) is not int for key, value in quantities.items()):
        raise PaymentError("تعدادهای پیشنهادی اقلام معتبر نیستند.")
    order = payment.order
    if not order.is_paid or order.status not in (OrderStatus.PAID, OrderStatus.PREPARING) or order.inventory_released:
        raise PaymentError("ویرایش این سفارش در مرحلهٔ فعلی مجاز نیست.")
    if order.discount_total or _data(payment).get("provider_discounted") or _data(payment).get("manual_block") or _other_blockers(payment):
        raise PaymentError("سفارش تخفیف‌دار یا دارای پرداخت حل‌نشده قابل ویرایش نیست.")
    items = list(order.items.order_by("pk"))
    variant_ids = {item.variant_id for item in items if item.variant_id}
    if any(not item.variant_id for item in items) or ProductVariant.objects.filter(pk__in=variant_ids).count() != len(variant_ids):
        raise PaymentError("رزرو موجودی سفارش پیش از ویرایش باید بررسی شود.")
    if set(quantities) - {item.pk for item in items}:
        raise PaymentError("اقلام پیشنهادی متعلق به این سفارش نیستند.")
    proposed = []
    for item in items:
        quantity = quantities.get(item.pk, item.quantity)
        if not 0 <= quantity <= item.quantity:
            raise PaymentError("ویرایش ترب‌پی فقط برای کاهش تعداد یا حذف کالا مجاز است.")
        if quantity:
            revised = copy(item)
            revised.quantity = quantity
            revised.line_total = item.unit_price * quantity
            revised.discount_amount = (item.original_unit_price - item.unit_price) * quantity
            proposed.append(revised)
    if not proposed:
        raise PaymentError("برای حذف تمام اقلام، ابتدا لغو و بازپرداخت تأییدشده انجام شود.")
    revised_order = copy(order)
    revised_order._torobpay_items = tuple(proposed)
    revised_order.subtotal = sum((item.original_unit_price * item.quantity for item in proposed), Decimal(0))
    revised_order.discount_total = sum((item.discount_amount for item in proposed), Decimal(0))
    revised_order.total = revised_order.subtotal - revised_order.discount_total + order.shipping_fee
    if revised_order.total >= Decimal(_data(payment).get("amount_toman", str(payment.amount))):
        raise PaymentError("مبلغ جدید باید کمتر باشد؛ برای مبلغ برابر یا بیشتر، لغو تأییدشده و سفارش تازه لازم است.")
    return revised_order


@transaction.atomic
def _update_intent(lease, actor, quantities):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    _authorize(payment, actor, staff_only=True)
    revision = _revision(payment, quantities)
    _data(payment)["pending_update"] = {
        "actor_id": actor.pk, "quantities": {str(key): value for key, value in quantities.items()},
        "amount_rial": toman_to_rial(revision.total), "amount_toman": str(revision.total),
        "base_identity": lease.identity,
        "items_before": [{key: str(getattr(item, key)) if isinstance(getattr(item, key), Decimal) else getattr(item, key)
                          for key in _ITEM_FIELDS} for item in payment.order.items.order_by("pk")],
        "order_before": {key: str(getattr(payment.order, key)) for key in ("subtotal", "discount_total", "shipping_fee", "total")},
        "created_at": timezone.now().isoformat(),
    }
    payment.save(update_fields=["metadata"])
    lease.payment = payment
    return revision


def update_payment(payment, *, quantities, actor, evidence=None):
    """Staff-only service capability, no new storefront/admin edit UI.

Only saved item quantities may decrease. Saved prices, categories and shipping
are reused. Local totals/stock change only after confirmed remote update.
"""
    _unlocked()
    current = _load(payment)
    _authorize(current, actor, staff_only=True)
    if _data(current).get("cancellation_intent") or _data(current).get("uncertain_mutation", {}).get("operation") in ("verify", "settle", "revert", "cancel"):
        raise PaymentError("پیش از ویرایش، نتیجهٔ عملیات قبلی ترب‌پی باید تعیین تکلیف شود.")
    if _data(current).get("pending_update"):
        return reconcile_payment(current)  # Status first, no duplicate POST.
    _revision(current, quantities)  # Reject equal/increased/discount before HTTP.
    current, lease = _claim(current, "update")
    if not lease:
        raise PaymentError("عملیات قبلی پرداخت هنوز تعیین تکلیف نشده است.")
    try:
        gateway = TorobPayGateway()
        remote = _status(lease, gateway)
        state = _cancel_state(lease.payment, remote, evidence, staff=True)
        if state != "ONGOING":
            raise PaymentError("فقط سفارش فعال ترب‌پی قابل ویرایش است.")
        revision = _update_intent(lease, actor, quantities)
        try:
            _perform(lease, "update", lambda p: gateway.update(p, revision, remote_state=state))
        except TorobPayError as error:
            if error.code in (1005, 1048, 1007):
                raise ReconciliationConflict("provider_token_mismatch") from None
            remote = _status(lease, gateway, update=True)
            if remote.amount_rial != _data(lease.payment)["pending_update"]["amount_rial"]:
                return _rejected_operation(lease, error) if error.outcome == "rejected" else _unknown(lease)
        else:
            remote = _status(lease, gateway, update=True)
        return _finish_update(lease, remote)
    except ReconciliationConflict as error:
        return _review(lease, error.reason)
    except (TorobPayError, PaymentGatewayError):
        return _unknown(lease)
    except PaymentError:
        _unknown(lease)
        raise
    except OrderError:
        return _review(lease, "local_update_rejected")


@transaction.atomic
def _finish_update(lease, remote):
    payment = _locked(lease.payment.pk)
    _check_lease(payment, lease)
    _validate_status(payment, remote, update=True)
    data = _data(payment)
    proposal = data.get("pending_update", {})
    if remote.status != "SETTLE" or remote.amount_rial != proposal.get("amount_rial"):
        raise ReconciliationConflict("updated_amount_not_confirmed")
    revision = _revision(payment, {int(key): value for key, value in proposal["quantities"].items()})
    items = list(payment.order.items.select_for_update().order_by("pk"))
    variants = {variant.pk: variant for variant in ProductVariant.objects.select_for_update().filter(
        pk__in={item.variant_id for item in items if item.variant_id}).order_by("pk")}
    if any(not item.variant_id or item.variant_id not in variants for item in items):
        raise ReconciliationConflict("inventory_reservation_unavailable")
    for item in items:
        quantity = proposal["quantities"].get(str(item.pk), item.quantity)
        removed = item.quantity - quantity
        if removed:
            variants[item.variant_id].restore_stock(removed)
            if quantity:
                item.quantity = quantity
                item.line_total = item.unit_price * quantity
                item.discount_amount = (item.original_unit_price - item.unit_price) * quantity
                item.save(update_fields=["quantity", "line_total", "discount_amount"])
            else:
                item.delete()
    order = payment.order
    order.subtotal, order.discount_total, order.total = revision.subtotal, revision.discount_total, revision.total
    order.save(update_fields=["subtotal", "discount_total", "total", "updated_at"])
    data.setdefault("update_history", []).append({**proposal, "transaction_id": remote.transaction_id,
        "confirmed_at": timezone.now().isoformat(), "original_payment_amount_toman": str(payment.amount)})
    data.update(amount_toman=str(order.total), amount_rial=remote.amount_rial, transaction_id=remote.transaction_id,
                last_known_order_state="UPDATED", api_status="SETTLE")
    data.pop("pending_update", None)
    data.pop("uncertain_mutation", None)
    data["active_operation"].update(status="confirmed", finished_at=timezone.now().isoformat())
    payment.status = PaymentStatus.SUCCESSFUL
    payment.reservation_hold = False
    payment.reconciliation_lease_expires_at = None
    payment.error_message = "کاهش مبلغ توسط ترب‌پی تأیید شد؛ زمان بازپرداخت اختلاف تابع درگاه است."
    payment.save()
    return payment


def inspect_payment(payment):
    """Read-only gateway status; no local writes, verify, settle or refund."""
    _unlocked()
    current = _load(payment)
    if not current.provider_token:
        return {"payment_id": current.pk, "order_id": current.order_id, "token_present": False,
                "creation_status": current.creation_status, "manual_reconciliation_required": True}
    remote = TorobPayGateway().status(current)
    valid = True
    try:
        _validate_status(current, remote, update=bool(_data(current).get("pending_update")))
    except ReconciliationConflict:
        valid = False
    return {"payment_id": current.pk, "order_id": current.order_id, "token_present": True,
            "api_status": remote.status, "amount_rial": remote.amount_rial, "identity_matches": valid,
            "local_status": current.status, "reservation_hold": current.reservation_hold,
            "operation_status": _data(current).get("active_operation", {}).get("status", ""),
            "settle_confirmed_at": current.settle_confirmed_at.isoformat() if current.settle_confirmed_at else None,
            "reconciliation_lease_expires_at": current.reconciliation_lease_expires_at.isoformat() if current.reconciliation_lease_expires_at else None,
            "refund_execution": "none"}


def bind_recovered_token(payment, token, *, actor):
    """Support-obtained token must first match authenticated status + UUID.

No token from a browser callback is ever accepted. This only binds creation
evidence; it cannot fulfill an order or authorize a refund by itself.
"""
    _unlocked()
    current = _load(payment)
    _authorize(current, actor, staff_only=True)
    candidate = copy(current)
    candidate.provider_token, candidate.authority = token, token_fingerprint(token)
    remote = TorobPayGateway().status(candidate)
    _validate_status(current, remote)
    identity = _identity(current)
    with transaction.atomic():
        current = _locked(current.pk)
        if _identity(current) != identity or (current.provider_token and current.provider_token != token):
            raise PaymentError("تلاش پرداخت هنگام بررسی تغییر کرده است.")
        try:
            with transaction.atomic():
                current.provider_token, current.authority = token, token_fingerprint(token)
                # Status cannot recover the hosted URL. Never invent one.
                current.creation_status = PaymentCreationStatus.REVIEW
                current.reservation_hold = remote.status != "REVERT"
                data = _data(current)
                data.update(amount_toman=str(current.amount), amount_rial=toman_to_rial(current.amount),
                            transaction_id=str(current.request_id), recovered_token=True,
                            recovered_by=actor.pk, recovered_at=timezone.now().isoformat())
                current.save(update_fields=["provider_token", "authority", "creation_status", "reservation_hold", "metadata"])
        except IntegrityError:
            raise PaymentError("این شناسهٔ درگاه به تلاش دیگری متصل است؛ شواهد بازنویسی نشد.") from None
    return current


def confirm_updated_transaction(payment, *, actor, transaction_id, support_reference):
    """Resolve a lost updated-copy ID with independent staff evidence + status.

Only the original durable quantity/price proposal can be applied. This never
issues another update/refund request or rewrites the original receipt ID.
"""
    from .gateways.torobpay_data import identifier
    _unlocked()
    current = _load(payment)
    _authorize(current, actor, staff_only=True)
    transaction_id, support_reference = identifier(transaction_id), identifier(support_reference)
    data = _data(current)
    proposal = data.get("pending_update")
    if not proposal or data.get("manual_block") not in (None, "remote_transaction_mismatch"):
        raise PaymentError("پیشنهاد و شواهد لازم برای بازیابی ویرایش وجود ندارد.")
    identity = _identity(current)
    if proposal.get("base_identity") != identity:
        raise PaymentError("اطلاعات سفارش پس از پیشنهاد ویرایش تغییر کرده است.")
    remote = TorobPayGateway().status(current)
    if remote.status != "SETTLE" or remote.amount_rial != proposal["amount_rial"] or remote.transaction_id != transaction_id:
        raise PaymentError("شاهد مستقل و وضعیت سروری ویرایش مطابقت ندارند.")
    with transaction.atomic():
        current = _locked(current.pk)
        if _identity(current) != identity:
            raise PaymentError("سفارش هنگام بررسی ویرایش تغییر کرده است.")
        data = _data(current)
        data["pending_update"]["response_transaction_id"] = transaction_id
        data.setdefault("manual_update_evidence", []).append({"actor_id": actor.pk, "support_reference": support_reference,
            "transaction_id": transaction_id, "observed_at": timezone.now().isoformat()})
        data.pop("manual_block", None)
        current.save(update_fields=["metadata"])
    return reconcile_payment(current)
