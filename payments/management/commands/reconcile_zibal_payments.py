"""Inspect gateway evidence, then replay explicitly selected payments through the service.

Inspection only calls Inquiry. Applying requires a fresh, readable SQL backup and
repeats server-side verification in process_callback. This command never refunds.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from orders.choices import PaymentStatus
from orders.models import Order
from payments.gateways import PaymentGatewayError, get_gateway
from payments.gateways.zibal import _track_id
from payments.models import Payment
from payments.services import PaymentError, process_callback


def validate_backup(filename: str, database: str) -> dict:
    """Check gzip integrity, the database header, critical tables and dump completion.

    SQL contents are never printed. This checks readability, not a restore test.
    Mount the host backup directory read-only into the one-off recovery container.
    """
    path = Path(filename)
    try:
        stat = path.stat()
        age = timezone.now().timestamp() - stat.st_mtime
        if not path.is_file() or not 0 <= age <= 3600:
            raise CommandError("Backup must be a file created within the last hour.")
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
                if line.startswith(b"-- Host:") and line.partition(b"Database: ")[2].strip() == database.encode():
                    database_matches = True
                if b"MySQL dump" in line:
                    markers[b"MariaDB dump"] = True
                for marker in markers:
                    if marker in line:
                        markers[marker] = True
        if not database_matches or not all(markers.values()):
            raise CommandError("Backup is not a completed SQL dump of the expected database and tables.")
        with path.open("rb") as compressed:
            digest = hashlib.file_digest(compressed, "sha256").hexdigest()
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise CommandError("Backup is missing, unreadable or corrupt; no recovery was performed.") from exc
    return {"file": str(path), "sha256": digest, "gzip_and_sql_checks": "passed"}


def order_snapshot(order: Order) -> dict:
    """Explicit financial/inventory fields only; no customer data or raw metadata."""
    return {
        "id": order.pk,
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_toman": str(order.total),
        "paid_at": order.paid_at,
        "expires_at": order.expires_at,
        "can_pay": order.can_pay,
        "payments": list(order.payments.order_by("created_at", "pk").values(
            "id", "authority", "gateway", "amount", "status", "reference_id",
            "transaction_id", "callback_count", "order_id", "created_at",
            "verified_at", "paid_at", "needs_refund",
        )),
        "inventory": list(order.items.order_by("pk").values(
            "id", "variant_id", "quantity", "variant__stock",
        )),
    }


class Command(BaseCommand):
    help = "Inspect Zibal payments; optionally replay verified transactions after checking a fresh backup."

    def add_arguments(self, parser):
        parser.add_argument("--order-number", required=True)
        parser.add_argument("--track-id", nargs="+", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--backup", help="Path to a fresh .sql.gz backup, readable by this process.")
        parser.add_argument(
            "--gateway-timezone",
            help="Confirmed timezone for offset-free gateway timestamps, if mixed with offset-bearing timestamps.",
        )

    def handle(self, *args, **options):
        track_ids = options["track_id"]
        if len(set(track_ids)) != len(track_ids):
            raise CommandError("Duplicate track IDs are not allowed.")
        try:
            for track_id in track_ids:
                _track_id(track_id)
            order = Order.objects.get(order_number=options["order_number"])
            gateway = get_gateway("zibal")
        except (Order.DoesNotExist, PaymentGatewayError) as exc:
            raise CommandError("Order, transaction IDs or Zibal configuration are invalid.") from exc
        payments = list(order.payments.order_by("created_at", "pk"))
        by_authority = {payment.authority: payment for payment in payments}
        if any(track_id not in by_authority or by_authority[track_id].gateway != "zibal" for track_id in track_ids):
            raise CommandError("Every selected transaction must be a Zibal payment belonging to this order.")
        confirmed_timezone = None
        if options["gateway_timezone"]:
            try:
                confirmed_timezone = ZoneInfo(options["gateway_timezone"])
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise CommandError("Gateway timezone is invalid.") from exc

        report = {
            "mode": "apply" if options["apply"] else "inspect",
            "active_gateway": settings.PAYMENT_GATEWAY,
            "merchant_configured": bool(settings.ZIBAL_MERCHANT.strip()),
            "stored_amount_unit": "toman",
            "gateway_amount_unit": "rial",
            "before": order_snapshot(order),
            "gateway": {},
            "blocking_issues": [],
            "replayed": [],
        }
        if options["apply"]:
            if not options["backup"]:
                raise CommandError("--apply requires --backup; no payment or order was changed.")
            report["backup"] = validate_backup(options["backup"], connection.settings_dict["NAME"])

        verified = {}
        for payment in payments:
            payment.order = order
            evidence = report["gateway"][payment.authority] = {}
            if payment.gateway != "zibal":
                evidence["error"] = "Historical gateway cannot be revalidated by Zibal."
                if payment.is_successful:
                    report["blocking_issues"].append("A historical successful payment requires an independent audit.")
                continue
            try:
                if payment.amount != order.total:
                    raise PaymentGatewayError("Local payment and order amounts do not match.")
                data = gateway._post("/v1/inquiry", {
                    "merchant": gateway.merchant, "trackId": _track_id(payment.authority),
                })
                evidence["inquiry_result"] = data["result"]
                if data["result"] != 100:
                    raise gateway._api_error(data["result"])
                status = gateway._check_details(payment, data)
                evidence.update({
                    "status": status, "amount_rial": data["amount"],
                    "amount_matches": True, "order_matches": True,
                })
                if status != 1:
                    if payment.is_successful or status == 2:
                        raise PaymentGatewayError("A recorded or potentially paid transaction is not gateway-verified.")
                    evidence["verified"] = False
                    continue
                result = gateway._verified_result(payment, data, 100)
                paid_at = parse_datetime(data["paidAt"])
                if timezone.is_naive(paid_at) and confirmed_timezone:
                    paid_at = timezone.make_aware(paid_at, confirmed_timezone)
                if payment.is_successful and (
                    payment.reference_id != result.reference_id or payment.transaction_id != payment.authority
                ):
                    raise PaymentGatewayError("Recorded successful payment identifiers differ from gateway evidence.")
                if payment.status == PaymentStatus.CANCELLED:
                    raise PaymentGatewayError("A locally cancelled payment requires review; the callback service treats it as terminal.")
                evidence.update({
                    "verified": True, "reference_id": result.reference_id,
                    "paid_at": data["paidAt"], "normalized_paid_at": paid_at,
                    "track_matches": True if "trackId" in data else "not included; Inquiry requested this track ID",
                })
                verified[payment.authority] = (paid_at, payment.created_at, payment.authority)
            except PaymentGatewayError as exc:
                # Gateway exceptions contain local messages only, never API bodies.
                evidence.update({"verified": False, "error": str(exc)})
                if payment.authority not in track_ids or payment.is_successful:
                    report["blocking_issues"].append(f"Order history could not be validated for {payment.authority}.")

        timestamp_types = {timezone.is_aware(value[0]) for value in verified.values()}
        if len(timestamp_types) > 1:
            report["blocking_issues"].append("Gateway timestamps mix naive and aware values; confirm --gateway-timezone before replay.")
            chronological = []
        else:
            chronological = sorted(verified, key=verified.get)
        report["gateway_time_order"] = chronological
        report["gateway_timezone"] = options["gateway_timezone"] or "Compare gateway timestamps on the same API clock; no timezone conversion."

        primary = [payment for payment in payments if payment.is_successful and not payment.needs_refund]
        if order.is_paid:
            if len(primary) != 1 or primary[0].authority not in verified:
                report["blocking_issues"].append("Paid order does not have exactly one independently validated primary payment.")
            report["planned_primary"] = primary[0].authority if len(primary) == 1 else None
        elif primary:
            report["blocking_issues"].append("An unpaid order already has a successful payment without a refund flag; review the ledger.")
            report["planned_primary"] = None
        else:
            report["planned_primary"] = next((key for key in chronological if key in track_ids), None) if order.can_pay else None

        unselected = [key for key in chronological if key not in track_ids and not by_authority[key].is_successful]
        if unselected:
            report["blocking_issues"].append(
                "Other gateway-verified payments need explicit recovery; include their track IDs: " + ", ".join(unselected)
            )
        replay_failures = []
        if options["apply"] and not report["blocking_issues"]:
            for track_id in chronological:
                if track_id not in track_ids:
                    continue
                payment = Payment.objects.select_related("order").get(pk=by_authority[track_id].pk)
                try:
                    payment = process_callback(payment, {
                        "success": "1", "status": "2", "trackId": track_id,
                        "orderId": order.order_number,
                    })
                    report["replayed"].append({
                        "track_id": track_id, "status": payment.status,
                        "needs_refund": payment.needs_refund, "reference_id": payment.reference_id,
                        "verify_result": payment.metadata.get("verification", {}).get("verify_result"),
                    })
                    if not payment.is_successful:
                        replay_failures.append(track_id)
                except PaymentError:
                    # Each service invocation has its own atomic transaction. A
                    # failed replay stays retryable while other valid ones proceed.
                    replay_failures.append(track_id)
                    report["replayed"].append({"track_id": track_id, "error": "Verification failed; local changes rolled back."})

        order.refresh_from_db()
        report["after"] = order_snapshot(order)
        report["inventory_unchanged"] = report["before"]["inventory"] == report["after"]["inventory"]
        report["successful_count"] = order.payments.filter(status=PaymentStatus.SUCCESSFUL).count()
        report["refund_required"] = list(order.payments.filter(
            status=PaymentStatus.SUCCESSFUL, needs_refund=True,
        ).values("authority", "reference_id", "amount"))
        report["actual_primary"] = list(order.payments.filter(
            status=PaymentStatus.SUCCESSFUL, needs_refund=False,
        ).values_list("authority", flat=True))
        report["refund_execution"] = "Not requested or performed by this command; dashboard confirmation is required."
        self.stdout.write(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        failed_preflight = any(not report["gateway"][track_id].get("verified") for track_id in track_ids)
        if options["apply"] and (report["blocking_issues"] or replay_failures or failed_preflight):
            raise CommandError("Recovery is incomplete. Review the sanitized report; failed transactions were not forced successful.")
