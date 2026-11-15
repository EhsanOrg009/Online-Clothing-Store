"""Gateway time, delayed receipt and inventory rules; only simulated Zibal data."""

from datetime import datetime, timedelta, timezone as datetime_timezone

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order, OrderItem
from orders.services import cancel_order, expire_due_orders
from payments.gateways.zibal_time import parse_gateway_paid_at
from payments.services import process_callback
import payments.tests as fixtures


@override_settings(ZIBAL_PAID_AT_TIMEZONE="")
class GatewayTimestampTests(SimpleTestCase):
    now = datetime(2026, 11, 15, 12, tzinfo=datetime_timezone.utc)

    def parse(self, value, **kwargs):
        return parse_gateway_paid_at(value, now=self.now, **kwargs)

    def test_aware_offset_uses_its_actual_offset(self):
        result = self.parse("2026-11-14T10:28:00.123456+03:30")
        self.assertEqual(result.value, datetime(2026, 11, 14, 6, 58, 0, 123456, tzinfo=datetime_timezone.utc))
        self.assertEqual(result.source, "offset")
        self.assertEqual(result.issue, "")

    def test_z_means_utc_and_preserves_microseconds(self):
        self.assertEqual(self.parse("2026-11-14T10:28:00.000001Z").value.microsecond, 1)
        self.assertEqual(self.parse("2026-11-14T10:28:00Z").value.utcoffset(), timedelta(0))

    def test_configured_timezone_does_not_replace_an_explicit_offset(self):
        result = self.parse("2026-11-14T10:28:00+05:45", confirmed_timezone="America/New_York")
        self.assertEqual(result.value.hour, 4)
        self.assertEqual(result.value.minute, 43)
        self.assertEqual(result.source, "offset")

    def test_naive_time_is_ambiguous_without_confirmation(self):
        with override_settings(TIME_ZONE="America/New_York"):
            result = self.parse("2026-11-14T10:28:00")
        self.assertIsNone(result.value)
        self.assertEqual(result.issue, "timezone_unconfirmed")

    def test_explicit_iana_configuration_normalizes_naive_time(self):
        # Synthetic test confirmation only; this is not evidence about Zibal.
        with override_settings(ZIBAL_PAID_AT_TIMEZONE="Asia/Tehran"):
            result = self.parse("2026-11-14T10:28:00")
        self.assertEqual(result.value.hour, 6)
        self.assertEqual(result.value.minute, 58)
        self.assertEqual(result.source, "configuration")

    def test_invalid_configuration_requires_manual_review(self):
        for name in ("unknown/zone", " Asia/Tehran", "", "/etc/localtime"):
            with self.subTest(name=name):
                self.assertTrue(self.parse("2026-11-14T10:28:00", confirmed_timezone=name).issue)

    def test_dst_fold_is_not_silently_chosen(self):
        self.assertEqual(self.parse(
            "2026-11-01T01:30:00", confirmed_timezone="America/New_York",
        ).issue, "ambiguous_local_time")

    def test_dst_gap_is_not_made_aware(self):
        self.assertEqual(self.parse(
            "2026-03-08T02:30:00", confirmed_timezone="America/New_York",
        ).issue, "nonexistent_local_time")

    def test_invalid_and_overprecise_timestamps_are_flagged(self):
        for value in (
            None, True, 100, {}, "", "invalid", "2026-02-30T10:28:00Z",
            "2026-11-14 10:28:00", "2026-11-14", "۲۰۲۶-11-14T10:28:00Z",
            "2026-11-14T10:28:00.1234567Z", "2026-11-14T10:28:00+25:00",
            "2026-11-14T10:28:00+03:60", "2026-11-14T10:28:00-00:99",
            "2026-11-14T10:28:00+24:00",
            " 2026-11-14T10:28:00Z", "2026-11-14T10:28:00Z\n",
            "0001-01-01T00:00:00Z",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.parse(value).issue, "invalid_timestamp")

    def test_unknown_rfc3339_offset_requires_review(self):
        self.assertEqual(self.parse("2026-11-14T10:28:00-00:00").issue, "unknown_offset")

    def test_future_timestamp_is_flagged_with_interpretation_preserved(self):
        result = self.parse((self.now + timedelta(seconds=1)).isoformat())
        self.assertEqual(result.issue, "future_timestamp")
        self.assertIsNotNone(result.audit()["normalized_paid_at"])


