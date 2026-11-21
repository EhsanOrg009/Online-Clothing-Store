"""Status-only by default; --apply uses the normal callback safety policy."""

import json
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from accounts.models import User
from orders.choices import PaymentStatus
from payments.maintenance import MaintenanceLeaseLost
from payments.gateways.base import PaymentGatewayError
from payments.management.torobpay_support import protected_backup, read_private_token
from payments.models import Payment, PaymentCreationStatus
from payments.services import PaymentError
from payments.torobpay_services import (
    _authorize, bind_recovered_token, confirm_updated_transaction, inspect_payment, reconcile_payment,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "استعلام امن ترب‌پی؛ apply فقط چرخهٔ عادی و بازیابی وضعیت را اعمال می‌کند و بازپرداخت جدید نمی‌فرستد."
    stealth_options = ("maintenance_heartbeat",)

    def add_arguments(self, parser):
        parser.add_argument("--payment-id", type=int, nargs="+")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--max-runtime-seconds", type=int)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        parser.add_argument("--token-file")
        parser.add_argument("--updated-transaction-id")
        parser.add_argument("--support-reference")
        parser.add_argument("--actor-id", type=int)
        parser.add_argument("--backup")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("limit باید بین ۱ تا ۵۰۰ باشد.")
        budget = options.get("max_runtime_seconds")
        if budget is not None and not 1 <= budget <= 240:
            raise CommandError("مهلت اجرای نرم باید بین ۱ تا ۲۴۰ ثانیه باشد.")
        selected = options.get("payment_id")
        if selected and len(set(selected)) != len(selected):
            raise CommandError("شناسهٔ پرداخت تکراری مجاز نیست.")
        if selected and len(selected) > options["limit"]:
            raise CommandError("تعداد شناسه‌های انتخابی از limit بیشتر است.")
        payments = Payment.objects.filter(gateway="torobpay")
        if selected:
            payments = payments.filter(pk__in=selected)
            if payments.count() != len(selected):
                raise CommandError("همهٔ شناسه‌ها باید پرداخت ترب‌پی موجود باشند.")
        else:
            payments = payments.filter(Q(reservation_hold=True) | Q(status__in=(PaymentStatus.PENDING, PaymentStatus.MANUAL_REVIEW))
                | Q(creation_status__in=(PaymentCreationStatus.REQUESTING, PaymentCreationStatus.UNCERTAIN, PaymentCreationStatus.REVIEW))
                | Q(reconciliation_lease_expires_at__isnull=False))
            if options["apply"]:
                from django.utils import timezone
                payments = payments.filter(Q(reconciliation_lease_expires_at__isnull=True)
                                             | Q(reconciliation_lease_expires_at__lte=timezone.now()))
        # Oldest unexamined/least-recently-attempted first. A large manual-review
        # backlog or fresh checkout traffic cannot permanently starve a receipt.
        payments = list(payments.order_by("reconciliation_attempted_at", "created_at", "pk")[:options["limit"]])
        manual = bool(options.get("token_file") or options.get("updated_transaction_id"))
        if options.get("token_file") and options.get("updated_transaction_id"):
            raise CommandError("اتصال توکن و بازیابی ویرایش دو عملیات جدا هستند.")
        if manual:
            if len(payments) != 1 or not selected or not options.get("actor_id"):
                raise CommandError("بازیابی دستی یک payment-id و actor-id مشخص می‌خواهد.")
            try:
                actor = User.objects.get(pk=options["actor_id"])
                _authorize(Payment.objects.select_related("order").get(pk=payments[0].pk), actor, staff_only=True)
            except (User.DoesNotExist, PaymentError) as error:
                raise CommandError("کاربر مجاز پشتیبانی برای این عملیات یافت نشد.") from error
            if not options["apply"]:
                raise CommandError("ابتدا استعلام معمولی انجام دهید؛ اتصال شواهد دستی به apply و بکاپ تأییدشده نیاز دارد.")
            try:
                with protected_backup(options.get("backup")):
                    if options.get("token_file"):
                        token = read_private_token(options["token_file"])
                        bind_recovered_token(payments[0], token, actor=actor)
                    else:
                        if not options.get("support_reference"):
                            raise CommandError("شناسهٔ شاهد مستقل پشتیبانی لازم است.")
                        confirm_updated_transaction(payments[0], actor=actor, transaction_id=options["updated_transaction_id"],
                                                    support_reference=options["support_reference"])
            except (PaymentError, PaymentGatewayError) as error:
                raise CommandError("شواهد دستی با پرداخت سروری مطابقت ندارند؛ هیچ درخواست مالی تازه‌ای ساخته نشد.") from error
            # Binding a missing create token never also verifies/fulfills.
            report = {"mode": "manual_evidence", "payment_id": payments[0].pk, "token_or_update_evidence_bound": True,
                      "new_refund_requests": False}
            self.stdout.write(json.dumps(report, ensure_ascii=False))
            return
        reports, failures = [], False
        stats = {"attempted": 0, "manual_review": 0, "uncertain": 0, "failed": 0}
        deadline = time.monotonic() + budget if budget is not None else None
        heartbeat = options.get("maintenance_heartbeat")
        for payment in payments:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if heartbeat:
                heartbeat()
            stats["attempted"] += 1
            try:
                if options["apply"]:
                    current = reconcile_payment(payment, heartbeat=heartbeat) if heartbeat else reconcile_payment(payment)
                    data = current.metadata.get("torobpay", {})
                    operation = data.get("active_operation", {})
                    if operation.get("status") == "uncertain":
                        failures = True
                        stats["uncertain"] += 1
                        logger.error("TorobPay reconciliation unresolved order_id=%s payment_id=%s operation=%s",
                                     current.order_id, current.pk, operation.get("last_step", {}).get("operation", ""))
                    manual_required = (not current.provider_token and current.creation_status != PaymentCreationStatus.REJECTED)
                    if current.requires_manual_review or manual_required:
                        stats["manual_review"] += 1
                        logger.warning("TorobPay reconciliation requires review order_id=%s payment_id=%s reason=%s",
                                       current.order_id, current.pk, data.get("manual_block", "token_unavailable" if manual_required else "receipt_review"))
                    reports.append({"payment_id": current.pk, "order_id": current.order_id, "local_status": current.status,
                        "creation_status": current.creation_status, "reservation_hold": current.reservation_hold,
                        "api_status": data.get("api_status", ""), "operation_status": operation.get("status", ""),
                        "settle_confirmed_at": current.settle_confirmed_at.isoformat() if current.settle_confirmed_at else None,
                        "manual_reconciliation_required": manual_required, "explicit_settle": bool(data.get("explicit_settle"))})
                else:
                    reports.append(inspect_payment(payment))
            except MaintenanceLeaseLost:
                raise
            except Exception as error:
                failures = True
                stats["failed"] += 1
                logger.error("TorobPay reconciliation failed order_id=%s payment_id=%s error_type=%s",
                             payment.order_id, payment.pk, type(error).__name__)
                reports.append({"payment_id": payment.pk, "result": "نیازمند بررسی پشتیبانی؛ استعلام قابل تأیید نیست."})
        self.stdout.write(json.dumps({"mode": "apply" if options["apply"] else "dry_run", "payments": reports,
                                      "new_refund_requests": False, "stats": stats,
                                      "budget_exhausted": bool(deadline and time.monotonic() >= deadline)}, ensure_ascii=False, indent=2))
        logger.log(logging.ERROR if failures else logging.INFO,
                   "TorobPay reconciliation finished attempted=%s manual_review=%s uncertain=%s failed=%s",
                   stats["attempted"], stats["manual_review"], stats["uncertain"], stats["failed"])
        if failures:
            raise CommandError("بخشی از استعلام‌ها کامل نشد؛ هیچ پرداخت یا بازپرداخت تازه‌ای ساخته نشد.")
