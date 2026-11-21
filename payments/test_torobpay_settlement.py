"""First validated server observation, independent of callback/DB write time."""

from datetime import timedelta
import json
from unittest.mock import patch

from django.utils import timezone

from orders.models import Order
from payments.models import Payment
from payments.test_torobpay import CPGTestCase
from payments.torobpay_services import reconcile_payment


class SettlementObservationTests(CPGTestCase):
    def settled(self):
        payment = self.start()
        self.outcome(payment, "ONGOING")
        return payment

    def test_in_time_response_is_not_late_when_database_write_is_delayed(self):
        from payments.torobpay_services import _end_step
        payment = self.settled()
        observed = self.order.expires_at - timedelta(seconds=1)
        clock = [observed]

        def delayed(*args, **kwargs):
            clock[0] = self.order.expires_at + timedelta(seconds=1)
            return _end_step(*args, **kwargs)

        with patch("payments.torobpay_services.timezone.now", side_effect=lambda: clock[0]), \
                patch("payments.torobpay_services._end_step", side_effect=delayed):
            result = reconcile_payment(payment)
        self.assertTrue(result.is_successful)
        self.assertEqual(result.settle_confirmed_at, observed)
        self.assertIsNone(result.gateway_paid_at)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.assertFalse(self.order.inventory_released)

    def test_confirmation_at_exact_deadline_is_late(self):
        payment = self.settled()
        with patch("payments.torobpay_services.timezone.now", return_value=self.order.expires_at):
            result = reconcile_payment(payment)
        self.assert_review(result)
        self.assertEqual(result.settle_confirmed_at, self.order.expires_at)
        self.assertIsNone(result.gateway_paid_at)

    def test_confirmation_after_deadline_is_preserved_for_review(self):
        payment = self.settled()
        observed = self.order.expires_at + timedelta(microseconds=1)
        with patch("payments.torobpay_services.timezone.now", return_value=observed):
            result = reconcile_payment(payment)
        self.assert_review(result)
        self.assertEqual(result.settle_confirmed_at, observed)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_repeated_settle_keeps_first_timestamp_and_fulfills_once(self):
        from orders.services import mark_order_paid
        payment = self.settled()
        observed = timezone.now()
        with patch("payments.torobpay_services.timezone.now", return_value=observed), \
                patch("payments.torobpay_services.mark_order_paid", wraps=mark_order_paid) as paid:
            first = reconcile_payment(payment)
            with patch("payments.torobpay_services.timezone.now", return_value=observed + timedelta(minutes=40)):
                repeated = reconcile_payment(payment)  # Deliberately stale caller instance.
        self.assertEqual(first.settle_confirmed_at, observed)
        self.assertEqual(repeated.settle_confirmed_at, observed)
        self.assertTrue(repeated.is_successful)
        self.assertEqual(paid.call_count, 1)
        self.assertFalse(repeated.reservation_hold)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def assert_status_mismatch(self, key, value):
        payment = self.settled()
        self.remote[payment.provider_token][key] = value
        result = reconcile_payment(payment)
        self.assert_review(result)
        self.assertIsNone(result.settle_confirmed_at)
        self.assertIsNone(result.gateway_paid_at)

    def test_wrong_amount_never_creates_authoritative_timestamp(self):
        self.assert_status_mismatch("amount", 1)

    def test_wrong_transaction_never_creates_authoritative_timestamp(self):
        self.assert_status_mismatch("transactionId", "wrong-transaction")

    def test_unvalidated_identifier_echo_cannot_expose_merchant_jwt_in_audit(self):
        payment = self.settled()
        self.remote[payment.provider_token]["transactionId"] = self.jwt
        with self.assertLogs("payments", level="WARNING") as logs:
            result = reconcile_payment(payment)
        self.assertEqual(result.status, "manual_review")
        self.assertIsNone(result.settle_confirmed_at)
        self.assertNotIn(self.jwt, json.dumps(result.metadata["torobpay"]) + "\n".join(logs.output))

    def test_wrong_token_never_starts_status_or_sets_timestamp(self):
        payment = self.settled()
        Payment.objects.filter(pk=payment.pk).update(provider_token="synthetic-wrong-token")
        result = reconcile_payment(payment)
        self.assert_review(result)
        self.assertIsNone(result.settle_confirmed_at)
        self.assertEqual(len(self.api_calls("status")), 0)

    def test_cancelled_released_order_is_not_revived_by_settle(self):
        payment = self.settled()
        Order.objects.filter(pk=self.order.pk).update(status="cancelled", inventory_released=True)
        result = reconcile_payment(payment)
        self.assert_review(result)
        self.assertIsNotNone(result.settle_confirmed_at)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertTrue(self.order.inventory_released)

    def test_conflicting_status_preserves_first_receipt_and_enters_review(self):
        payment = reconcile_payment(self.settled())
        first = payment.settle_confirmed_at
        self.outcome(payment, "CANCELED")
        result = reconcile_payment(payment)
        self.assertEqual(result.status, "manual_review")
        self.assertEqual(result.settle_confirmed_at, first)
        self.assertTrue(result.reservation_hold)
        self.assertIsNone(result.gateway_paid_at)

    def test_callback_time_is_separate_from_first_server_confirmation(self):
        from payments.services import process_callback
        payment = self.settled()
        callback_time, observation_time = timezone.now(), timezone.now() + timedelta(seconds=2)
        clock = [callback_time]

        def status(record):
            clock[0] = observation_time
            return self.server_response(record, "status")

        self.responses["status"] = [status]
        with patch("payments.torobpay_services.timezone.now", side_effect=lambda: clock[0]):
            result = process_callback(payment, self.callback_data(payment))
        self.assertEqual(result.settle_confirmed_at, observation_time)
        self.assertNotEqual(result.settle_confirmed_at.isoformat(), result.metadata["torobpay"]["last_callback"]["received_at"])
        self.assertIsNone(result.gateway_paid_at)

    def test_changed_order_association_during_http_is_never_accepted(self):
        from payments.torobpay_services import _end_step
        payment = self.settled()
        other = Order.objects.create(customer=self.customer, order_number="ORD-WRONG-ASSOCIATION",
                                     total=self.order.total, expires_at=self.order.expires_at)

        def moved(*args, **kwargs):
            Payment.objects.filter(pk=payment.pk).update(order_id=other.pk)
            return _end_step(*args, **kwargs)

        with patch("payments.torobpay_services._end_step", side_effect=moved):
            result = reconcile_payment(payment)
        self.assertEqual(result.status, "manual_review")
        self.assertIsNone(result.settle_confirmed_at)
        self.assertTrue(result.metadata["torobpay"]["last_unvalidated_response"])
        other.refresh_from_db()
        self.order.refresh_from_db()
        self.assertFalse(other.is_paid)
        self.assertFalse(self.order.is_paid)

    def test_locked_receipt_identifier_conflict_stops_successful_reconciliation(self):
        payment = reconcile_payment(self.settled())
        first = payment.settle_confirmed_at

        def changed(record):
            Payment.objects.filter(pk=payment.pk).update(transaction_id="wrong-local-transaction")
            return self.server_response(record, "status")

        self.responses["status"] = [changed]
        result = reconcile_payment(payment)
        self.assertEqual(result.status, "manual_review")
        self.assertEqual(result.settle_confirmed_at, first)
        self.assertTrue(result.reservation_hold)
        self.assertIsNone(result.gateway_paid_at)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_legacy_recording_time_is_not_backfilled_or_reinterpreted_as_observation(self):
        payment = reconcile_payment(self.settled())
        Payment.objects.filter(pk=payment.pk).update(settle_confirmed_at=None)
        data = payment.metadata
        data["torobpay"]["settlement_evidence"].pop("source")
        Payment.objects.filter(pk=payment.pk).update(metadata=data)
        result = reconcile_payment(payment)
        self.assertTrue(result.is_successful)
        self.assertIsNone(result.settle_confirmed_at)
        self.assertIsNone(result.gateway_paid_at)

    def test_known_receipt_is_not_demoted_by_read_only_status_timeout(self):
        payment = reconcile_payment(self.settled())
        first = payment.settle_confirmed_at
        self.responses["status"] = [TimeoutError("synthetic")]
        result = reconcile_payment(payment)
        self.assertTrue(result.is_successful)
        self.assertEqual(result.settle_confirmed_at, first)
        self.assertTrue(result.reservation_hold)
        recovered = reconcile_payment(result)
        self.assertTrue(recovered.is_successful)
        self.assertFalse(recovered.reservation_hold)
        self.assertEqual(recovered.settle_confirmed_at, first)

    def test_persisted_in_time_observation_recovers_after_crash_with_stale_caller(self):
        from payments.gateways.torobpay import TorobPayGateway
        from payments.torobpay_services import _claim, _perform
        payment = self.settled()
        deadline = self.order.expires_at
        observed = deadline - timedelta(seconds=1)
        with patch("payments.torobpay_services.timezone.now", return_value=observed):
            _, lease = _claim(payment, "reconcile")
            _perform(lease, "status", TorobPayGateway().status)
        self.assertIsNone(payment.settle_confirmed_at)  # Caller predates the write.
        self.assertEqual(Payment.objects.get(pk=payment.pk).settle_confirmed_at, observed)
        Payment.objects.filter(pk=payment.pk).update(reconciliation_lease_expires_at=deadline)
        with patch("payments.torobpay_services.timezone.now", return_value=deadline + timedelta(seconds=2)):
            recovered = reconcile_payment(payment)
        self.assertTrue(recovered.is_successful)
        self.assertEqual(recovered.settle_confirmed_at, observed)
        self.assertIsNone(recovered.gateway_paid_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
