"""One production tick: real reconciliation command, then due expiration."""

import io
import json
import logging
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from payments.maintenance import (
    MaintenanceLeaseLost, claim_maintenance_job, expiration_due, maintenance_status,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "وضعیت وظیفهٔ نگهداری پرداخت؛ apply چرخهٔ عادی بازیابی و انقضای دوره‌ای را اجرا می‌کند، بدون بازپرداخت تازه."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--status", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--max-runtime-seconds", type=int, default=45)

    def handle(self, *args, **options):
        if not options["apply"]:
            self.stdout.write(json.dumps(maintenance_status(), ensure_ascii=False, indent=2))
            return
        if not 1 <= options["limit"] <= 500 or not 1 <= options["max_runtime_seconds"] <= 240:
            raise CommandError("limit باید بین ۱ تا ۵۰۰ و مهلت اجرای نرم بین ۱ تا ۲۴۰ ثانیه باشد.")
        job, lease = claim_maintenance_job()
        if lease is None:
            logger.info("Payment maintenance skipped: shared lease active")
            self.stdout.write("اجرای دیگری فعال است؛ عملیات تکراری آغاز نشد.")
            return
        started = time.monotonic()
        deadline = started + options["max_runtime_seconds"]
        run_expiration = expiration_due(job.last_expiration_at)
        errors, summary, expiration_completed = [], {}, False
        try:
            reconciliation_output = io.StringIO()
            reconciliation_budget = max(1, options["max_runtime_seconds"] * 2 // 3) if run_expiration else options["max_runtime_seconds"]
            try:
                call_command("reconcile_torobpay_payments", apply=True, limit=options["limit"],
                    max_runtime_seconds=reconciliation_budget, maintenance_heartbeat=lease.pulse, stdout=reconciliation_output)
            except CommandError:
                errors.append("reconciliation_failed")
                logger.error("Payment maintenance phase=reconciliation result=failed lease_id=%s", lease.lease_id)
            if reconciliation_output.getvalue():
                self.stdout.write(reconciliation_output.getvalue().rstrip())
                summary["reconciliation"] = json.loads(reconciliation_output.getvalue())
            lease.pulse()
            remaining = int(deadline - time.monotonic())
            if run_expiration and remaining >= 1:
                try:
                    call_command("expire_pending_orders", limit=options["limit"], max_runtime_seconds=remaining,
                                 maintenance_heartbeat=lease.pulse, stdout=self.stdout)
                    expiration_completed = True
                except CommandError:
                    errors.append("expiration_failed")
                    logger.error("Payment maintenance phase=expiration result=failed lease_id=%s", lease.lease_id)
            summary.update(expiration_completed=expiration_completed,
                           budget_exhausted=time.monotonic() >= deadline, errors=errors)
            lease.finish(success=not errors, summary=summary, error=",".join(errors), expiration_completed=expiration_completed)
        except MaintenanceLeaseLost:
            logger.error("Payment maintenance stopped: shared lease lost lease_id=%s", lease.lease_id)
            raise CommandError("اجرای قبلی متوقف شد؛ مالک جدید باید بازیابی را ادامه دهد.") from None
        except Exception as error:
            logger.error("Payment maintenance failed lease_id=%s error_type=%s", lease.lease_id, type(error).__name__)
            try:
                lease.finish(success=False, summary={"errors": ["unexpected_failure"]}, error="unexpected_failure")
            except MaintenanceLeaseLost:
                pass
            raise CommandError("اجرای نگهداری پرداخت کامل نشد؛ لاگ و وضعیت وظیفه را بررسی کنید.") from None
        logger.log(logging.ERROR if errors else logging.INFO,
            "Payment maintenance finished lease_id=%s success=%s expiration_completed=%s elapsed_seconds=%.1f",
            lease.lease_id, not errors, expiration_completed, time.monotonic() - started)
        if errors:
            raise CommandError("بخشی از نگهداری پرداخت کامل نشد؛ وضعیت نامطمئن حفظ شد و اجرای بعدی قابل بازیابی است.")