class PaymentDeadlineTests(fixtures.PaymentFixtureMixin, fixtures.PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()

    def set_time(self, value):
        self.server_transactions[self.payment.authority]["paidAt"] = value

    def pay(self):
        return process_callback(self.payment, self.callback_data(self.payment))

    def test_before_deadline_delayed_callback_accepts_intact_reservation(self):
        deadline = timezone.now() - timedelta(minutes=5)
        paid_at = deadline - timedelta(minutes=2)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.set_time(paid_at.isoformat())
        arrival = timezone.now()
        payment = self.pay()
        self.order.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.assertFalse(payment.needs_refund)
        self.assertEqual(payment.gateway_paid_at, paid_at)
        self.assertGreaterEqual(payment.paid_at, arrival)
        self.assertEqual(self.order.paid_at, payment.paid_at)
        self.assertEqual(payment.metadata["decision"]["order_expires_at"], deadline.isoformat())
        self.assertEqual(payment.metadata["verification"]["paidAt"], paid_at.isoformat())
        self.assertFalse(self.order.inventory_released)
        self.assertEqual(self.variant.stock, 4)
        self.assertEqual(expire_due_orders(), 0)

    def test_payment_exactly_at_deadline_is_accepted(self):
        deadline = timezone.now() - timedelta(minutes=1)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.set_time(deadline.isoformat())
        self.assertFalse(self.pay().needs_refund)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)

    def test_actual_late_payment_records_receipt_without_fulfillment(self):
        deadline = timezone.now() - timedelta(minutes=5)
        paid_at = deadline + timedelta(minutes=2)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.set_time(paid_at.isoformat())
        payment = self.pay()
        self.assertTrue(payment.needs_refund)
        self.assertEqual(payment.gateway_paid_at, paid_at)
        self.assertEqual(payment.metadata["decision"]["reason"], "paid_after_deadline")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.expires_at, deadline)
        self.assertEqual(expire_due_orders(), 1)
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_expiry_first_never_revives_even_an_early_gateway_payment(self):
        deadline = timezone.now() - timedelta(minutes=5)
        self.set_time((deadline - timedelta(minutes=2)).isoformat())
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.assertEqual(expire_due_orders(), 1)
        payment = self.pay()
        self.assertTrue(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.assertIsNone(self.order.paid_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_released_reservation_flag_prevents_revival_despite_corrupt_status(self):
        cancel_order(self.order)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.PENDING_PAYMENT)
        self.assertTrue(self.pay().needs_refund)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_missing_variant_requires_manual_review_and_preserves_receipt(self):
        OrderItem.objects.filter(order=self.order).update(variant=None)
        payment = self.pay()
        self.assertEqual(payment.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(payment.metadata["decision"]["reason"], "inventory_reservation_unavailable")
        self.assertIsNotNone(payment.paid_at)
        self.assertFalse(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_naive_timestamp_after_deadline_is_reviewed_without_refund_guess(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        raw = "2026-11-14T10:28:00.000012"
        self.set_time(raw)
        payment = self.pay()
        self.assertEqual(payment.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(payment.needs_refund)
        self.assertIsNone(payment.gateway_paid_at)
        self.assertEqual(payment.metadata["verification"]["paidAt"], raw)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)

    def test_naive_timestamp_on_cancelled_order_does_not_automatically_refund(self):
        cancel_order(self.order)
        self.set_time("2026-11-14T10:28:00")
        payment = self.pay()
        self.assertTrue(payment.requires_manual_review)
        self.assertFalse(payment.needs_refund)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_naive_additional_payment_is_reviewed_without_refund_decision(self):
        other = self.start_payment()
        process_callback(other, self.callback_data(other))
        self.set_time("2026-11-14T10:28:00")
        payment = self.pay()
        self.assertTrue(payment.requires_manual_review)
        self.assertFalse(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)

    def test_confirmed_timezone_can_resolve_a_retry_without_rewriting_payment_time(self):
        raw = (timezone.now() - timedelta(minutes=2)).replace(tzinfo=None).isoformat()
        self.set_time(raw)
        first = self.pay()
        with override_settings(ZIBAL_PAID_AT_TIMEZONE="UTC"):
            recovered = self.pay()
        self.assertEqual(recovered.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(recovered.paid_at, first.paid_at)
        self.assertEqual(len(recovered.metadata["verification_history"]), 2)
        self.order.refresh_from_db()
        self.assertGreaterEqual(self.order.paid_at, first.paid_at)
        self.assertTrue(self.order.is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_future_timestamp_is_manual_and_does_not_expire_into_automatic_acceptance(self):
        paid_at = timezone.now() + timedelta(hours=1)
        self.set_time(paid_at.isoformat())
        payment = self.pay()
        self.assertTrue(payment.requires_manual_review)
        self.assertFalse(payment.needs_refund)
        self.assertEqual(payment.gateway_paid_at, paid_at)
        from unittest.mock import patch
        with patch("django.utils.timezone.now", return_value=paid_at + timedelta(seconds=1)):
            payment = self.pay()
        self.assertEqual(payment.metadata["decision"]["reason"], "previous_future_timestamp")
        self.assertTrue(payment.requires_manual_review)

    def test_conflicting_gateway_response_cannot_erase_manual_receipt(self):
        self.set_time("2026-11-14T10:28:00")
        first = self.pay()
        self.set_outcome(self.payment, "failed")
        repeated = self.pay()
        self.assertTrue(repeated.requires_manual_review)
        self.assertEqual(repeated.reference_id, first.reference_id)
        self.assertEqual(repeated.paid_at, first.paid_at)
        self.assertEqual(repeated.metadata["decision"]["reason"], "gateway_receipt_conflict")
        self.assertEqual(len(repeated.metadata["verification_history"]), 2)

    def test_locally_cancelled_attempt_can_be_reverified_after_real_success(self):
        self.set_outcome(self.payment, "cancelled")
        first = process_callback(self.payment, self.callback_data(self.payment, "cancelled"))
        self.assertEqual(first.status, PaymentStatus.CANCELLED)
        self.set_outcome(self.payment, "successful")
        self.assertEqual(self.pay().status, PaymentStatus.SUCCESSFUL)

    def test_manual_callback_returns_to_order_without_success_page(self):
        self.client.force_login(self.customer)
        self.set_time("2026-11-14T10:28:00")
        response = self.client.get("/payments/callback/?" + self.callback_query(self.payment))
        from django.urls import reverse
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.requires_manual_review)

    def test_manual_receipt_blocks_another_charge_and_hides_pay_button(self):
        from django.urls import reverse
        from payments.services import PaymentError, create_payment_for_order
        self.set_time("2026-11-14T10:28:00")
        self.pay()
        calls = len(self.api_calls("/v1/request"))
        with self.assertRaises(PaymentError):
            create_payment_for_order(self.order)
        self.assertEqual(len(self.api_calls("/v1/request")), calls)
        self.client.force_login(self.customer)
        response = self.client.get(reverse("orders:detail", args=[self.order.order_number]))
        self.assertContains(response, "لطفاً پرداخت تازه انجام ندهید")
        self.assertNotContains(response, 'action="' + reverse("orders:pay", args=[self.order.order_number]) + '"')
