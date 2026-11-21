"""
لغو سفارش‌های پرداخت‌نشده منقضی و آزادسازی موجودی رزروشده.

اجرا مستقل برای عملیات بررسی‌شده:
    uv run python manage.py expire_pending_orders

زمان‌بندی اصلی تولید با minikandoo-payments.timer و run_payment_maintenance
انجام می‌شود؛ Cron موازی برای همین فرمان نصب نشود.
"""

from django.core.management.base import BaseCommand, CommandError

from orders.services import OrderError, expire_due_orders


class Command(BaseCommand):
    help = "سفارش‌های در انتظار پرداخت که مهلت آن‌ها گذشته را لغو و موجودی را آزاد می‌کند."
    stealth_options = ("maintenance_heartbeat",)

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int)
        parser.add_argument("--max-runtime-seconds", type=int)

    def handle(self, *args, **options):
        limit, budget = options.get("limit"), options.get("max_runtime_seconds")
        if limit is not None and not 1 <= limit <= 500:
            raise CommandError("limit باید بین ۱ تا ۵۰۰ باشد.")
        if budget is not None and not 1 <= budget <= 240:
            raise CommandError("مهلت اجرای نرم باید بین ۱ تا ۲۴۰ ثانیه باشد.")
        try:
            expired = expire_due_orders(limit=limit, max_runtime_seconds=budget,
                heartbeat=options.get("maintenance_heartbeat"), raise_on_error=True)
        except OrderError as error:
            raise CommandError(str(error)) from None
        if expired:
            self.stdout.write(
                self.style.SUCCESS(f"{expired} سفارش منقضی لغو و موجودی آزاد شد.")
            )
        else:
            self.stdout.write("سفارش منقضی‌ای برای لغو وجود نداشت.")
