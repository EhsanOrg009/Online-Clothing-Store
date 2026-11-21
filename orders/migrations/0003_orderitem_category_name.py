from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0002_order_inventory_released_alter_order_payment_status")]
    operations = [migrations.AddField(
        model_name="orderitem", name="category_name",
        field=models.CharField(blank=True, default="", max_length=120, verbose_name="دسته‌بندی هنگام سفارش"),
    )]
    # No backfill from today's mutable catalog into historical snapshots.
