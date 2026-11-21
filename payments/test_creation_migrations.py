"""Creation schema preserves receipts and refuses destructive evidence rollback."""

from uuid import uuid4

from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from orders.choices import PaymentStatus
from payments.models import Payment, PaymentCreationStatus
from payments.tests import PaymentFixtureMixin


class PaymentCreationMigrationTests(PaymentFixtureMixin, TransactionTestCase):
    previous = ("payments", "0003_payment_gateway_paid_at_alter_payment_needs_refund_and_more")
    latest = ("payments", "0004_payment_creation_status_payment_request_id_and_more")

    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        MigrationExecutor(connection).migrate(self.leaves)

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def test_legacy_receipt_survives_reverse_and_forward_without_invented_request_id(self):
        observed = timezone.now()
        payment = Payment.objects.create(
            order=self.order, gateway="zibal", authority="123456789", amount=self.order.total,
            status=PaymentStatus.MANUAL_REVIEW, transaction_id="123456789", reference_id="0012345",
            paid_at=observed, verified_at=observed, gateway_paid_at=observed,
            metadata={"verification": {"status": 1, "paidAt": observed.isoformat()}},
        )
        self.migrate(self.previous)
        apps = self.migrate(self.latest)
        migrated = apps.get_model("payments", "Payment").objects.get(pk=payment.pk)
        self.assertEqual(migrated.authority, "123456789")
        self.assertEqual(migrated.reference_id, "0012345")
        self.assertEqual(migrated.paid_at, observed)
        self.assertEqual(migrated.gateway_paid_at, observed)
        self.assertEqual(migrated.metadata, payment.metadata)
        self.assertEqual(migrated.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(migrated.creation_status, PaymentCreationStatus.READY)
        self.assertIsNone(migrated.request_id)

    def test_reverse_refuses_to_remove_new_request_evidence_before_any_schema_change(self):
        payment = Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total,
                                         request_id=uuid4(), creation_status=PaymentCreationStatus.UNCERTAIN)
        with self.assertRaises(IrreversibleError):
            self.migrate(self.previous)
        # Later additive migrations may already have reversed when 0004's own
        # guard refuses removal. Inspect the evidence protected by 0004 only.
        payment.refresh_from_db(fields=["authority", "request_id", "creation_status"])
        self.assertIsNone(payment.authority)
        self.assertIsNotNone(payment.request_id)
        self.assertEqual(payment.creation_status, PaymentCreationStatus.UNCERTAIN)

    def test_independent_unbound_attempts_have_unique_local_ids_and_nullable_remote_ids(self):
        first = Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total, request_id=uuid4())
        second = Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total, request_id=uuid4())
        self.assertIsNone(first.authority)
        self.assertIsNone(second.authority)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total, request_id=first.request_id)
        self.assertEqual(self.order.payments.count(), 2)
