from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def protect_provider_evidence(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    if Payment.objects.using(schema_editor.connection.alias).filter(
        models.Q(gateway="torobpay") | models.Q(reservation_hold=True) | ~models.Q(provider_token="")
    ).exists():
        raise IrreversibleError("Provider tokens/reconciliation evidence exists; rollback requires a reviewed backup, not deletion of receipts.")


class Migration(migrations.Migration):
    dependencies = [("payments", "0004_payment_creation_status_payment_request_id_and_more"),
                    ("orders", "0003_orderitem_category_name")]
    operations = [
        migrations.AddField(model_name="payment", name="provider_token", field=models.TextField(
            blank=True, default="", editable=False, verbose_name="شناسهٔ محرمانهٔ ارائه‌دهنده",
            help_text="فقط برای تماس سروری؛ در فهرست، گزارش و صفحات مشتری نمایش داده نمی‌شود.",
        )),
        migrations.AddField(model_name="payment", name="reservation_hold", field=models.BooleanField(
            db_index=True, default=False, editable=False, verbose_name="رزرو منتظر تعیین وضعیت درگاه",
            help_text="انقضا و لغو تا تأیید وضعیت نهایی درگاه موجودی را آزاد نمی‌کنند.",
        )),
        migrations.RunPython(migrations.RunPython.noop, protect_provider_evidence),
    ]
