"""Forward/backward schema checks only in Django's isolated test database."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class PaymentRecoveryMigrationTests(TransactionTestCase):
    old_targets = [("orders", "0001_initial"), ("payments", "0002_payment_needs_refund")]
    new_targets = [
        ("orders", "0002_order_inventory_released_alter_order_payment_status"),
        ("payments", "0003_payment_gateway_paid_at_alter_payment_needs_refund_and_more"),
    ]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.leaves = executor.loader.graph.leaf_nodes()
        executor.migrate(self.old_targets)
        self.addCleanup(self.restore_schema)
        apps = executor.loader.project_state(self.old_targets).apps
        User = apps.get_model("accounts", "User")
        Order = apps.get_model("orders", "Order")
        Payment = apps.get_model("payments", "Payment")
        user = User.objects.create(phone_number="09120007777")
        self.recorded_at = timezone.now()
        self.raw_time = "2026-11-14T10:28:00"
        self.cancelled = Order.objects.create(customer=user, order_number="ORD-TEST-CANCELLED", status="cancelled")
        self.active = Order.objects.create(customer=user, order_number="ORD-TEST-ACTIVE", status="pending_payment")
        self.paid = Order.objects.create(
            customer=user, order_number="ORD-TEST-PAID", status="paid",
            payment_status="successful", paid_at=self.recorded_at,
        )
        self.payment = Payment.objects.create(
            order=self.paid, gateway="zibal", amount=500000, authority="15966442233311",
            status="successful", paid_at=self.recorded_at, verified_at=self.recorded_at,
            reference_id="0012345", metadata={"verification": {"paidAt": self.raw_time}},
        )

    def restore_schema(self):
        MigrationExecutor(connection).migrate(self.leaves)

    def test_existing_receipts_and_local_timestamps_remain_unchanged(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.new_targets)
        apps = executor.loader.project_state(self.new_targets).apps
        payment = apps.get_model("payments", "Payment").objects.get(pk=self.payment.pk)
        order = apps.get_model("orders", "Order").objects.get(pk=self.paid.pk)
        self.assertIsNone(payment.gateway_paid_at)
        self.assertEqual(payment.paid_at, self.recorded_at)
        self.assertEqual(payment.verified_at, self.recorded_at)
        self.assertEqual(order.paid_at, self.recorded_at)
        self.assertEqual(payment.reference_id, "0012345")
        self.assertEqual(payment.metadata["verification"]["paidAt"], self.raw_time)
        self.assertEqual(payment.status, "successful")

    def test_legacy_cancellations_are_flagged_without_restoring_stock(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.new_targets)
        Order = executor.loader.project_state(self.new_targets).apps.get_model("orders", "Order")
        cancelled = Order.objects.get(pk=self.cancelled.pk)
        self.assertTrue(cancelled.inventory_released)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertFalse(Order.objects.get(pk=self.active.pk).inventory_released)
        self.assertFalse(Order.objects.get(pk=self.paid.pk).inventory_released)

    def test_schema_is_reversible_without_rewriting_legacy_financial_records(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.new_targets)
        executor = MigrationExecutor(connection)
        executor.migrate(self.old_targets)
        Payment = executor.loader.project_state(self.old_targets).apps.get_model("payments", "Payment")
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(payment.reference_id, "0012345")
        self.assertEqual(payment.status, "successful")
        self.assertEqual(payment.paid_at, self.recorded_at)
