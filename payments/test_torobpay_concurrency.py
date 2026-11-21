"""Real Order/Payment locks on isolated MariaDB; provider HTTP remains mocked."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
from threading import Event

from django.db import close_old_connections, connection, transaction
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from orders.models import Order
from orders.services import expire_due_orders
from payments.models import Payment, PaymentMaintenanceJob
from payments.services import PaymentError, create_payment_for_order, process_callback
from payments.tests import PaymentFixtureMixin
from payments.torobpay_services import _begin_step, _claim, reconcile_payment
from unittest.mock import patch
from payments.torobpay_test_support import CPGHTTPStubMixin, TOROBPAY_TEST_SETTINGS


@skipUnlessDBFeature("has_select_for_update")
@override_settings(**TOROBPAY_TEST_SETTINGS)
class CPGConcurrencyTests(PaymentFixtureMixin, CPGHTTPStubMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.assert_unlocked_http = True

    def worker(self, action):
        close_old_connections()
        try:
            return action()
        finally:
            close_old_connections()

    def start(self):
        return create_payment_for_order(self.order, gateway_name="torobpay")

    def test_create_wait_does_not_hold_order_or_payment_locks(self):
        started, release = Event(), Event()
        def slow(record):
            response = self.server_response(record, "token")
            started.set()
            self.assertTrue(release.wait(10))
            return response
        self.responses["token"] = [slow]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.worker, self.start)
            try:
                self.assertTrue(started.wait(10))
                attempt = self.order.payments.get()
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
                    Payment.objects.select_for_update(nowait=True).get(pk=attempt.pk)
            finally:
                release.set()
            self.assertEqual(future.result(20).creation_status, "ready")

    def test_concurrent_create_submissions_send_one_token_request(self):
        started, release = Event(), Event()
        def slow(record):
            response = self.server_response(record, "token")
            started.set()
            self.assertTrue(release.wait(10))
            return response
        self.responses["token"] = [slow]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.worker, self.start)
            try:
                self.assertTrue(started.wait(10))
                second = pool.submit(self.worker, self.start)
                with self.assertRaises(PaymentError):
                    second.result(10)
            finally:
                release.set()
            first.result(20)
        self.assertEqual(len(self.api_calls("token")), 1)

    def test_duplicate_callback_during_verify_does_not_issue_second_verify_or_settle(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        started, release = Event(), Event()
        def slow(record):
            response = self.server_response(record, "verify")
            started.set()
            self.assertTrue(release.wait(10))
            return response
        self.responses["verify"] = [slow]
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.worker, lambda: process_callback(payment, self.callback_data(payment)))
            try:
                self.assertTrue(started.wait(10))
                duplicate = process_callback(payment, self.callback_data(payment))
                self.assertEqual(duplicate.status, "pending")
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
                    Payment.objects.select_for_update(nowait=True).get(pk=payment.pk)
            finally:
                release.set()
            self.assertTrue(first.result(20).is_successful)
        payment.refresh_from_db()
        self.assertEqual(payment.callback_count, 2)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 1)

    def test_stale_http_response_cannot_downgrade_newer_reconciled_receipt(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        started, release = Event(), Event()
        def slow(record):
            response = self.server_response(record, "verify")
            started.set()
            self.assertTrue(release.wait(10))
            return response
        self.responses["verify"] = [slow]
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.worker, lambda: process_callback(payment, self.callback_data(payment)))
            try:
                self.assertTrue(started.wait(10))
                current = Payment.objects.get(pk=payment.pk)
                current.metadata["torobpay"]["active_operation"]["started_at"] = (timezone.now() - timedelta(minutes=5)).isoformat()
                current.reconciliation_lease_expires_at = timezone.now() - timedelta(seconds=1)
                current.save(update_fields=["metadata", "reconciliation_lease_expires_at"])
                newer = reconcile_payment(current)
                self.assertTrue(newer.is_successful)
            finally:
                release.set()
            self.assertTrue(first.result(20).is_successful)
        current = Payment.objects.get(pk=payment.pk)
        self.assertTrue(current.is_successful)
        self.assertFalse(current.reservation_hold)
        self.assertTrue(current.metadata["torobpay"]["stale_responses"])
        self.assertEqual(len(self.api_calls("settle")), 1)

    def test_expiration_during_create_keeps_unknown_remote_reservation(self):
        started, release = Event(), Event()
        def slow(record):
            response = self.server_response(record, "token")
            started.set()
            self.assertTrue(release.wait(10))
            return response
        self.responses["token"] = [slow]
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.worker, self.start)
            try:
                self.assertTrue(started.wait(10))
                Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
                self.assertEqual(expire_due_orders(), 0)
            finally:
                release.set()
            with self.assertRaises(PaymentError):
                first.result(20)
        self.order.refresh_from_db()
        self.assertFalse(self.order.inventory_released)
        self.assertFalse(self.order.is_paid)
        self.assertTrue(Payment.objects.get().provider_token)

    def test_in_time_observation_survives_real_order_lock_race_with_expiration(self):
        from payments.torobpay_services import _end_step
        deadline = timezone.now() + timedelta(seconds=10)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        payment = self.start()
        self.outcome(payment, "ONGOING")
        observed, write_started, release = Event(), Event(), Event()
        clock, times = [deadline - timedelta(seconds=1)], []

        def delayed(*args, **kwargs):
            times.append(kwargs["observed_at"])
            observed.set()
            self.assertTrue(release.wait(10))
            write_started.set()
            return _end_step(*args, **kwargs)

        with patch("payments.torobpay_services.timezone.now", side_effect=lambda: clock[0]), \
                patch("payments.torobpay_services._end_step", side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.worker, lambda: reconcile_payment(payment))
            try:
                self.assertTrue(observed.wait(10))
                with transaction.atomic():
                    Order.objects.select_for_update().get(pk=self.order.pk)
                    # This deliberately delays DB application after the API
                    # response has been received, validated and timestamped.
                    clock[0] = deadline + timedelta(seconds=1)
                    release.set()
                    self.assertTrue(write_started.wait(10))
                    expiration = pool.submit(self.worker, expire_due_orders)
                result = first.result(20)
                self.assertEqual(expiration.result(20), 0)
            finally:
                release.set()
        self.assertTrue(result.is_successful)
        self.assertEqual(result.settle_confirmed_at, times[0])
        self.assertEqual(times[0], deadline - timedelta(seconds=1))
        self.assertIsNone(result.gateway_paid_at)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.assertFalse(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        self.assertEqual(len(self.api_calls("status")), 1)

    def test_two_scheduler_invocations_do_not_process_payment_twice(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        started, release = Event(), Event()

        def slow(record):
            response = self.server_response(record, "status")
            started.set()
            self.assertTrue(release.wait(10))
            return response

        def tick():
            output = io.StringIO()
            call_command("run_payment_maintenance", apply=True, stdout=output)
            return output.getvalue()

        self.responses["status"] = [slow]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.worker, tick)
            try:
                self.assertTrue(started.wait(10))
                second = pool.submit(self.worker, tick)
                self.assertIn("اجرای دیگری فعال", second.result(10))
                self.assertEqual(len(self.api_calls("status")), 1)
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
                    Payment.objects.select_for_update(nowait=True).get(pk=payment.pk)
            finally:
                release.set()
            first.result(20)
        payment.refresh_from_db()
        self.assertTrue(payment.is_successful)
        first_time = payment.settle_confirmed_at
        call_command("run_payment_maintenance", apply=True, stdout=io.StringIO())
        payment.refresh_from_db()
        self.assertEqual(payment.settle_confirmed_at, first_time)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 1)
        self.assertIsNotNone(PaymentMaintenanceJob.objects.get().last_success_at)

    def test_expiration_reclaims_crashed_read_lease_only_after_definitive_unpaid_status(self):
        payment = self.start()
        _, lease = _claim(payment, "reconcile")
        _begin_step(lease, "status")
        self.outcome(payment, "FAILED")
        Payment.objects.filter(pk=payment.pk).update(reconciliation_lease_expires_at=timezone.now() - timedelta(seconds=1))
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_orders(), 1)
        self.assertEqual(expire_due_orders(), 0)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertIsNone(payment.reconciliation_lease_expires_at)
        self.assertIsNone(payment.settle_confirmed_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)
