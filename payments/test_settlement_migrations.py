"""Additive timestamp/lease schema; old financial records remain untouched."""

from datetime import timedelta
import hashlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import TransactionTestCase
from django.utils import timezone

from payments.models import Payment, PaymentMaintenanceJob


class SettlementMigrationTests(TransactionTestCase):
    previous = [("payments", "0005_payment_provider_token_payment_reservation_hold"),
                ("orders", "0003_orderitem_category_name")]

    def setUp(self):
        super().setUp()
        self.leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self.addCleanup(self.restore)

    def restore(self):
        MigrationExecutor(connection).migrate(self.leaves)

    def old_apps(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.previous)
        return executor.loader.project_state(self.previous).apps

    def test_existing_torobpay_and_zibal_records_are_preserved_without_timestamp_guess(self):
        apps = self.old_apps()
        user = apps.get_model("accounts", "User").objects.create(phone_number="09120006661")
        order = apps.get_model("orders", "Order").objects.create(customer=user, order_number="ORD-OLD-SETTLE")
        previous_payment = apps.get_model("payments", "Payment")
        recorded = timezone.now()
        zibal = previous_payment.objects.create(order=order, gateway="zibal", authority="15678", amount=550000,
            transaction_id="15678", reference_id="0012345", status="successful", paid_at=recorded,
            gateway_paid_at=recorded - timedelta(seconds=3), verified_at=recorded,
            metadata={"verification": {"paidAt": recorded.isoformat()}})
        token = "synthetic-legacy-token"
        torobpay = previous_payment.objects.create(order=order, gateway="torobpay", amount=550000,
            authority=hashlib.sha256(token.encode()).hexdigest(), provider_token=token, reservation_hold=True,
            status="manual_review", metadata={"torobpay": {"settlement_evidence": {
                "observed_at": recorded.isoformat(), "amount_rial": 5500000, "transaction_id": "legacy-txn"}}})
        originals = {p.pk: previous_payment.objects.values().get(pk=p.pk) for p in (zibal, torobpay)}
        self.restore()
        for pk, original in originals.items():
            current = Payment.objects.get(pk=pk)
            for name, value in original.items():
                self.assertEqual(getattr(current, name), value, name)
            self.assertIsNone(current.settle_confirmed_at)
            self.assertIsNone(current.reconciliation_lease_expires_at)
        self.assertFalse(PaymentMaintenanceJob.objects.exists())
        with self.assertRaises(IrreversibleError):
            self.old_apps()
        self.assertEqual(Payment.objects.get(pk=torobpay.pk).provider_token, token)

    def test_reverse_is_safe_before_any_settlement_or_scheduler_evidence(self):
        apps = self.old_apps()
        user = apps.get_model("accounts", "User").objects.create(phone_number="09120006662")
        order = apps.get_model("orders", "Order").objects.create(customer=user, order_number="ORD-REVERSE-SETTLE")
        old = apps.get_model("payments", "Payment").objects.create(order=order, gateway="zibal", amount=10,
                                                                    authority="001234", metadata={"unchanged": True})
        self.restore()
        reversed_payment = self.old_apps().get_model("payments", "Payment").objects.get(pk=old.pk)
        self.assertEqual(reversed_payment.authority, "001234")
        self.assertEqual(reversed_payment.metadata, {"unchanged": True})
