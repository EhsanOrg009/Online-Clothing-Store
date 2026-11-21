"""Explicit authorized cancellation; private fresh backup required on apply."""

from datetime import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import User
from payments.gateways.base import PaymentGatewayError
from payments.management.torobpay_support import protected_backup
from payments.models import Payment
from payments.services import PaymentError
from payments.torobpay_services import RemoteEvidence, _authorize, cancel_payment, inspect_payment


class Command(BaseCommand):
    help = "لغو و بازپرداخت صریح ترب‌پی؛ پیش‌فرض فقط استعلام، apply نیازمند کاربر مجاز و بکاپ معتبر است."

    def add_arguments(self, parser):
        parser.add_argument("--payment-id", type=int, required=True)
        parser.add_argument("--actor-id", type=int, required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        parser.add_argument("--backup")
        parser.add_argument("--remote-state", choices=("W_FOR_VERIFY", "W_FOR_SETTLE", "ONGOING"))
        parser.add_argument("--support-reference")
        parser.add_argument("--gateway-paid-at", help="زمان واقعی با offset صریح، فقط از شاهد مستقل ترب‌پی؛ نه زمان محلی callback.")

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(pk=options["actor_id"])
            payment = Payment.objects.select_related("order").get(pk=options["payment_id"], gateway="torobpay")
            _authorize(payment, actor, staff_only=True)
            if not options["apply"]:
                self.stdout.write(json.dumps(inspect_payment(payment), ensure_ascii=False, indent=2))
                return
            evidence = None
            if options.get("remote_state"):
                if not options.get("support_reference"):
                    raise CommandError("وضعیت دقیق به شاهد مستقل پشتیبانی نیاز دارد.")
                paid_at = None
                if options.get("gateway_paid_at"):
                    paid_at = datetime.fromisoformat(options["gateway_paid_at"].replace("Z", "+00:00"))
                    if timezone.is_naive(paid_at):
                        raise CommandError("زمان واقعی پرداخت باید offset صریح داشته باشد.")
                evidence = RemoteEvidence(options["remote_state"], options["support_reference"], paid_at)
            elif options.get("gateway_paid_at") or options.get("support_reference"):
                raise CommandError("شاهد زمان یا پشتیبانی بدون وضعیت دقیق قابل اعمال نیست.")
            with protected_backup(options.get("backup")):
                result = cancel_payment(payment, actor=actor, evidence=evidence)
            self.stdout.write(json.dumps({"payment_id": result.pk, "order_id": result.order_id,
                "local_status": result.status, "reservation_hold": result.reservation_hold,
                "provider_cancellation_confirmed": bool(result.metadata.get("torobpay", {}).get("cancellation_confirmed")),
                "customer_bank_credit_confirmed": False}, ensure_ascii=False, indent=2))
        except (User.DoesNotExist, Payment.DoesNotExist, PaymentError, PaymentGatewayError, ValueError) as error:
            raise CommandError("لغو پرداخت کامل یا قابل تأیید نیست؛ وضعیت و شواهد را با پشتیبانی بررسی کنید.") from error
