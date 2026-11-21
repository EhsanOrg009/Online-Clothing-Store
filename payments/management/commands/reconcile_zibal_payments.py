"""Read-only inquiries by default; verified, explicitly selected recovery on apply."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
import zlib
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from orders.choices import PaymentStatus
from orders.models import Order
from payments.gateways import PaymentGatewayError, get_gateway
from payments.gateways.zibal import _track_id
from payments.gateways.zibal_time import confirmed_zone
from payments.models import Payment, PaymentCreationStatus
from payments.services import (
    PaymentError, classify_receipt, process_callback, potentially_successful_receipt,
    receipt_history_fingerprint, receipt_review_reasons, recovery_receipt_snapshot, _authorize_recovery_primary,
)
from scripts.db_backup import BackupError, protect_recovery_backup, verify_backup_checksum


def validate_backup(filename: str, database: str) -> dict:
    """Recorded SHA256, full gzip CRC, expected schema and completed dump header.

    Readability/integrity is not a restore drill. SQL contents are never printed.
    Use a read-only mount for both the backup and its existing checksum sidecar.
    """
    path = Path(filename)
    try:
        stat = path.stat()
        age = timezone.now().timestamp() - stat.st_mtime
        if not path.name.endswith(".sql.gz") or not 0 <= age <= 3600:
            raise CommandError("Backup must be a .sql.gz file created within the last hour.")
        digest = verify_backup_checksum(path)
        markers = {
            b"MariaDB dump": False,
            b"CREATE TABLE `payments_payment`": False,
            b"CREATE TABLE `orders_order`": False,
            b"CREATE TABLE `products_productvariant`": False,
            b"-- Dump completed on ": False,
        }
        database_matches = False
        with gzip.open(path, "rb") as dump:
            for line in dump:
                if line.startswith(b"-- Host:") and line.partition(b"Database: ")[2].strip() == str(database).encode():
                    database_matches = True
                if b"MySQL dump" in line:
                    markers[b"MariaDB dump"] = True
                for marker in markers:
                    if marker in line:
                        markers[marker] = True
        if not database_matches or not all(markers.values()):
            raise CommandError("Backup is not a completed SQL dump of the expected database and critical tables.")
        # Recheck after decompression, not just before it.
        if verify_backup_checksum(path) != digest or path.stat().st_mtime_ns != stat.st_mtime_ns:
            raise CommandError("Backup changed during validation.")
    except BackupError as exc:
        raise CommandError(f"Backup validation failed: {exc} No recovery was performed.") from exc
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise CommandError("Backup is missing, unreadable or corrupt; no recovery was performed.") from exc
    return {"file": str(path), "sha256": digest, "sha256_sidecar_check": "passed", "gzip_and_sql_checks": "passed"}


def order_snapshot(order: Order) -> dict:
    """Financial/inventory fields only; no customer data, secrets or raw metadata."""
    return {
        "id": order.pk, "order_number": order.order_number,
        "status": order.status, "payment_status": order.payment_status,
        "total_toman": str(order.total), "paid_at": order.paid_at,
        "expires_at": order.expires_at, "inventory_released": order.inventory_released,
        "can_pay": order.can_pay,
        "payments": list(order.payments.order_by("created_at", "pk").values(
            "id", "authority", "request_id", "creation_status", "gateway", "amount", "status", "reference_id",
            "transaction_id", "callback_count", "order_id", "created_at",
            "verified_at", "paid_at", "gateway_paid_at", "needs_refund",
        )),
        "inventory": list(order.items.order_by("pk").values(
            "id", "variant_id", "quantity", "variant__stock",
        )),
    }


def timestamp_for_report(value):
    # Malformed arbitrary gateway fields can contain sensitive strings/objects.
    # Keep the original in the private verification audit, not in console output.
    if isinstance(value, str) and re.fullmatch(r"[0-9TZ:+.\-]{19,40}", value):
        return value
    return "invalid timestamp; inspect the private gateway verification audit"


class Command(BaseCommand):
    help = "Inspect Zibal receipts; --apply requires a fresh SQL backup with a matching SHA256 sidecar."

    def add_arguments(self, parser):
        parser.add_argument("--order-number", required=True)
        parser.add_argument("--track-id", nargs="+", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--request-id",
            help="Resolve one durable creation attempt against one explicit track ID, without fulfillment; then inspect the complete ledger.",
        )
        parser.add_argument("--backup", help="Fresh .sql.gz plus its .sha256 sidecar, both readable.")
        parser.add_argument(
            "--gateway-timezone",
            help="Only an officially confirmed IANA timezone for offset-free paidAt; defaults to ZIBAL_PAID_AT_TIMEZONE.",
        )

    def handle(self, *args, **options):
        if options["apply"] and options.get("backup"):
            try:
                with protect_recovery_backup(options["backup"]):
                    return self.reconcile(*args, **options)
            except BackupError as exc:
                raise CommandError(f"Recovery backup lock failed: {exc}") from exc
        return self.reconcile(*args, **options)

    def reconcile(self, *args, **options):
        track_ids = options["track_id"]
        if len(set(track_ids)) != len(track_ids):
            raise CommandError("Duplicate track IDs are not allowed.")
        gateway_timezone = options.get("gateway_timezone")
        if gateway_timezone is not None:
            try:
                confirmed_zone(gateway_timezone)
            except ValueError as exc:
                raise CommandError("Gateway timezone must be an explicitly confirmed IANA identifier.") from exc
        try:
            for track_id in track_ids:
                _track_id(track_id)
            order = Order.objects.get(order_number=options["order_number"])
            gateway = get_gateway("zibal")
        except (Order.DoesNotExist, PaymentGatewayError) as exc:
            raise CommandError("Order, transaction IDs or Zibal configuration are invalid.") from exc
        if gateway_timezone is not None:
            gateway.paid_at_timezone = gateway_timezone
        if options.get("request_id"):
            return self.resolve_creation_request(order, gateway, **options)
        payments = list(order.payments.order_by("created_at", "pk"))
        by_authority = {payment.authority: payment for payment in payments}
        if any(track_id not in by_authority or by_authority[track_id].gateway != "zibal" for track_id in track_ids):
            raise CommandError("Every selected transaction must be a Zibal payment belonging to this order.")
        report = {
            "mode": "apply" if options["apply"] else "inspect",
            "active_gateway": settings.PAYMENT_GATEWAY,
            "merchant_configured": bool(settings.ZIBAL_MERCHANT.strip()),
            "stored_amount_unit": "toman", "gateway_amount_unit": "rial",
            "gateway_timezone": gateway.paid_at_timezone or "unconfirmed; naive timestamps require manual review",
            "before": order_snapshot(order), "gateway": {}, "blocking_issues": [], "replayed": [],
        }
        if options["apply"]:
            if not options["backup"]:
                raise CommandError("--apply requires --backup and its recorded .sha256; no records changed.")
            report["backup"] = validate_backup(options["backup"], connection.settings_dict["NAME"])

        manual = [p for p in payments if p.status == PaymentStatus.MANUAL_REVIEW]
        potential = {p.authority for p in payments if p.authority and potentially_successful_receipt(p)}
        unresolved_creation = [p for p in payments if p.creation_status in (
            PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN, PaymentCreationStatus.REVIEW,
        ) or (not p.authority and (
            p.creation_status != PaymentCreationStatus.REJECTED or potentially_successful_receipt(p)
        ))]
        report["unresolved_creation_requests"] = [
            {"payment_id": p.pk, "request_id": str(p.request_id) if p.request_id else None,
             "track_id": p.authority, "creation_status": p.creation_status}
            for p in unresolved_creation
        ]
        if unresolved_creation:
            report["blocking_issues"].append("Unresolved gateway creation attempts prevent primary selection; validate their track IDs before recovery.")
        report["existing_manual_review_receipts"] = [p.authority for p in manual]
        report["unselected_manual_review_receipts"] = [p.authority for p in manual if p.authority not in track_ids]
        report["historical_non_zibal_receipts"] = [p.authority for p in payments if p.gateway != "zibal"]
        report["incomplete_inquiries"] = []
        report["primary_selection_blockers"] = []
        report["unresolved_provider_payments"] = [p.pk for p in payments if p.reservation_hold]
        if report["unresolved_provider_payments"]:
            issue = "Unresolved remote payment operations prevent primary selection; reconcile their provider before recovery."
            report["blocking_issues"].append(issue)
            report["primary_selection_blockers"].append(issue)
        if report["unselected_manual_review_receipts"]:
            report["blocking_issues"].append("Unselected manual-review receipts prevent primary selection.")
        primary = [payment for payment in payments if payment.is_successful and not payment.needs_refund]
        verified = {}
        for payment in payments:
            payment.order = order
            key = payment.authority or "request:" + str(payment.request_id or payment.pk)
            evidence = report["gateway"][key] = {}
            if not payment.authority:
                evidence["creation_status"] = payment.creation_status
                evidence["error"] = "Documented request rejection; no remote track ID." if (
                    payment.creation_status == PaymentCreationStatus.REJECTED
                ) else "Unknown remote track ID; use --request-id with an independently obtained track ID."
                continue
            if payment.gateway != "zibal":
                evidence["error"] = "Historical gateway requires an independent audit."
                if potentially_successful_receipt(payment):
                    report["blocking_issues"].append("A historical successful receipt needs independent validation.")
                continue
            try:
                if payment.amount != order.total:
                    raise PaymentGatewayError("Local payment and order amounts do not match.")
                data = gateway._post("/v1/inquiry", {
                    "merchant": gateway.merchant, "trackId": _track_id(payment.authority),
                })
                evidence.update({
                    "inquiry_result": data["result"],
                    "amount_rial": data.get("amount") if type(data.get("amount")) is int else None,
                    "amount_matches": type(data.get("amount")) is int and data["amount"] == int(payment.amount) * 10,
                    "order_matches": data.get("orderId") == order.order_number,
                    "track_matches": "not included; Inquiry requested this track ID" if "trackId" not in data
                    else _track_id(data["trackId"]) == _track_id(payment.authority),
                })
                if data["result"] != 100:
                    raise gateway._api_error(data["result"])
                status = gateway._check_details(payment, data)
                evidence["status"] = status
                if status != 1:
                    evidence["verified"] = False
                    if potentially_successful_receipt(payment) or status == 2:
                        raise PaymentGatewayError("A recorded or potentially paid receipt is not gateway-verified.")
                    report["incomplete_inquiries"].append(payment.authority)
                    continue
                result = gateway._verified_result(payment, data, 100)
                if (payment.transaction_id and payment.transaction_id != result.transaction_id) or (
                    payment.reference_id and (payment.reference_id != result.reference_id
                                             or payment.transaction_id != result.transaction_id)
                ):
                    raise PaymentGatewayError("Recorded receipt identifiers differ from gateway evidence.")
                decision = classify_receipt(
                    order, result.gateway_paid_at, result.timestamp_issue,
                    other_manual_receipt=any(p.pk != payment.pk for p in manual),
                )
                evidence.update({
                    "verified": True, "reference_id": result.reference_id,
                    "paid_at": timestamp_for_report(data.get("paidAt")),
                    "normalized_paid_at": result.gateway_paid_at,
                    "timestamp_interpretation": result.raw["payment_time"],
                    "action": decision.action, "reason": decision.reason,
                })
                verified[payment.authority] = result
            except PaymentGatewayError as exc:
                evidence.update({"verified": False, "error": str(exc)})
                report["incomplete_inquiries"].append(payment.authority)
                if payment.authority not in track_ids or potentially_successful_receipt(payment):
                    report["blocking_issues"].append(f"Order history could not be validated for {payment.authority}.")

        chronological = sorted(
            (key for key, result in verified.items() if not result.timestamp_issue and result.gateway_paid_at),
            key=lambda key: (verified[key].gateway_paid_at, by_authority[key].created_at, by_authority[key].pk),
        )
        ambiguous = [key for key, result in verified.items() if result.timestamp_issue]
        report["gateway_time_order"] = chronological
        report["ambiguous_timestamps"] = ambiguous
        report["duplicate_successful_payments"] = list(verified) if len(verified) > 1 else []
        potential.update(verified)
        report["potentially_successful_receipts"] = sorted(potential)
        report["confirmed_successful_receipts"] = list(verified)
        unresolved = potential - set(verified)
        unselected = [key for key in potential if key not in track_ids and (
            not order.is_paid or not by_authority[key].is_successful
        )]
        report["unselected_potentially_successful_receipts"] = sorted(unselected)
        if unselected:
            report["blocking_issues"].append("Include all potentially successful unrecovered receipts explicitly: " + ", ".join(sorted(unselected)))
        if unresolved:
            report["blocking_issues"].append("Potentially successful receipts have incomplete validation: " + ", ".join(sorted(unresolved)))
        independent_review = [p.authority for p in manual if receipt_review_reasons(p) & {
            "future_timestamp", "previous_future_timestamp", "gateway_receipt_conflict",
            "receipt_identifiers_changed", "payment_identity_changed_during_verification", "previous_receipt_conflict",
        }]
        selection_blockers = report["primary_selection_blockers"]
        selection_blockers.extend(report["blocking_issues"])
        if ambiguous:
            selection_blockers.append("Ambiguous gateway timestamps prevent unique primary selection.")
        if independent_review:
            selection_blockers.append("Previously conflicting or future receipts require independent human review.")
        if any(key not in verified for key in track_ids):
            selection_blockers.append("Not every selected transaction has a validated successful Inquiry.")
        report["planned_primary"] = None
        if order.is_paid:
            if len(primary) != 1 or primary[0].authority not in verified:
                report["blocking_issues"].append("Paid order needs exactly one independently validated primary receipt.")
            else:
                report["planned_primary"] = primary[0].authority
        elif primary:
            report["blocking_issues"].append("An unpaid order already has a primary receipt; review the ledger.")
        elif not selection_blockers:
            candidates = [key for key in chronological if key in track_ids and classify_receipt(
                order, verified[key].gateway_paid_at, verified[key].timestamp_issue,
            ).action == "accept"]
            if candidates:
                earliest = verified[candidates[0]].gateway_paid_at
                if sum(verified[key].gateway_paid_at == earliest for key in candidates) == 1:
                    report["planned_primary"] = candidates[0]
                else:
                    selection_blockers.append("Equal earliest gateway times do not establish a unique primary.")
        report["recovery_plan"] = {
            "order_id": order.pk, "inspected_payment_ids": sorted(p.pk for p in payments),
            "selected_track_ids": track_ids, "potentially_successful_receipts": sorted(potential),
            "primary": report["planned_primary"], "history_fingerprint": receipt_history_fingerprint(payments),
            "receipts": {key: recovery_receipt_snapshot(result) for key, result in verified.items()},
            "backup_sha256": report.get("backup", {}).get("sha256"),
        }
        authorization = False
        if options["apply"] and not order.is_paid and report["planned_primary"] and not selection_blockers:
            authorization = _authorize_recovery_primary(
                order=order, payments=payments, verified=verified, selected_track_ids=track_ids,
                primary=by_authority[report["planned_primary"]], backup=report["backup"], plan=report["recovery_plan"],
            )

        replay_failures = []
        if options["apply"] and not report["blocking_issues"]:
            # An absent/failed primary is never replaced by every other receipt.
            # Others retain their successful evidence in manual review until retry.
            for track_id in [*chronological, *ambiguous]:
                if track_id not in track_ids:
                    continue
                payment = Payment.objects.select_related("order").get(pk=by_authority[track_id].pk)
                try:
                    payment = process_callback(
                        payment, {"success": "1", "status": "2", "trackId": track_id, "orderId": order.order_number},
                        gateway_timezone=gateway_timezone,
                        allow_fulfillment=track_id == report["planned_primary"] and not selection_blockers,
                        recovery_primary_authorized=authorization if track_id == report["planned_primary"] else False,
                        recovery_audit={
                            "source": "reconcile_zibal_payments", "backup_sha256": report["backup"]["sha256"],
                            "backup_file": Path(report["backup"]["file"]).name,
                            "selected_track_ids": track_ids, "planned_primary": report["planned_primary"],
                            "recovery_plan": report["recovery_plan"],
                        },
                    )
                    report["replayed"].append({
                        "track_id": track_id, "status": payment.status,
                        "needs_refund": payment.needs_refund, "reference_id": payment.reference_id,
                        "gateway_paid_at": payment.gateway_paid_at,
                        "decision": payment.metadata.get("decision"),
                        "verify_result": payment.metadata.get("verification", {}).get("verify_result"),
                    })
                    if not payment.is_successful:
                        replay_failures.append(track_id)
                except PaymentError:
                    replay_failures.append(track_id)
                    report["replayed"].append({"track_id": track_id, "error": "Verification failed; this transaction rolled back."})

        order.refresh_from_db()
        report["after"] = order_snapshot(order)
        report["inventory_unchanged"] = report["before"]["inventory"] == report["after"]["inventory"]
        report["successful_count"] = order.payments.filter(status=PaymentStatus.SUCCESSFUL).count()
        report["refund_required"] = list(order.payments.filter(
            status=PaymentStatus.SUCCESSFUL, needs_refund=True,
        ).values("authority", "reference_id", "amount"))
        report["manual_review_required"] = list(order.payments.filter(
            status=PaymentStatus.MANUAL_REVIEW,
        ).values("authority", "reference_id", "amount", "gateway_paid_at"))
        report["actual_primary"] = list(order.payments.filter(
            status=PaymentStatus.SUCCESSFUL, needs_refund=False,
        ).values_list("authority", flat=True))
        report["refund_execution"] = "No refund was requested or performed."
        self.stdout.write(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        failed_preflight = any(not report["gateway"][track_id].get("verified") for track_id in track_ids)
        if options["apply"] and (report["blocking_issues"] or replay_failures or failed_preflight or selection_blockers):
            raise CommandError("Recovery is incomplete or requires manual review. Read the sanitized report; no receipt was forced successful.")

    def resolve_creation_request(self, order, gateway, **options):
        """Bind authenticated gateway evidence only; never authorize a primary.

        The request UUID is local correlation, not proof of payment or a gateway
        idempotency key. A fresh backup is still mandatory for local writes.
        """
        if len(options["track_id"]) != 1:
            raise CommandError("--request-id requires exactly one explicit --track-id; complete primary recovery is a separate preflight.")
        try:
            request_id = UUID(options["request_id"])
            payment = order.payments.get(request_id=request_id, gateway="zibal")
        except (ValueError, TypeError, Payment.DoesNotExist) as exc:
            raise CommandError("Creation request must belong to this order and Zibal.") from exc
        track_id = options["track_id"][0]
        if payment.authority not in (None, track_id) or Payment.objects.exclude(pk=payment.pk).filter(authority=track_id).exists():
            raise CommandError("Track ID is already linked to a different attempt; no evidence was overwritten.")
        if payment.amount != order.total:
            raise CommandError("Local attempt and order amounts differ; no binding was authorized.")
        report = {"mode": "resolve_request_apply" if options["apply"] else "resolve_request_inspect",
                  "before": order_snapshot(order), "request_id": str(request_id), "track_id": track_id,
                  "fulfillment_authorized": False, "refund_execution": "No refund was requested or performed."}
        if options["apply"]:
            if not options.get("backup"):
                raise CommandError("--apply requires --backup and its recorded .sha256; no records changed.")
            report["backup"] = validate_backup(options["backup"], connection.settings_dict["NAME"])
        payment.order = order
        original_authority = payment.authority
        payment.authority = track_id  # An in-memory Inquiry candidate, never a write.
        try:
            data = gateway._post("/v1/inquiry", {"merchant": gateway.merchant, "trackId": _track_id(track_id)})
            if data["result"] != 100:
                raise gateway._api_error(data["result"])
            status = gateway._check_details(payment, data)
            if status == 1:
                result = gateway._verified_result(payment, data, 100)
                if payment.reference_id and payment.reference_id != result.reference_id:
                    raise PaymentGatewayError("Recorded reference differs from Inquiry evidence.")
            report["inquiry"] = {"result": 100, "status": status, "amount_matches": True,
                                 "order_matches": True, "track_id": track_id}
        except PaymentGatewayError as exc:
            raise CommandError("Creation Inquiry failed validation; the attempt remains unresolved.") from exc
        payment.authority = original_authority
        plan = {"payment_id": payment.pk, "request_id": str(request_id), "track_id": track_id,
                "order_id": order.pk, "inquiry": report["inquiry"], "allow_fulfillment": False,
                "backup_sha256": report.get("backup", {}).get("sha256")}
        report["creation_resolution_plan"] = plan
        if options["apply"]:
            try:
                payment = process_callback(
                    payment, {"success": "1", "status": "2", "trackId": track_id,
                              "orderId": order.order_number, "request_id": str(request_id)},
                    gateway_timezone=options.get("gateway_timezone"), allow_fulfillment=False,
                    recovery_audit={"source": "reconcile_zibal_payment_request", "creation_resolution_plan": plan,
                                    "backup_sha256": report["backup"]["sha256"],
                                    "backup_file": Path(report["backup"]["file"]).name},
                )
            except PaymentError as exc:
                raise CommandError("Creation resolution is incomplete; retry after reviewing gateway evidence.") from exc
            report["resolved"] = payment.authority == track_id and payment.creation_status == PaymentCreationStatus.READY
        order.refresh_from_db()
        report["after"] = order_snapshot(order)
        report["next_step"] = "Inspect all order track IDs without --request-id; primary selection still requires complete validated recovery."
        self.stdout.write(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        if options["apply"] and not report["resolved"]:
            raise CommandError("Creation evidence requires manual review; no primary was authorized.")
