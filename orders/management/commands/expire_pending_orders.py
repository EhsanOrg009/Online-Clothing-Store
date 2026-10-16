"""
لغو سفارش‌های پرداخت‌نشده منقضی و آزادسازی موجودی رزروشده.

اجرا (مثلاً هر ۵ دقیقه با cron):
    uv run python manage.py expire_pending_orders

نمونه Crontab:
    */5 * * * * cd /path/to/project && uv run python manage.py expire_pending_orders
"""

from django.core.management.base import BaseCommand

from orders.services import expire_due_orders


class Command(BaseCommand):
    help = "سفارش‌های در انتظار پرداخت که مهلت آن‌ها گذشته را لغو و موجودی را آزاد می‌کند."

    def handle(self, *args, **options):
        expired = expire_due_orders()
        if expired:
            self.stdout.write(
                self.style.SUCCESS(f"{expired} سفارش منقضی لغو و موجودی آزاد شد.")
            )
        else:
            self.stdout.write("سفارش منقضی‌ای برای لغو وجود نداشت.")
