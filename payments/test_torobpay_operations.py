"""Support commands, lost-operation recovery, settings and migration safety."""

from copy import deepcopy
from datetime import timedelta
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order, OrderItem
from payments.models import Payment
from payments.services import PaymentError, cancel_order_with_payment, create_payment_for_order
from payments.test_torobpay import CPGTestCase
from payments.torobpay_services import RemoteEvidence, cancel_payment, confirm_updated_transaction, reconcile_payment, update_payment


class CPGOperationalTests(CPGTestCase):
    def synthetic_backup(self):
        directory = self.enterContext(TemporaryDirectory(prefix="torobpay-synthetic-backup-"))
        path = Path(directory) / "synthetic.sql.gz"
        body = (f"-- MariaDB dump\n-- Host: localhost    Database: {connection.settings_dict['NAME']}\n"
                "CREATE TABLE `payments_payment` (id int);\nCREATE TABLE `orders_order` (id int);\n"
                "CREATE TABLE `products_productvariant` (id int);\n-- Dump completed on synthetic-test\n")
        path.write_bytes(gzip.compress(body.encode()))
        Path(str(path) + ".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")
        return path

    def test_reconciliation_command_default_is_read_only_and_redacted(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        before = Payment.objects.values().get(pk=payment.pk)
        output = io.StringIO()
        call_command("reconcile_torobpay_payments", payment_id=[payment.pk], stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertFalse(report["new_refund_requests"])
        self.assertEqual(Payment.objects.values().get(pk=payment.pk), before)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertNotIn(payment.provider_token, output.getvalue())
        self.assertNotIn(self.jwt, output.getvalue())

    def test_normal_reconciliation_apply_uses_callback_policy_without_refund(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        output = io.StringIO()
        call_command("reconcile_torobpay_payments", payment_id=[payment.pk], apply=True, stdout=output)
        payment.refresh_from_db()
        self.assertTrue(payment.is_successful)
        self.assertEqual(len(self.api_calls("cancel")), 0)
        self.assertEqual(len(self.api_calls("revert")), 0)

    def test_zibal_recovery_cannot_bypass_unresolved_credit_reservation(self):
        credit = self.start()
        zibal = Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total,
                                       authority="15966442233311")
        response = {"result": 100, "status": 1, "amount": int(self.order.total) * 10,
                    "orderId": self.order.order_number, "refNumber": "0012345",
                    "paidAt": timezone.now().isoformat()}
        output = io.StringIO()
        backup = self.synthetic_backup()
        with patch("payments.gateways.zibal.ZibalGateway._post", return_value=response) as http:
            with self.assertRaises(CommandError):
                call_command("reconcile_zibal_payments", order_number=self.order.order_number,
                             track_id=[zibal.authority], apply=True, backup=str(backup), stdout=output)
        report = json.loads(output.getvalue())
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["unresolved_provider_payments"], [credit.pk])
        self.assertEqual([call.args[0] for call in http.call_args_list], ["/v1/inquiry"])
        self.order.refresh_from_db()
        zibal.refresh_from_db()
        self.assertFalse(self.order.is_paid)
        self.assertTrue(Payment.objects.get(pk=credit.pk).reservation_hold)
        self.assertEqual(zibal.callback_count, 0)

    def test_internal_primary_authorization_rejects_held_provider_receipt(self):
        from payments.gateways.zibal import ZibalGateway
        from payments.services import _authorize_recovery_primary, receipt_history_fingerprint, recovery_receipt_snapshot
        credit = self.start()
        zibal = Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total,
                                       authority="15966442233311")
        result = ZibalGateway()._verified_result(zibal, {
            "status": 1, "amount": int(self.order.total) * 10, "orderId": self.order.order_number,
            "refNumber": "0012345", "paidAt": timezone.now().isoformat(),
        }, 100)
        history = [credit, zibal]
        backup = {"sha256_sidecar_check": "passed", "sha256": "a" * 64}
        plan = {"order_id": self.order.pk, "inspected_payment_ids": sorted(p.pk for p in history),
                "selected_track_ids": [zibal.authority], "primary": zibal.authority,
                "history_fingerprint": receipt_history_fingerprint(history),
                "receipts": {zibal.authority: recovery_receipt_snapshot(result)}, "backup_sha256": backup["sha256"]}
        with self.assertRaises(PaymentError):
            _authorize_recovery_primary(order=self.order, payments=history, verified={zibal.authority: result},
                                        selected_track_ids=[zibal.authority], primary=zibal, backup=backup, plan=plan)
        credit.reservation_hold = False
        self.assertNotEqual(receipt_history_fingerprint(history), plan["history_fingerprint"])

    def test_manual_cancel_requires_backup_before_any_refund_request(self):
        payment = self.pay(self.start())
        actor = self.staff()
        with self.assertRaises(CommandError):
            call_command("cancel_torobpay_payment", payment_id=payment.pk, actor_id=actor.pk, apply=True, stdout=io.StringIO())
        self.assertEqual(len(self.api_calls("cancel")), 0)
        self.assertFalse(Payment.objects.get(pk=payment.pk).metadata["torobpay"].get("cancellation_confirmed"))

    def test_manual_cancel_with_synthetic_verified_backup_is_idempotent(self):
        payment = self.pay(self.start())
        actor = self.staff()
        backup = self.synthetic_backup()
        for _ in range(2):
            output = io.StringIO()
            call_command("cancel_torobpay_payment", payment_id=payment.pk, actor_id=actor.pk,
                         apply=True, backup=str(backup), stdout=output)
            self.assertTrue(json.loads(output.getvalue())["provider_cancellation_confirmed"])
            self.assertNotIn(payment.provider_token, output.getvalue())
        self.assertEqual(len(self.api_calls("cancel")), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_generic_cancellation_service_uses_optional_provider_capability(self):
        payment = self.pay(self.start())
        order = cancel_order_with_payment(self.order, actor=self.customer)
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(len(self.api_calls("cancel")), 1)

    def test_paid_credit_order_cannot_be_cancelled_locally_without_provider_refund(self):
        from orders.services import cancel_order, transition_status, OrderError
        payment = self.pay(self.start())
        with self.assertRaises(OrderError):
            cancel_order(payment.order)
        with self.assertRaises(OrderError):
            transition_status(payment.order, "cancelled")
        self.assertEqual(len(self.api_calls("cancel")), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_cancel_definitive_rejection_preserves_original_successful_receipt(self):
        payment = self.pay(self.start())
        self.responses["cancel"] = [self.error(1053)]
        result = cancel_payment(payment, actor=self.customer)
        self.assertEqual(result.status, "successful")
        self.assertEqual(result.metadata["torobpay"]["active_operation"]["status"], "rejected")
        self.assertFalse(result.reservation_hold)
        self.assertFalse(result.metadata["torobpay"].get("cancellation_confirmed"))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_provider_discount_rejection_is_durable_and_never_repeated_as_update(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        item = self.order.items.get()
        actor = self.staff()
        self.responses["update"] = [self.error(1016)]
        result = update_payment(payment, quantities={item.pk: 1}, actor=actor)
        self.assertTrue(result.is_successful)
        self.assertTrue(result.metadata["torobpay"]["provider_discounted"])
        self.assertEqual(result.metadata["torobpay"]["active_operation"]["status"], "rejected")
        with self.assertRaises(PaymentError):
            update_payment(result, quantities={item.pk: 1}, actor=actor)
        self.assertEqual(len(self.api_calls("update")), 1)

    def test_unknown_update_cannot_start_a_cancel_or_other_financial_action(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        actor = self.staff()
        self.responses["update"] = [TimeoutError("synthetic")]
        result = update_payment(payment, quantities={self.order.items.get().pk: 1}, actor=actor)
        with self.assertRaises(PaymentError):
            cancel_payment(result, actor=actor)
        self.assertEqual(len(self.api_calls("cancel")), 0)

    def test_lost_updated_copy_identifier_needs_independent_evidence_and_no_second_post(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        item = self.order.items.get()
        actor = self.staff()
        def lost(record):
            self.server_response(record, "update")
            raise TimeoutError("synthetic")
        self.responses["update"] = [lost]
        result = update_payment(payment, quantities={item.pk: 1}, actor=actor)
        self.assertEqual(result.status, "manual_review")
        with self.assertRaises(PaymentError):
            confirm_updated_transaction(result, actor=actor, transaction_id="wrong", support_reference="SUP-synthetic")
        recovered = confirm_updated_transaction(result, actor=actor,
            transaction_id=self.remote[payment.provider_token]["transactionId"], support_reference="SUP-synthetic")
        self.assertTrue(recovered.is_successful)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, 550000)
        self.assertEqual(len(self.api_calls("update")), 1)
        self.assertEqual(recovered.transaction_id, str(payment.request_id))

    def test_late_settled_receipt_can_be_explicitly_refunded_with_staff_state_evidence(self):
        payment = self.start()
        self.outcome(payment, "ONGOING")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        reviewed = reconcile_payment(payment)
        self.assertEqual(reviewed.status, "manual_review")
        result = cancel_payment(reviewed, actor=self.staff(), evidence=RemoteEvidence("ONGOING", "SUP-synthetic"))
        self.assertEqual(result.status, "cancelled")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_eligibility_authorization_is_not_reused_for_another_order(self):
        from payments.gateways.torobpay import TorobPayGateway
        from cart.models import Cart, CartItem
        from orders.services import create_order
        gateway = TorobPayGateway()
        authorization = gateway.authorize_order(self.order)
        cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        other = create_order(customer=self.customer, cart=cart, address=self.address)
        create_payment_for_order(other, gateway_name="torobpay", authorization=authorization)
        self.assertEqual(len(self.api_calls("eligible")), 2)

    def test_same_amount_snapshot_change_during_create_requires_review(self):
        def changed(record):
            response = self.server_response(record, "token")
            Order.objects.filter(pk=self.order.pk).update(address_line="نشانی دیگر")
            return response
        self.responses["token"] = [changed]
        with self.assertRaises(PaymentError):
            self.start()
        payment = Payment.objects.get()
        self.assertEqual(payment.creation_status, "review")
        self.assertTrue(payment.provider_token)

    def test_auth_failure_before_create_is_a_definitive_no_token_request(self):
        from django.core.cache import caches
        from payments.gateways.torobpay import TorobPayGateway
        authorization = TorobPayGateway().authorize_order(self.order)
        caches["default"].clear()
        self.responses["oauth"] = [TimeoutError("synthetic")]
        with self.assertRaises(PaymentError):
            create_payment_for_order(self.order, gateway_name="torobpay", authorization=authorization)
        attempt = Payment.objects.get()
        self.assertEqual(attempt.creation_status, "rejected")
        self.assertFalse(attempt.reservation_hold)
        self.assertEqual(len(self.api_calls("token")), 0)

    def test_admin_never_renders_provider_token_or_hosted_url(self):
        payment = self.start()
        self.client.force_login(self.staff())
        response = self.client.get(reverse("admin:payments_payment_change", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, payment.provider_token)
        self.assertNotContains(response, self.jwt)
        self.assertNotContains(response, payment.metadata["redirect_url"])


class CPGProductionSettingsTests(SimpleTestCase):
    def settings_result(self, **changes):
        environment = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "LANG") if key in os.environ}
        environment.update(SECRET_KEY="synthetic-production-settings-only-" + "x" * 60,
            DATABASE_PASSWORD="synthetic-database-password", ALLOWED_HOSTS="merchant.example",
            SITE_URL="https://merchant.example", API_URL="https://api.merchant.example",
            PAYMENT_GATEWAY="zibal", PAYMENT_GATEWAYS="", ZIBAL_MERCHANT="synthetic-real-format-merchant")
        environment.update(changes)
        return subprocess.run([sys.executable, "-c", "import config.settings.production; print('configured')"],
                              env=environment, capture_output=True, text=True)

    def test_disabled_torobpay_does_not_require_its_credentials(self):
        self.assertEqual(self.settings_result().returncode, 0)

    def test_enabled_torobpay_requires_all_four_merchant_credentials(self):
        result = self.settings_result(PAYMENT_GATEWAYS="zibal,torobpay")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOROBPAY_CLIENT_ID", result.stderr)
        valid = dict(TOROBPAY_CLIENT_ID="synthetic-id", TOROBPAY_CLIENT_SECRET="synthetic-secret",
                     TOROBPAY_USERNAME="synthetic-user", TOROBPAY_PASSWORD="synthetic-password")
        self.assertEqual(self.settings_result(PAYMENT_GATEWAYS="zibal,torobpay", **valid).returncode, 0)
        valid["TOROBPAY_CLIENT_SECRET"] = ""
        self.assertNotEqual(self.settings_result(PAYMENT_GATEWAYS="zibal,torobpay", **valid).returncode, 0)

    def test_bad_gateway_configuration_fails_and_legacy_class_path_still_works(self):
        self.assertNotEqual(self.settings_result(PAYMENT_GATEWAY="unknown").returncode, 0)
        self.assertNotEqual(self.settings_result(PAYMENT_GATEWAYS="zibal,zibal").returncode, 0)
        self.assertEqual(self.settings_result(PAYMENT_GATEWAY="payments.gateways.zibal.ZibalGateway").returncode, 0)

    def test_production_rejects_insecure_api_or_callback_url(self):
        credentials = dict(PAYMENT_GATEWAYS="zibal,torobpay", TOROBPAY_CLIENT_ID="synthetic-id",
                           TOROBPAY_CLIENT_SECRET="synthetic-secret", TOROBPAY_USERNAME="synthetic-user", TOROBPAY_PASSWORD="synthetic-password")
        self.assertNotEqual(self.settings_result(TOROBPAY_BASE_URL="http://cpg.torobpay.com", **credentials).returncode, 0)
        self.assertNotEqual(self.settings_result(SITE_URL="http://merchant.example", **credentials).returncode, 0)


class CPGMigrationTests(TransactionTestCase):
    previous = [("orders", "0002_order_inventory_released_alter_order_payment_status"),
                ("payments", "0004_payment_creation_status_payment_request_id_and_more")]

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

    def test_forward_and_reverse_preserve_existing_zibal_receipt_values(self):
        apps = self.old_apps()
        user = apps.get_model("accounts", "User").objects.create(phone_number="09120007777")
        order = apps.get_model("orders", "Order").objects.create(customer=user, order_number="ORD-MIGRATION-CPG")
        observed = timezone.now()
        payment = apps.get_model("payments", "Payment").objects.create(order=order, gateway="zibal", amount=500000,
            authority="123456789", transaction_id="123456789", reference_id="0012345", status="successful",
            paid_at=observed, verified_at=observed, metadata={"verification": {"paidAt": observed.isoformat()}})
        self.restore()
        current = Payment.objects.get(pk=payment.pk)
        for field in ("amount", "authority", "transaction_id", "reference_id", "status", "paid_at", "verified_at", "metadata"):
            self.assertEqual(getattr(current, field), getattr(payment, field))
        self.assertEqual(current.provider_token, "")
        self.assertFalse(current.reservation_hold)
        old = self.old_apps().get_model("payments", "Payment").objects.get(pk=payment.pk)
        self.assertEqual(old.reference_id, "0012345")
        self.assertEqual(old.metadata, payment.metadata)

    def test_reverse_guard_keeps_torobpay_identifiers_before_schema_removal(self):
        from accounts.models import User
        user = User.objects.create_user(phone_number="09120008888")
        order = Order.objects.create(customer=user, order_number="ORD-CPG-REVERSE")
        payment = Payment.objects.create(order=order, gateway="torobpay", amount=500000, provider_token="synthetic-token",
                                         authority=hashlib.sha256(b"synthetic-token").hexdigest(), reservation_hold=True)
        with self.assertRaises(IrreversibleError):
            self.old_apps()
        payment.refresh_from_db()
        self.assertEqual(payment.provider_token, "synthetic-token")
        self.assertTrue(payment.reservation_hold)
