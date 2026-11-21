"""Production ticks, fencing, fair batches and real host-adapter execution."""

import configparser
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from cart.models import Cart, CartItem
from orders.models import Order
from orders.services import create_order, expire_due_orders
from payments.maintenance import JOB_NAME, MaintenanceLeaseLost, claim_maintenance_job
from payments.models import Payment, PaymentMaintenanceJob
from payments.test_torobpay import CPGTestCase
from payments.torobpay_services import _begin_step, _claim, reconcile_payment


class PaymentMaintenanceTests(CPGTestCase):
    def second_order(self):
        cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        return create_order(customer=self.customer, cart=cart, address=self.address)

    def tick(self, **options):
        output = io.StringIO()
        call_command("run_payment_maintenance", apply=True, stdout=output, **options)
        return output.getvalue()

    def test_tick_runs_correct_commands_in_order_and_keeps_accepted_receipt(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        expired_order = self.second_order()
        Order.objects.filter(pk=expired_order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch("payments.management.commands.run_payment_maintenance.call_command", wraps=call_command) as commands:
            output = self.tick()
        self.assertEqual([call.args[0] for call in commands.call_args_list], ["reconcile_torobpay_payments", "expire_pending_orders"])
        payment.refresh_from_db()
        first = payment.settle_confirmed_at
        self.assertTrue(payment.is_successful)
        self.assertIsNotNone(first)
        self.assertIsNone(payment.gateway_paid_at)
        expired_order.refresh_from_db()
        self.assertEqual(expired_order.payment_status, "expired")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        self.assertNotIn(payment.provider_token, output)
        self.assertNotIn(self.jwt, output)
        self.tick()
        with patch("payments.maintenance.timezone.now", return_value=timezone.now() + timedelta(minutes=6)):
            self.tick()
        payment.refresh_from_db()
        self.assertEqual(payment.settle_confirmed_at, first)
        self.assertTrue(payment.is_successful)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_expiration_runs_on_initial_tick_and_each_five_minute_boundary(self):
        initial = (timezone.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        with patch("payments.maintenance.timezone.now", return_value=initial):
            self.tick()
        with patch("payments.management.commands.run_payment_maintenance.call_command", wraps=call_command) as commands:
            with patch("payments.maintenance.timezone.now", return_value=initial + timedelta(minutes=4, seconds=59)):
                self.tick()
            self.assertEqual([call.args[0] for call in commands.call_args_list], ["reconcile_torobpay_payments"])
            commands.reset_mock()
            with patch("payments.maintenance.timezone.now", return_value=initial + timedelta(minutes=5)):
                self.tick()
            self.assertEqual([call.args[0] for call in commands.call_args_list], ["reconcile_torobpay_payments", "expire_pending_orders"])

    def test_expiration_completion_seconds_do_not_delay_the_next_five_minute_slot(self):
        from payments.maintenance import expiration_due
        boundary = (timezone.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self.assertFalse(expiration_due(boundary + timedelta(seconds=20), now=boundary + timedelta(minutes=4, seconds=59)))
        self.assertTrue(expiration_due(boundary + timedelta(seconds=20), now=boundary + timedelta(minutes=5)))

    def test_status_is_read_only_and_does_not_create_a_scheduler_record(self):
        output = io.StringIO()
        call_command("run_payment_maintenance", status=True, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["state"], "not_started")
        self.assertFalse(PaymentMaintenanceJob.objects.exists())
        self.http.assert_not_called()

    def test_admin_monitoring_cannot_save_stale_receipts_or_job_leases(self):
        payment = self.start()
        job, lease = claim_maintenance_job()
        self.client.force_login(self.staff())
        for name, pk in (("admin:payments_payment_change", payment.pk),
                         ("admin:payments_paymentmaintenancejob_change", job.pk)):
            url = reverse(name, args=[pk])
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertEqual(PaymentMaintenanceJob.objects.get(pk=job.pk).lease_id, lease.lease_id)
        payment.refresh_from_db()
        self.assertIsNone(payment.settle_confirmed_at)
        self.assertTrue(payment.reservation_hold)

    def test_active_global_lease_skips_all_commands(self):
        job, lease = claim_maintenance_job()
        with patch("payments.management.commands.run_payment_maintenance.call_command") as commands:
            self.tick()
        commands.assert_not_called()
        self.assertEqual(PaymentMaintenanceJob.objects.get(pk=JOB_NAME).lease_id, lease.lease_id)

    def test_expired_global_lease_is_reclaimed_and_old_worker_is_fenced(self):
        job, old = claim_maintenance_job()
        PaymentMaintenanceJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        job, new = claim_maintenance_job()
        self.assertNotEqual(old.lease_id, new.lease_id)
        with self.assertRaises(MaintenanceLeaseLost):
            old.pulse()
        with self.assertRaises(MaintenanceLeaseLost):
            old.finish(success=True, summary={})
        self.assertEqual(PaymentMaintenanceJob.objects.get(pk=job.pk).lease_id, new.lease_id)
        new.finish(success=True, summary={})

    def test_timeout_returns_nonzero_and_records_sanitized_failure_then_recovers(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        secret = "synthetic-never-log-timeout-detail"
        self.responses["status"] = [TimeoutError(secret)]
        output = io.StringIO()
        with self.assertLogs("payments", level="ERROR") as logs, self.assertRaises(CommandError):
            call_command("run_payment_maintenance", apply=True, stdout=output)
        job = PaymentMaintenanceJob.objects.get(pk=JOB_NAME)
        self.assertIsNone(job.last_success_at)
        self.assertEqual(job.last_error, "reconciliation_failed")
        self.assertIsNone(job.lease_id)
        self.assertIn(f"payment_id={payment.pk}", "\n".join(logs.output))
        for value in (secret, self.jwt, payment.provider_token):
            self.assertNotIn(value, output.getvalue() + "\n".join(logs.output))
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertTrue(payment.reservation_hold)
        self.assertIsNone(payment.settle_confirmed_at)
        self.tick()
        payment.refresh_from_db()
        self.assertTrue(payment.is_successful)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertIsNotNone(PaymentMaintenanceJob.objects.get(pk=JOB_NAME).last_success_at)

    def test_late_settle_goes_to_review_and_stock_is_not_released(self):
        payment = self.start()
        self.outcome(payment, "ONGOING")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.tick()
        self.assert_review(payment)
        self.assertIsNotNone(payment.settle_confirmed_at)
        self.assertEqual(PaymentMaintenanceJob.objects.get(pk=JOB_NAME).summary["reconciliation"]["stats"]["manual_review"], 1)
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_definitive_unpaid_status_expires_stock_only_once(self):
        payment = self.start()
        self.outcome(payment, "FAILED")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.tick()
        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertFalse(payment.reservation_hold)
        self.assertTrue(self.order.inventory_released)
        self.assertEqual(self.order.payment_status, "expired")
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_active_payment_lease_blocks_expiration_even_if_hold_flag_is_false(self):
        payment = self.start()
        _claim(payment, "reconcile")
        Payment.objects.filter(pk=payment.pk).update(reservation_hold=False)
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_orders(), 0)
        self.assertEqual(len(self.api_calls("status")), 0)
        self.order.refresh_from_db()
        self.assertFalse(self.order.inventory_released)

    def test_expired_payment_lease_recovers_via_status_without_duplicate_mutation(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_SETTLE")
        _, old = _claim(payment, "reconcile")
        _begin_step(old, "verify")  # A crashed worker may have already verified.
        Payment.objects.filter(pk=payment.pk).update(reconciliation_lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.tick()
        payment.refresh_from_db()
        self.assertTrue(payment.is_successful)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("settle")), 1)
        self.assertIsNone(payment.reconciliation_lease_expires_at)

    def test_fair_limit_does_not_starve_unexamined_receipts(self):
        from payments.services import create_payment_for_order
        first = self.start()
        other = self.second_order()
        second = create_payment_for_order(other, gateway_name="torobpay")
        first_output, second_output = io.StringIO(), io.StringIO()
        call_command("reconcile_torobpay_payments", apply=True, limit=1, stdout=first_output)
        call_command("reconcile_torobpay_payments", apply=True, limit=1, stdout=second_output)
        self.assertEqual(json.loads(first_output.getvalue())["payments"][0]["payment_id"], first.pk)
        self.assertEqual(json.loads(second_output.getvalue())["payments"][0]["payment_id"], second.pk)

    def test_budget_stops_new_attempts_and_leaves_them_for_next_tick(self):
        from payments.services import create_payment_for_order
        first = self.start()
        self.outcome(first, "ONGOING")
        second = create_payment_for_order(self.second_order(), gateway_name="torobpay")
        clock = [0]

        def slow(record):
            clock[0] = 2
            return self.server_response(record, "status")

        self.responses["status"] = [slow]
        output = io.StringIO()
        with patch("payments.management.commands.reconcile_torobpay_payments.time.monotonic", side_effect=lambda: clock[0]):
            call_command("reconcile_torobpay_payments", apply=True, max_runtime_seconds=1, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["stats"]["attempted"], 1)
        self.assertTrue(report["budget_exhausted"])
        self.assertIsNone(Payment.objects.get(pk=second.pk).reconciliation_attempted_at)

    def test_unexpected_expiration_error_has_nonzero_exit_without_sensitive_text(self):
        output = io.StringIO()
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch("orders.services._restore_stock", side_effect=RuntimeError("synthetic-private-error")), \
                self.assertLogs("orders.services", level="ERROR") as logs, self.assertRaises(CommandError):
            call_command("expire_pending_orders", stdout=output)
        self.assertIn(f"order_id={self.order.pk}", "\n".join(logs.output))
        self.assertNotIn("synthetic-private-error", output.getvalue() + "\n".join(logs.output))
        self.order.refresh_from_db()
        self.assertFalse(self.order.inventory_released)


class PaymentSchedulerAdapterTests(SimpleTestCase):
    root = Path(__file__).resolve().parent.parent

    def test_systemd_units_define_one_minute_timer_and_journal_failure_visibility(self):
        service, timer = configparser.ConfigParser(interpolation=None), configparser.ConfigParser(interpolation=None)
        service.read(self.root / "scripts/systemd/minikandoo-payments.service")
        timer.read(self.root / "scripts/systemd/minikandoo-payments.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* *:*:00")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["StandardError"], "journal")
        self.assertEqual(service["Service"]["TimeoutStartSec"], "330s")
        self.assertIn("run-payment-maintenance", service["Service"]["ExecStart"])

    def test_host_adapter_uses_existing_production_web_env_and_inside_container_timeout(self):
        with TemporaryDirectory(prefix="minikandoo-scheduler-adapter-qa-") as directory:
            root = Path(directory)
            env_file, trace = root / "synthetic-config.env", root / "trace.json"
            env_file.write_text("# isolated QA placeholders only\n")
            docker = root / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                "Path(os.environ['MINIKANDOO_QA_DOCKER_TRACE']).write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd()}))\n")
            docker.chmod(0o700)
            environment = {"PATH": str(root) + ":" + os.environ["PATH"], "ENV_FILE": str(env_file),
                           "MINIKANDOO_QA_DOCKER_TRACE": str(trace)}
            result = subprocess.run(["/bin/sh", str(self.root / "scripts/run-payment-maintenance")],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(trace.read_text())
            self.assertEqual(record["cwd"], str(self.root))
            self.assertEqual(record["args"], ["compose", "-f", "docker-compose.yml", "-f", "docker-compose.production.yml",
                "--env-file", str(env_file), "exec", "-T", "-e", "DJANGO_SETTINGS_MODULE=config.settings.production", "web",
                "timeout", "--signal=TERM", "--kill-after=10s", "300s", "python", "manage.py", "run_payment_maintenance",
                "--apply", "--limit", "100", "--max-runtime-seconds", "45"])

    def test_host_adapter_rejects_arbitrary_arguments_before_docker_execution(self):
        result = subprocess.run(["/bin/sh", str(self.root / "scripts/run-payment-maintenance"), "cancel_torobpay_payment"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
