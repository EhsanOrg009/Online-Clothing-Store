"""Real Order/Payment locks on isolated MariaDB; provider HTTP remains mocked."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from orders.models import Order
from orders.services import expire_due_orders
from payments.models import Payment
from payments.services import PaymentError, create_payment_for_order, process_callback
from payments.tests import PaymentFixtureMixin
from payments.torobpay_services import reconcile_payment
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
                current.save(update_fields=["metadata"])
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
