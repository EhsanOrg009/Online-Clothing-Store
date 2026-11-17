"""تست چرخه زیبال با پاسخ‌های HTTP شبیه‌سازی‌شده؛ هیچ تراکنش واقعی انجام نمی‌شود."""

import gzip
import hashlib
import io
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import (
    Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase,
    override_settings, skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone

from accounts.models import Address, User
from cart.models import Cart, CartItem
from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order
from orders.services import create_order, mark_order_paid
from payments.gateways import ZibalGateway, get_gateway
from payments.gateways.base import PaymentGatewayError
from payments.models import Payment, PaymentCreationStatus
from payments.services import (
    PaymentError, create_payment_for_order, expire_stale_payments, process_callback,
)
from products.models import Category, Color, Product, ProductVariant, Size


TEST_SETTINGS = {
    "DEBUG": True,
    "PAYMENT_GATEWAY": "zibal",
    "ZIBAL_MERCHANT": "zibal",
    "ZIBAL_TIMEOUT_SECONDS": 10,
    "ZIBAL_PAID_AT_TIMEZONE": "",
    "SITE_URL": "https://minikandoo.example",
}


class PaymentAPIStubMixin:
    """حالت درگاه مستقل از پارامترهای Callback است و فقط در حافظه تست نگهداری می‌شود."""

    def setUp(self):
        super().setUp()
        self.server_transactions = {}
        self.next_track_id = 15966442233311
        self.http = self.enterContext(patch("payments.gateways.zibal._http_open"))
        self.http.side_effect = self.api_call

    @staticmethod
    def http_response(data=None, *, body=None, status=200):
        response = MagicMock()
        response.status = status
        response.read.return_value = body if body is not None else json.dumps(data).encode()
        response.__enter__.return_value = response
        return response

    def api_call(self, request, *, timeout):
        self.assertEqual(urlsplit(request.full_url).netloc, "gateway.zibal.ir")
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        path = urlsplit(request.full_url).path
        if path == "/v1/request":
            track_id = self.next_track_id
            self.next_track_id += 1
            self.server_transactions[str(track_id)] = {
                "result": 100, "status": 2,
                "amount": payload["amount"], "orderId": payload["orderId"],
                "refNumber": str(track_id + 100),
                "paidAt": (timezone.now() - timedelta(seconds=1)).isoformat(),
                "cardNumber": "62741****44", "message": "success",
            }
            data = {"result": 100, "trackId": track_id, "message": "success"}
        else:
            data = self.server_transactions[str(payload["trackId"])]
            if path == "/v1/verify":
                if data["status"] == 2:
                    data["status"] = 1
                    data = data.copy()
                elif data["status"] == 1:
                    data = {"result": 201, "message": "already verified"}
                else:
                    data = {"result": 202, "message": "not paid"}
            elif path == "/v1/inquiry":
                data = data.copy()
            else:
                raise AssertionError(f"Unexpected API endpoint: {path}")
        return self.http_response(data)

    def set_outcome(self, payment, outcome):
        self.server_transactions[payment.authority]["status"] = {
            "successful": 2, "failed": 5, "cancelled": 3, "pending": -1,
        }[outcome]

    @staticmethod
    def callback_data(payment, outcome="successful"):
        success, status = {
            "successful": ("1", "2"), "failed": ("0", "5"),
            "cancelled": ("0", "3"), "pending": ("0", "-1"),
        }[outcome]
        return {
            "trackId": payment.authority, "success": success,
            "status": status, "orderId": payment.order.order_number,
        }

    def callback_query(self, payment, outcome="successful"):
        return urlencode(self.callback_data(payment, outcome))

    def api_calls(self, path):
        return [call for call in self.http.call_args_list if urlsplit(call.args[0].full_url).path == path]


@override_settings(**TEST_SETTINGS)
class PaymentTestCase(PaymentAPIStubMixin, TestCase):
    pass


class PaymentFixtureMixin:
    def build_payment_data(self, stock=5, price=500000):
        self.category = Category.objects.create(name="پلیور")
        self.product = Product.objects.create(
            name="پلیور نمونه", category=self.category, base_price=Decimal(price),
            is_published=True, is_active=True,
        )
        self.color = Color.objects.create(name="سرمه‌ای")
        self.size = Size.objects.create(name="L")
        self.variant = ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=stock,
        )
        self.customer = User.objects.create_user(
            phone_number="09123459999", first_name="کاربر", last_name="پرداخت",
        )
        self.address = Address.objects.create(
            user=self.customer, receiver_name="کاربر پرداخت", receiver_phone="09123459999",
            province="تهران", city="تهران", address_line="خیابان انقلاب، پلاک ۵",
            postal_code="1234567890", is_default=True,
        )
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        self.order = create_order(customer=self.customer, cart=cart, address=self.address)

    def start_payment(self):
        return create_payment_for_order(self.order)

    def historical_payment(self):
        """Explicit historical attempts, including duplicates created by old code.

        New submissions reuse a pending attempt; receipt-safety tests must still
        exercise distinct gateway transactions rather than that deduplication.
        """
        gateway = get_gateway()
        result = gateway.create_payment(self.order)
        return Payment.objects.create(
            order=self.order, gateway=gateway.name, amount=self.order.total,
            authority=result.authority, metadata={"redirect_url": result.redirect_url},
        )


class GatewayTests(PaymentTestCase):
    def test_registry_returns_zibal(self):
        self.assertIsInstance(get_gateway(), ZibalGateway)
        self.assertEqual(get_gateway().name, "zibal")

    def test_existing_class_path_configuration_is_supported(self):
        self.assertIsInstance(get_gateway("payments.gateways.zibal.ZibalGateway"), ZibalGateway)

    def test_unknown_gateway_path_is_rejected(self):
        with self.assertRaises(PaymentGatewayError):
            get_gateway("payments.gateways.not_exists.Nope")

    def test_other_gateway_cannot_be_selected(self):
        with self.assertRaises(PaymentGatewayError):
            get_gateway("mock")

    def test_missing_merchant_is_rejected(self):
        with override_settings(ZIBAL_MERCHANT=""):
            with self.assertRaises(PaymentGatewayError):
                get_gateway()
        self.http.assert_not_called()

    def test_official_test_account_is_disabled_in_production(self):
        with override_settings(DEBUG=False):
            with self.assertRaises(PaymentGatewayError):
                get_gateway()

    def test_production_merchant_is_supported_without_debug(self):
        with override_settings(DEBUG=False, ZIBAL_MERCHANT="unit-test-merchant"):
            self.assertIsInstance(get_gateway(), ZibalGateway)

    def test_invalid_timeouts_are_rejected(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=value), override_settings(ZIBAL_TIMEOUT_SECONDS=value):
                with self.assertRaises(PaymentGatewayError):
                    get_gateway()


class CreatePaymentTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()

    def test_payment_request_uses_official_contract_and_rials(self):
        payment = self.start_payment()
        request = self.api_calls("/v1/request")[0].args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://gateway.zibal.ir/v1/request")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(payload["merchant"], "zibal")
        self.assertEqual(payload["amount"], int(self.order.total) * 10)
        self.assertIs(type(payload["amount"]), int)
        self.assertEqual(payload["orderId"], self.order.order_number)
        self.assertEqual(payload["mobile"], self.customer.phone_number)
        self.assertEqual(payload["callbackUrl"], "https://minikandoo.example" + reverse(
            "payments:callback_request", kwargs={"request_id": payment.request_id},
        ))
        self.assertEqual(payment.gateway, "zibal")
        self.assertEqual(payment.amount, self.order.total)
        self.assertEqual(payment.order_id, self.order.pk)
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertEqual(payment.authority, "15966442233311")

    def test_customer_redirect_uses_official_url_without_credentials(self):
        payment = self.start_payment()
        self.assertEqual(payment.metadata["redirect_url"], f"https://gateway.zibal.ir/start/{payment.authority}")
        self.assertEqual(urlsplit(payment.metadata["redirect_url"]).query, "")

    def test_client_amount_and_host_do_not_control_payment(self):
        request = RequestFactory().post("/", {"amount": "1"}, HTTP_HOST="attacker.example")
        payment = create_payment_for_order(self.order, request)
        payload = json.loads(self.http.call_args.args[0].data)
        self.assertEqual(payment.amount, self.order.total)
        self.assertEqual(payload["amount"], int(self.order.total) * 10)
        self.assertEqual(payload["callbackUrl"], "https://minikandoo.example" + reverse(
            "payments:callback_request", kwargs={"request_id": payment.request_id},
        ))

    def test_development_callback_can_use_configured_http_origin(self):
        with override_settings(SITE_URL="http://localhost:8765"):
            payment = self.start_payment()
        self.assertEqual(json.loads(self.http.call_args.args[0].data)["callbackUrl"], "http://localhost:8765" + reverse(
            "payments:callback_request", kwargs={"request_id": payment.request_id},
        ))

    def test_production_request_requires_https_callback(self):
        with override_settings(DEBUG=False, ZIBAL_MERCHANT="unit-test-merchant", SITE_URL="http://minikandoo.example"):
            with self.assertRaises(PaymentError):
                self.start_payment()
        self.http.assert_not_called()

    def test_invalid_site_urls_do_not_create_a_payment(self):
        for origin in ("", "javascript:alert(1)", "https://user:password@example.com", "https://example.com?redirect=evil"):
            with self.subTest(origin=origin), override_settings(SITE_URL=origin):
                with self.assertRaises(PaymentError):
                    self.start_payment()
        self.assertEqual(Payment.objects.count(), 0)
        self.http.assert_not_called()

    def test_gateway_amount_limits_do_not_change_order_data(self):
        for amount in (Decimal("99"), Decimal("400000001")):
            with self.subTest(amount=amount):
                Order.objects.filter(pk=self.order.pk).update(total=amount)
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.order.refresh_from_db()
                self.assertEqual(self.order.total, amount)
        self.assertEqual(Payment.objects.count(), 0)
        self.http.assert_not_called()

    def test_api_request_errors_leave_order_unpaid(self):
        for code in (102, 103, 104, 105, 106, 112, 113, 115, 140, 999):
            with self.subTest(code=code), transaction.atomic():
                self.http.side_effect = None
                self.http.return_value = self.http_response({"result": code, "message": "sensitive remote text"})
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.count(), 1)
                attempt = Payment.objects.get()
                self.assertEqual(attempt.creation_status, PaymentCreationStatus.UNCERTAIN if code == 999 else PaymentCreationStatus.REJECTED)
                self.assertIsNone(attempt.authority)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
                transaction.set_rollback(True)

    def test_invalid_request_track_ids_are_rejected(self):
        for track_id in (None, "", 0, -1, True, 1.5, "1/redirect", "01", 2**63):
            with self.subTest(track_id=track_id), transaction.atomic():
                self.http.side_effect = None
                self.http.return_value = self.http_response({"result": 100, "trackId": track_id})
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.get().creation_status, PaymentCreationStatus.UNCERTAIN)
                transaction.set_rollback(True)

    def test_invalid_api_responses_are_handled(self):
        for body in (b"not json", b"[]", b"null", b'{"result":true}', b'{"result":"100"}', b"{}", b"\xff"):
            with self.subTest(body=body), transaction.atomic():
                self.http.side_effect = None
                self.http.return_value = self.http_response(body=body)
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.get().creation_status, PaymentCreationStatus.UNCERTAIN)
                transaction.set_rollback(True)

    def test_connection_timeout_and_http_errors_are_handled(self):
        for error in (
            URLError("offline"), TimeoutError("timed out"), BadStatusLine("invalid HTTP"),
            IncompleteRead(b"partial", 10),
            HTTPError("https://gateway.zibal.ir/v1/request", 302, "redirect", {}, None),
            HTTPError("https://gateway.zibal.ir/v1/request", 503, "unavailable", {}, None),
        ):
            with self.subTest(error=type(error).__name__), transaction.atomic():
                self.http.side_effect = error
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.get().creation_status, PaymentCreationStatus.UNCERTAIN)
                transaction.set_rollback(True)
        self.http.side_effect = None
        self.http.return_value = self.http_response({}, status=502)
        with self.assertRaises(PaymentError):
            self.start_payment()

    def test_gateway_timeout_is_passed_to_http_client(self):
        with override_settings(ZIBAL_TIMEOUT_SECONDS=3.5):
            self.start_payment()
        self.assertEqual(self.http.call_args.kwargs["timeout"], 3.5)

    def test_duplicate_track_id_is_handled_without_duplicate_payment(self):
        first = self.start_payment()
        second = self.start_payment()
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(len(self.api_calls("/v1/request")), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_credentials_and_raw_error_text_are_not_logged(self):
        credential = "unit-test-sensitive-merchant"
        self.http.side_effect = None
        self.http.return_value = self.http_response({"result": 104, "message": credential})
        with override_settings(ZIBAL_MERCHANT=credential), self.assertLogs("payments.services", level="ERROR") as logs:
            with self.assertRaises(PaymentError):
                self.start_payment()
        self.assertNotIn(credential, "\n".join(logs.output))

    def test_already_paid_order_cannot_pay_again(self):
        payment = self.start_payment()
        process_callback(payment, self.callback_data(payment))
        with self.assertRaises(PaymentError):
            self.start_payment()

    def test_failed_payment_can_be_retried(self):
        first = self.start_payment()
        self.set_outcome(first, "failed")
        process_callback(first, self.callback_data(first, "failed"))
        second = self.start_payment()
        self.assertNotEqual(first.authority, second.authority)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.PENDING)

    def test_expired_order_cannot_start_payment(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.http.assert_not_called()

    def test_cancelled_order_cannot_start_payment(self):
        from orders.services import cancel_order
        cancel_order(self.order)
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.http.assert_not_called()

    def test_timeout_preserves_request_and_cannot_issue_another_gateway_request(self):
        self.http.side_effect = TimeoutError("synthetic timeout")
        with self.assertRaises(PaymentError):
            self.start_payment()
        payment = self.order.payments.get()
        self.assertIsNotNone(payment.request_id)
        self.assertIsNone(payment.authority)
        self.assertEqual(payment.creation_status, PaymentCreationStatus.UNCERTAIN)
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertFalse(payment.needs_refund)
        self.assertIsNone(payment.paid_at)
        self.assertEqual(payment.metadata["creation"]["responses"][0]["outcome"], "uncertain")
        self.http.side_effect = self.api_call
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.assertEqual(len(self.api_calls("/v1/request")), 1)
        self.assertEqual(self.order.payments.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_documented_rejection_permits_a_new_attempt_without_erasing_evidence(self):
        self.http.side_effect = None
        self.http.return_value = self.http_response({"result": 104})
        with self.assertRaises(PaymentError):
            self.start_payment()
        rejected = self.order.payments.get()
        self.http.side_effect = self.api_call
        ready = self.start_payment()
        self.assertNotEqual(ready.request_id, rejected.request_id)
        self.assertEqual(rejected.creation_status, PaymentCreationStatus.REJECTED)
        self.assertEqual(rejected.metadata["creation"]["responses"][0]["result"], 104)
        self.assertEqual(self.order.payments.count(), 2)
        self.assertEqual(ready.creation_status, PaymentCreationStatus.READY)

    def test_local_payment_expiry_reuses_known_session_without_new_http_request(self):
        first = self.start_payment()
        Payment.objects.filter(pk=first.pk).update(status=PaymentStatus.EXPIRED)
        reused = self.start_payment()
        self.assertEqual(reused.pk, first.pk)
        self.assertEqual(reused.authority, first.authority)
        self.assertEqual(reused.status, PaymentStatus.PENDING)
        self.assertEqual(len(self.api_calls("/v1/request")), 1)

    def test_multiple_historical_pending_attempts_block_new_creation(self):
        self.historical_payment()
        self.historical_payment()
        self.http.reset_mock()
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.http.assert_not_called()

    def test_created_track_is_preserved_if_cancellation_wins_during_http(self):
        from orders.services import cancel_order
        def cancel_then_reply(request, *, timeout):
            cancel_order(self.order)
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = cancel_then_reply
        with self.assertRaises(PaymentError):
            self.start_payment()
        payment = self.order.payments.get()
        self.assertIn(payment.authority, self.server_transactions)
        self.assertEqual(payment.creation_status, PaymentCreationStatus.REVIEW)
        self.assertFalse(payment.requires_manual_review)  # Creating a session is not received money.
        self.assertIsNone(payment.paid_at)
        self.assertFalse(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_order_amount_change_during_http_keeps_created_track_for_review(self):
        def change_then_reply(request, *, timeout):
            Order.objects.filter(pk=self.order.pk).update(total=self.order.total + 1)
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = change_then_reply
        with self.assertRaises(PaymentError):
            self.start_payment()
        payment = self.order.payments.get()
        self.assertEqual(payment.creation_status, PaymentCreationStatus.REVIEW)
        self.assertEqual(payment.amount, self.order.total)
        self.assertEqual(payment.metadata["creation"]["responses"][0]["track_id"], payment.authority)

    def test_callback_can_bind_request_before_creation_response_is_applied(self):
        callbacks = []
        def callback_then_reply(request, *, timeout):
            response = self.api_call(request, timeout=timeout)
            if urlsplit(request.full_url).path == "/v1/request":
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                data = {"trackId": track_id, "orderId": self.order.order_number, "success": "1", "status": "2"}
                callbacks.append(self.client.get(reverse("payments:callback_request", args=[attempt.request_id]), data))
            return response
        self.http.side_effect = callback_then_reply
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            payment = self.start_payment()
        paid.assert_called_once()
        self.assertTrue(payment.is_successful)
        self.assertEqual(callbacks[0].url, reverse("payments:result_success", args=[self.order.order_number]))
        self.assertEqual(payment.reference_id, self.server_transactions[payment.authority]["refNumber"])
        self.assertEqual(payment.callback_count, 1)
        self.assertIn("verification_history", payment.metadata)
        self.assertIn("responses", payment.metadata["creation"])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_unexpected_creation_failure_is_not_swallowed_and_keeps_durable_attempt(self):
        self.http.side_effect = RuntimeError("synthetic unexpected failure")
        with self.assertRaises(RuntimeError):
            self.start_payment()
        payment = self.order.payments.get()
        self.assertEqual(payment.creation_status, PaymentCreationStatus.REQUESTING)
        self.assertIsNone(payment.authority)
        self.http.side_effect = self.api_call
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.assertEqual(len(self.api_calls("/v1/request")), 1)

    def test_duplicate_gateway_track_for_another_order_keeps_both_attempts(self):
        first = self.start_payment()
        cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        another = create_order(customer=self.customer, cart=cart, address=self.address)
        self.http.side_effect = None
        self.http.return_value = self.http_response({"result": 100, "trackId": int(first.authority)})
        with self.assertRaises(PaymentError):
            create_payment_for_order(another)
        current = another.payments.get()
        self.assertIsNone(current.authority)
        self.assertEqual(current.creation_status, PaymentCreationStatus.REVIEW)
        self.assertEqual(current.metadata["creation"]["responses"][0]["track_id"], first.authority)
        first.refresh_from_db()
        self.assertEqual(first.creation_status, PaymentCreationStatus.READY)
        self.assertEqual(Payment.objects.count(), 2)

    def test_manual_receipt_committed_during_creation_blocks_the_returned_session(self):
        def manual_then_reply(request, *, timeout):
            Payment.objects.create(
                order=self.order, gateway="zibal", authority="987654321", amount=self.order.total,
                status=PaymentStatus.MANUAL_REVIEW, paid_at=timezone.now(),
                metadata={"verification": {"result": 100, "status": 1, "paidAt": "2026-11-14T10:28:00"}},
            )
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = manual_then_reply
        with self.assertRaises(PaymentError):
            self.start_payment()
        created = self.order.payments.get(request_id__isnull=False)
        self.assertEqual(created.creation_status, PaymentCreationStatus.REVIEW)
        self.assertIsNotNone(created.authority)
        self.assertFalse(created.needs_refund)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_timeout_after_remote_creation_can_be_resolved_by_verified_correlated_callback(self):
        def create_then_timeout(request, *, timeout):
            self.api_call(request, timeout=timeout)
            raise TimeoutError("synthetic missing response")
        self.http.side_effect = create_then_timeout
        with self.assertRaises(PaymentError):
            self.start_payment()
        attempt = self.order.payments.get()
        track_id = next(iter(self.server_transactions))
        self.http.side_effect = self.api_call
        response = self.client.get(reverse("payments:callback_request", args=[attempt.request_id]), {
            "trackId": track_id, "orderId": self.order.order_number, "success": "1", "status": "2",
        })
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_successful)
        self.assertEqual(attempt.authority, track_id)
        self.assertEqual(attempt.creation_status, PaymentCreationStatus.READY)
        self.assertEqual(attempt.metadata["creation"]["responses"][0]["outcome"], "uncertain")
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))
        self.assertEqual(len(self.api_calls("/v1/request")), 1)

    def test_failed_callback_during_creation_does_not_get_its_order_state_reset(self):
        def callback_then_reply(request, *, timeout):
            response = self.api_call(request, timeout=timeout)
            if urlsplit(request.full_url).path == "/v1/request":
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                self.server_transactions[track_id]["status"] = 5
                self.client.get(reverse("payments:callback_request", args=[attempt.request_id]), {
                    "trackId": track_id, "orderId": self.order.order_number, "success": "0", "status": "5",
                })
            return response
        self.http.side_effect = callback_then_reply
        attempt = self.start_payment()
        self.assertEqual(attempt.status, PaymentStatus.FAILED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAYMENT_FAILED)
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)

    def test_changed_identity_after_callback_binding_during_timeout_requires_review(self):
        def callback_then_timeout(request, *, timeout):
            response = self.api_call(request, timeout=timeout)
            if urlsplit(request.full_url).path == "/v1/request":
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                self.server_transactions[track_id]["status"] = -1
                self.client.get(reverse("payments:callback_request", args=[attempt.request_id]), {
                    "trackId": track_id, "orderId": self.order.order_number, "success": "0", "status": "-1",
                })
                Order.objects.filter(pk=self.order.pk).update(total=self.order.total + 10)
                raise TimeoutError("synthetic missing creation response")
            return response
        self.http.side_effect = callback_then_timeout
        with self.assertRaises(PaymentError):
            self.start_payment()
        attempt = self.order.payments.get()
        self.assertIsNotNone(attempt.authority)
        self.assertEqual(attempt.creation_status, PaymentCreationStatus.REVIEW)
        self.assertFalse(attempt.needs_refund)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)


class CallbackTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()

    def assert_unpaid(self):
        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertIsNone(self.payment.verified_at)
        self.assertIsNone(self.payment.paid_at)
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.PENDING)

    def test_success_is_verified_server_side_before_order_is_paid(self):
        paid_at = timezone.now() - timedelta(seconds=1)
        raw_time = paid_at.astimezone(datetime_timezone(timedelta(hours=3, minutes=30))).isoformat()
        self.server_transactions[self.payment.authority].update(trackId=int(self.payment.authority), paidAt=raw_time)
        payment = process_callback(self.payment, self.callback_data(self.payment))
        request = self.api_calls("/v1/verify")[0].args[0]
        self.assertEqual(json.loads(request.data), {"merchant": "zibal", "trackId": int(payment.authority)})
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.transaction_id, payment.authority)
        self.assertEqual(payment.reference_id, str(int(payment.authority) + 100))
        self.assertIsNotNone(payment.verified_at)
        self.assertIsNotNone(payment.paid_at)
        self.assertEqual(payment.gateway_paid_at, paid_at)
        self.assertEqual(payment.metadata["verification"]["paidAt"], raw_time)
        self.assertEqual(payment.metadata["verification"]["trackId"], int(payment.authority))
        self.assertEqual(payment.metadata["verification"]["payment_time"]["source"], "offset")
        self.assertEqual(payment.metadata["verification"]["amount"], int(payment.amount) * 10)
        self.assertNotIn("cardNumber", payment.metadata["verification"])
        self.assertNotIn("merchant", payment.metadata["verification"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(self.order.paid_at, payment.paid_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_callback_is_idempotent_without_reverification(self):
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            first = process_callback(self.payment, self.callback_data(self.payment))
            calls = self.http.call_count
            second = process_callback(first, self.callback_data(first))
        self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(self.http.call_count, calls)
        self.assertEqual(second.reference_id, first.reference_id)
        self.assertEqual(second.paid_at, first.paid_at)
        self.assertEqual(second.callback_count, 2)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        self.assertEqual(Payment.objects.filter(order=self.order, status=PaymentStatus.SUCCESSFUL).count(), 1)

    def test_integer_reference_is_persisted_as_string(self):
        self.server_transactions[self.payment.authority]["refNumber"] = 123456789012345
        payment = process_callback(self.payment, self.callback_data(self.payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.reference_id, "123456789012345")

    def test_numeric_string_reference_is_persisted(self):
        self.server_transactions[self.payment.authority]["refNumber"] = "123456789012345"
        payment = process_callback(self.payment, self.callback_data(self.payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.reference_id, "123456789012345")

    def test_reference_leading_zeros_are_preserved(self):
        self.server_transactions[self.payment.authority]["refNumber"] = "001234567890"
        payment = process_callback(self.payment, self.callback_data(self.payment))
        payment.refresh_from_db()
        self.assertEqual(payment.reference_id, "001234567890")
        self.assertEqual(payment.metadata["verification"]["refNumber"], "001234567890")
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)

    def test_maximum_length_integer_reference_is_accepted(self):
        reference = 10**63
        self.server_transactions[self.payment.authority]["refNumber"] = reference
        payment = process_callback(self.payment, self.callback_data(self.payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.reference_id, str(reference))
        self.assertEqual(len(payment.reference_id), 64)

    def test_maximum_length_string_reference_is_preserved(self):
        reference = "0" * 63 + "1"
        self.server_transactions[self.payment.authority]["refNumber"] = reference
        payment = process_callback(self.payment, self.callback_data(self.payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.reference_id, reference)

    def test_invalid_references_remain_pending_in_verify_and_inquiry(self):
        original = self.server_transactions[self.payment.authority].copy()
        invalid_references = (
            None, "", "0", "000", 0, -1, True, False,
            "abc123", "123abc", "-123", "+123", "123.45", " 123", "123 ",
            "1" * 65, 10**64, 123.0, [], {}, "۱۲۳", "١٢٣", "１２３", "123\n",
        )
        for verify_result in (100, 201):
            for reference in invalid_references:
                data = {**original, "status": 1, "refNumber": reference}
                responses = [self.http_response(data)] if verify_result == 100 else [
                    self.http_response({"result": 201}), self.http_response(data),
                ]
                with self.subTest(verify_result=verify_result, reference=reference):
                    self.http.side_effect = responses
                    with self.assertRaises(PaymentError):
                        process_callback(self.payment, self.callback_data(self.payment))
                    self.assert_unpaid()
                    self.assertEqual(self.payment.reference_id, "")
                    self.assertEqual(self.payment.transaction_id, "")
                    self.assertEqual(self.payment.callback_count, 0)
                    self.assertFalse(self.payment.needs_refund)
                    self.variant.refresh_from_db()
                    self.assertEqual(self.variant.stock, 4)

    def test_forged_success_does_not_override_failed_server_transaction(self):
        self.set_outcome(self.payment, "failed")
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.assertEqual(len(self.api_calls("/v1/verify")), 1)
        self.assertEqual(len(self.api_calls("/v1/inquiry")), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAYMENT_FAILED)
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_forged_cancellation_does_not_override_real_success(self):
        payment = process_callback(self.payment, self.callback_data(self.payment, "cancelled"))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)

    def test_pending_server_transaction_cannot_be_cancelled_by_callback(self):
        self.set_outcome(self.payment, "pending")
        payment = process_callback(self.payment, self.callback_data(self.payment, "cancelled"))
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assert_unpaid()

    def test_cancelled_transaction_is_confirmed_by_server_inquiry(self):
        self.set_outcome(self.payment, "cancelled")
        payment = process_callback(self.payment, self.callback_data(self.payment, "cancelled"))
        self.assertEqual(payment.status, PaymentStatus.CANCELLED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.CANCELLED)
        calls = self.http.call_count
        process_callback(payment, self.callback_data(payment, "cancelled"))
        self.assertEqual(self.http.call_count, calls)

    def test_invalid_callback_fields_do_not_mutate_order_or_payment(self):
        for field, value in (("trackId", "9900"), ("orderId", "OTHER-ORDER"), ("success", "true"), ("status", "999")):
            data = {**self.callback_data(self.payment), field: value}
            with self.subTest(field=field), self.assertRaises(PaymentError):
                process_callback(self.payment, data)
            self.assert_unpaid()
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)
        self.assertEqual(self.payment.callback_count, 0)

    def test_changed_order_amount_does_not_finalize_payment(self):
        Order.objects.filter(pk=self.order.pk).update(total=Decimal("1"))
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertFalse(self.order.is_paid)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_mismatched_server_amount_order_or_track_id_cannot_pay_order(self):
        original = self.server_transactions[self.payment.authority].copy()
        for field, value in (
            ("amount", original["amount"] + 10),
            ("amount", int(self.payment.amount)),  # Toman is not a Rial amount.
            ("amount", True),
            ("amount", float(original["amount"])),
            ("amount", str(original["amount"])),
            ("orderId", "OTHER-ORDER"),
            ("trackId", int(self.payment.authority) + 1),
            ("trackId", str(int(self.payment.authority) + 1)),
            ("trackId", None),
            ("trackId", True),
        ):
            self.server_transactions[self.payment.authority] = {**original, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(PaymentError):
                process_callback(self.payment, self.callback_data(self.payment))
            self.assert_unpaid()

    def test_success_response_requires_verified_status_and_reference(self):
        original = self.server_transactions[self.payment.authority].copy()
        for changes in (
            {"status": 2}, {"status": True}, {"status": 999},
            {"refNumber": None}, {"refNumber": 0}, {"refNumber": True},
        ):
            data = {**original, "status": 1, **changes}
            self.http.side_effect = None
            self.http.return_value = self.http_response(data)
            with self.subTest(changes=changes), self.assertRaises(PaymentError):
                process_callback(self.payment, self.callback_data(self.payment))
            self.assert_unpaid()

    def test_documented_response_can_omit_track_id_and_present_values_are_compared_numerically(self):
        original = {**self.server_transactions[self.payment.authority], "status": 1}
        for supplied in ({}, {"trackId": int(self.payment.authority)}, {"trackId": self.payment.authority}):
            with self.subTest(supplied=supplied):
                self.http.side_effect = [self.http_response({**original, **supplied})]
                result = get_gateway().verify_payment(self.payment, self.callback_data(self.payment))
                self.assertTrue(result.successful)
                self.assertEqual(result.transaction_id, self.payment.authority)
                self.assertEqual("trackId" in result.raw, "trackId" in supplied)
                self.assert_unpaid()  # A gateway result alone does not apply fulfillment.

    def test_previously_verified_transaction_requires_matching_inquiry(self):
        self.server_transactions[self.payment.authority]["status"] = 1
        self.server_transactions[self.payment.authority]["refNumber"] = "001234567890"
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.reference_id, "001234567890")
        self.assertEqual(len(self.api_calls("/v1/verify")), 1)
        self.assertEqual(len(self.api_calls("/v1/inquiry")), 1)
        self.assertEqual(payment.metadata["verification"]["verify_result"], 201)

    def test_previously_verified_result_alone_is_not_success(self):
        self.http.side_effect = [
            self.http_response({"result": 201}),
            self.http_response({"result": 100, "status": 1}),
        ]
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.assert_unpaid()

    def test_inquiry_must_report_verified_status_for_result_201(self):
        self.server_transactions[self.payment.authority]["status"] = 2
        self.http.side_effect = [
            self.http_response({"result": 201}),
            self.http_response(self.server_transactions[self.payment.authority]),
        ]
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.assert_unpaid()

    def test_inquiry_errors_do_not_finalize_transaction(self):
        for result in (102, 103, 104, 203, 999):
            with self.subTest(result=result):
                self.http.side_effect = [
                    self.http_response({"result": 201}),
                    self.http_response({"result": result}),
                ]
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

    def test_failed_verification_requires_matching_inquiry(self):
        self.set_outcome(self.payment, "failed")
        self.server_transactions[self.payment.authority]["orderId"] = "OTHER-ORDER"
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.assert_unpaid()

    def test_verification_api_errors_are_safe_and_retryable(self):
        for result in (102, 103, 104, 115, 203, 999):
            with self.subTest(result=result):
                self.http.side_effect = None
                self.http.return_value = self.http_response({"result": result})
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

    def test_network_failures_during_verification_do_not_finalize_payment(self):
        for error in (URLError("offline"), TimeoutError("timeout")):
            with self.subTest(error=type(error).__name__):
                self.http.side_effect = error
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

    def test_verification_transport_errors_are_retryable_and_redacted(self):
        sensitive = "unit-test-merchant-and-card-data"
        for error in (
            URLError(sensitive), TimeoutError(sensitive), OSError(sensitive),
            BadStatusLine(sensitive), IncompleteRead(sensitive.encode(), 100),
            HTTPError("https://gateway.zibal.ir/v1/verify", 503, sensitive, {}, None),
        ):
            with self.subTest(error=type(error).__name__):
                self.http.side_effect = error
                with self.assertLogs("payments.services", level="ERROR") as logs:
                    with self.assertRaises(PaymentError):
                        process_callback(self.payment, self.callback_data(self.payment))
                self.assertNotIn(sensitive, "\n".join(logs.output))
                self.assert_unpaid()

    def test_non_success_http_responses_during_verify_and_inquiry_stay_pending(self):
        sensitive = "unit-test-merchant-and-card-data"
        for verify_result in (None, 201, 202):
            with self.subTest(verify_result=verify_result):
                response = self.http_response({"result": 100, "message": sensitive}, status=502)
                self.http.side_effect = [response] if verify_result is None else [
                    self.http_response({"result": verify_result}), response,
                ]
                with self.assertLogs("payments.services", level="ERROR") as logs:
                    with self.assertRaises(PaymentError):
                        process_callback(self.payment, self.callback_data(self.payment))
                self.assertNotIn(sensitive, "\n".join(logs.output))
                self.assert_unpaid()

    def test_timeout_after_remote_verification_recovers_using_inquiry(self):
        self.server_transactions[self.payment.authority]["status"] = 1
        self.http.side_effect = TimeoutError("response lost")
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.assert_unpaid()
        self.http.side_effect = self.api_call
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.metadata["verification"]["verify_result"], 201)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_timeout_during_inquiry_remains_retryable(self):
        self.http.side_effect = [self.http_response({"result": 201}), TimeoutError("timeout")]
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        self.assert_unpaid()

    def test_network_and_http_failures_during_inquiry_are_retryable_and_redacted(self):
        sensitive = "unit-test-merchant-and-card-data"
        original_metadata = json.loads(json.dumps(self.payment.metadata))
        errors = (
            URLError(sensitive), TimeoutError(sensitive), OSError(sensitive),
            BadStatusLine(sensitive), IncompleteRead(sensitive.encode(), 100),
            HTTPError("https://gateway.zibal.ir/v1/inquiry", 503, sensitive, {}, None),
        )
        for verify_result in (201, 202):
            for error in errors:
                with self.subTest(verify_result=verify_result, error=type(error).__name__):
                    self.http.side_effect = [self.http_response({"result": verify_result}), error]
                    with self.assertLogs("payments.services", level="ERROR") as logs:
                        with self.assertRaises(PaymentError):
                            process_callback(self.payment, self.callback_data(self.payment))
                    self.assertNotIn(sensitive, "\n".join(logs.output))
                    self.assert_unpaid()
                    self.assertEqual(self.payment.metadata, original_metadata)

    def test_malformed_inquiry_responses_are_retryable_and_redacted(self):
        sensitive = "unit-test-merchant-and-card-data"
        bodies = (
            sensitive.encode(), b"[]", b"null", b"{}", b"\xff",
            b'{"result":true}', b'{"result":"100"}',
            json.dumps({"result": 100, "status": 1, "cardNumber": sensitive}).encode(),
        )
        for verify_result in (201, 202):
            for body in bodies:
                with self.subTest(verify_result=verify_result, body=body):
                    self.http.side_effect = [
                        self.http_response({"result": verify_result}),
                        self.http_response(body=body),
                    ]
                    with self.assertLogs("payments.services", level="ERROR") as logs:
                        with self.assertRaises(PaymentError):
                            process_callback(self.payment, self.callback_data(self.payment))
                    self.assertNotIn(sensitive, "\n".join(logs.output))
                    self.assert_unpaid()

    def test_malformed_paid_timestamp_preserves_receipt_for_manual_review(self):
        original = self.server_transactions[self.payment.authority].copy()
        for paid_at in (None, "", "invalid-date", "2026-02-30T10:30:00", 100, True, {}):
            with self.subTest(paid_at=paid_at):
                self.http.side_effect = [
                    self.http_response({**original, "status": 1, "paidAt": paid_at}),
                ]
                payment = process_callback(self.payment, self.callback_data(self.payment))
                self.assertEqual(payment.status, PaymentStatus.MANUAL_REVIEW)
                self.assertFalse(payment.needs_refund)
                self.assertEqual(payment.metadata["verification"]["paidAt"], paid_at)
                self.order.refresh_from_db()
                self.assertFalse(self.order.is_paid)
                self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
                self.assertEqual(payment.metadata["verification"]["payment_time"]["reason"], "invalid_timestamp")
                self.assertEqual(payment.metadata["decision"]["reason"], "invalid_timestamp")
                self.assertIsNotNone(payment.paid_at)

    def test_malformed_verification_response_does_not_pay_order(self):
        for body in (b"invalid", b"[]", b'{"result":true}', b'{"result":100}', b"\xff"):
            with self.subTest(body=body):
                self.http.side_effect = None
                self.http.return_value = self.http_response(body=body)
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

    def test_success_after_order_expiry_flags_refund_without_restoring_order(self):
        from orders.services import expire_due_orders
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(expire_due_orders(), 1)
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertTrue(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.payment_status, PaymentStatus.EXPIRED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_success_after_deadline_before_expiry_job_requires_refund(self):
        self.server_transactions[self.payment.authority]["refNumber"] = 123456789012345
        deadline = timezone.now() - timedelta(minutes=1)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertTrue(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.PENDING)
        self.assertEqual(self.order.expires_at, deadline)
        self.assertIsNone(self.order.paid_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_local_payment_expiry_does_not_hide_actual_gateway_payment(self):
        Payment.objects.filter(pk=self.payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(expire_stale_payments(), 1)
        payment = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)

    def test_two_successful_attempts_do_not_pay_order_twice(self):
        second = self.historical_payment()
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            first = process_callback(self.payment, self.callback_data(self.payment))
            extra = process_callback(second, self.callback_data(second))
        self.assertEqual(mark_paid.call_count, 1)
        self.assertFalse(first.needs_refund)
        self.assertTrue(extra.needs_refund)
        self.assertEqual(extra.status, PaymentStatus.SUCCESSFUL)
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, first.paid_at)
        self.assertEqual(Payment.objects.filter(order=self.order, status=PaymentStatus.SUCCESSFUL, needs_refund=False).count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_three_successful_attempts_and_repeated_callbacks_finalize_once(self):
        payments = [self.payment, self.historical_payment(), self.historical_payment()]
        self.variant.refresh_from_db()
        reserved_stock = self.variant.stock
        self.assertEqual(reserved_stock, 4)
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            recovered = [process_callback(payment, self.callback_data(payment)) for payment in payments]
            paid_at = recovered[0].paid_at
            calls = self.http.call_count
            for _ in range(2):
                for payment in recovered:
                    repeated = process_callback(payment, self.callback_data(payment))
                    self.assertEqual(repeated.reference_id, payment.reference_id)
                    self.assertEqual(repeated.paid_at, payment.paid_at)
                    self.assertEqual(repeated.needs_refund, payment.needs_refund)
            self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(self.http.call_count, calls)
        self.assertFalse(recovered[0].needs_refund)
        self.assertTrue(all(payment.needs_refund for payment in recovered[1:]))
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.SUCCESSFUL).count(), 3)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.SUCCESSFUL, needs_refund=False).count(), 1)
        self.assertEqual(self.order.payments.filter(needs_refund=True).count(), 2)
        self.assertEqual(list(self.order.payments.values_list("callback_count", flat=True)), [3, 3, 3])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(self.order.paid_at, paid_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, reserved_stock)

    def test_later_failed_attempt_does_not_downgrade_paid_order(self):
        second = self.historical_payment()
        process_callback(self.payment, self.callback_data(self.payment))
        self.set_outcome(second, "failed")
        process_callback(second, self.callback_data(second, "failed"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)

    def test_unpaid_response_with_changed_paid_state_stays_retryable(self):
        for status in (1, 2):
            with self.subTest(status=status):
                inquiry = {**self.server_transactions[self.payment.authority], "status": status}
                self.http.side_effect = [
                    self.http_response({"result": 202}),
                    self.http_response(inquiry),
                ]
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

    def test_gateway_declined_or_reversed_statuses_never_pay_order(self):
        for status in (-2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 18, 21):
            with self.subTest(status=status):
                self.server_transactions[self.payment.authority]["status"] = status
                payment = process_callback(self.payment, self.callback_data(self.payment))
                self.assertEqual(payment.status, PaymentStatus.FAILED)
                self.order.refresh_from_db()
                self.assertFalse(self.order.is_paid)


class CallbackViewTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client.force_login(self.customer)

    def callback_url(self, outcome="successful"):
        return reverse("payments:callback") + "?" + self.callback_query(self.payment, outcome)

    def test_callback_redirects_to_success_page(self):
        response = self.client.get(self.callback_url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))

    def test_callback_does_not_require_customer_session(self):
        self.client.logout()
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)

    def test_missing_or_invalid_callback_does_not_change_payment(self):
        valid = self.callback_data(self.payment)
        cases = [{}, {"authority": self.payment.authority}]
        cases.extend({key: value for key, value in valid.items() if key != missing} for missing in valid)
        cases.extend({**valid, "trackId": value} for value in ("", "0", "-1", "1.0", "01", "۱۲۳", str(2**63)))
        cases.extend(({**valid, "success": "yes"}, {**valid, "status": "999"}, {**valid, "orderId": "UNKNOWN"}))
        calls = self.http.call_count
        for data in cases:
            with self.subTest(data=data):
                response = self.client.get(reverse("payments:callback"), data)
                self.assertEqual(response.url, reverse("store:home"))
                self.order.refresh_from_db()
                self.payment.refresh_from_db()
                self.assertFalse(self.order.is_paid)
                self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
                self.assertEqual(self.payment.status, PaymentStatus.PENDING)
                self.assertEqual(self.payment.callback_count, 0)
        self.assertEqual(self.http.call_count, calls)

    def test_duplicate_callback_parameters_are_rejected(self):
        valid = self.callback_query(self.payment)
        calls = self.http.call_count
        for key, value in self.callback_data(self.payment).items():
            with self.subTest(key=key):
                response = self.client.get(reverse("payments:callback") + "?" + valid + "&" + urlencode({key: value}))
                self.assertEqual(response.url, reverse("store:home"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.callback_count, 0)
        self.assertEqual(self.http.call_count, calls)

    def test_unknown_track_id_does_not_find_payment_by_order_id(self):
        data = {**self.callback_data(self.payment), "trackId": "9900"}
        response = self.client.get(reverse("payments:callback"), data)
        self.assertEqual(response.url, reverse("store:home"))
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_post_callback_is_rejected_for_normal_zibal_flow(self):
        response = self.client.post(reverse("payments:callback"), self.callback_data(self.payment))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_duplicate_callback_keeps_same_result(self):
        self.client.get(self.callback_url())
        calls = self.http.call_count
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))
        self.assertEqual(self.http.call_count, calls)
        self.assertEqual(Payment.objects.filter(order=self.order, status=PaymentStatus.SUCCESSFUL).count(), 1)

    def test_callback_network_error_preserves_pending_payment(self):
        self.http.side_effect = TimeoutError("timeout")
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertFalse(self.order.is_paid)

    def test_pending_transaction_returns_to_order(self):
        self.set_outcome(self.payment, "pending")
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_refund_transaction_returns_to_order(self):
        from orders.services import cancel_order
        cancel_order(self.order)
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.needs_refund)

    def test_redirect_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_redirect_page_preserves_checkout_and_sends_zibal_referer(self):
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"https://gateway.zibal.ir/start/{self.payment.authority}")
        self.assertContains(response, 'http-equiv="refresh"')
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        home = self.client.get(reverse("store:home"))
        self.assertEqual(home.headers["Referrer-Policy"], "same-origin")

    def test_redirect_target_is_not_taken_from_tampered_metadata(self):
        self.payment.metadata["redirect_url"] = "https://attacker.example"
        self.payment.save(update_fields=["metadata"])
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertContains(response, f"https://gateway.zibal.ir/start/{self.payment.authority}")
        self.assertNotContains(response, "attacker.example")

    def test_pending_redirect_is_blocked_after_order_cancellation(self):
        from orders.services import cancel_order
        cancel_order(self.order)
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_pending_redirect_is_blocked_after_deadline(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_pending_redirect_is_blocked_by_another_manual_receipt(self):
        other = self.historical_payment()
        Payment.objects.filter(pk=other.pk).update(status=PaymentStatus.MANUAL_REVIEW)
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_uncertain_attempt_cannot_expose_a_gateway_url(self):
        Payment.objects.filter(pk=self.payment.pk).update(creation_status=PaymentCreationStatus.UNCERTAIN)
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_forged_correlated_callback_cannot_bind_unknown_attempt(self):
        Payment.objects.filter(pk=self.payment.pk).update(authority=None, creation_status=PaymentCreationStatus.UNCERTAIN)
        self.server_transactions[self.payment.authority]["amount"] += 10
        response = self.client.get(reverse("payments:callback_request", args=[self.payment.request_id]),
                                   self.callback_data(self.payment))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.payment.refresh_from_db()
        self.assertIsNone(self.payment.authority)
        self.assertEqual(self.payment.creation_status, PaymentCreationStatus.UNCERTAIN)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_request_path_for_another_attempt_cannot_overwrite_known_track(self):
        response = self.client.get(reverse("payments:callback_request", args=[uuid4()]),
                                   self.callback_data(self.payment))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_paid_payment_redirects_to_result(self):
        self.client.get(self.callback_url())
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))

    def test_foreign_user_cannot_see_redirect(self):
        other = User.objects.create_user(phone_number="09121112233")
        self.client.force_login(other)
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_historical_payment_records_are_not_processed_by_zibal_callback(self):
        Payment.objects.filter(pk=self.payment.pk).update(gateway="legacy-provider")
        response = self.client.get(self.callback_url())
        self.assertEqual(response.url, reverse("store:home"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.gateway, "legacy-provider")
        self.assertEqual(self.payment.amount, self.order.total)
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_test_fixture_and_obsolete_gateway_routes_are_not_public(self):
        calls = self.http.call_count
        for path in (
            "/__qa__/zibal/return/" + self.payment.authority + "/successful/",
            "/payments/mock/gateway/" + self.payment.authority + "/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.http.call_count, calls)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)


class ResultPageTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client.force_login(self.customer)

    def pay(self, outcome="successful"):
        self.set_outcome(self.payment, outcome)
        return self.client.get(reverse("payments:callback") + "?" + self.callback_query(self.payment, outcome))

    def test_success_page_displays_verified_reference(self):
        self.pay()
        self.payment.refresh_from_db()
        response = self.client.get(reverse("payments:result_success", args=[self.order.order_number]))
        self.assertContains(response, "پرداخت با موفقیت انجام شد")
        self.assertContains(response, self.payment.reference_id)

    def test_success_page_requires_actual_payment(self):
        response = self.client.get(reverse("payments:result_success", args=[self.order.order_number]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))

    def test_failed_page(self):
        self.pay("failed")
        response = self.client.get(reverse("payments:result_failed", args=[self.order.order_number]))
        self.assertContains(response, "پرداخت ناموفق بود")

    def test_cancelled_page(self):
        self.pay("cancelled")
        response = self.client.get(reverse("payments:result_cancelled", args=[self.order.order_number]))
        self.assertContains(response, "از پرداخت انصراف دادید")

    def test_cancelled_page_redirects_for_paid_order(self):
        self.pay()
        response = self.client.get(reverse("payments:result_cancelled", args=[self.order.order_number]))
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))

    def test_result_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("payments:result_success", args=[self.order.order_number]))
        self.assertIn(reverse("accounts:login"), response.url)

    def test_foreign_user_cannot_see_results(self):
        other = User.objects.create_user(phone_number="09123334455")
        self.client.force_login(other)
        response = self.client.get(reverse("payments:result_failed", args=[self.order.order_number]))
        self.assertEqual(response.status_code, 404)

    def test_historical_success_remains_visible_without_old_gateway_code(self):
        mark_order_paid(self.order)
        Payment.objects.filter(pk=self.payment.pk).update(
            gateway="legacy-provider", status=PaymentStatus.SUCCESSFUL,
            transaction_id="LEGACY-TRANSACTION", reference_id="LEGACY-REFERENCE",
        )
        calls = self.http.call_count
        response = self.client.get(reverse("payments:result_success", args=[self.order.order_number]))
        self.assertContains(response, "LEGACY-REFERENCE")
        self.assertEqual(self.http.call_count, calls)


class FullPurchaseFlowTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.client.force_login(self.customer)

    def checkout(self):
        response = self.client.get(reverse("orders:checkout"))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("orders:checkout"), {"action": "select", "address_id": self.address.pk})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("orders:checkout_review"), {"note": "تست کامل"})
        self.assertEqual(response.status_code, 302)
        return response, Order.objects.filter(customer=self.customer).latest("id")

    def test_cart_checkout_payment_callback_and_result(self):
        self.client.post(reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1, "next": "/"})
        self.assertEqual(CartItem.objects.first().quantity, 1)
        response, order = self.checkout()
        self.assertEqual(order.customer_note, "تست کامل")
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        payment = order.payments.get()
        self.assertEqual(response.url, reverse("payments:redirect", args=[payment.pk]))
        response = self.client.get(response.url)
        self.assertContains(response, f"https://gateway.zibal.ir/start/{payment.authority}")
        self.assertEqual(payment.amount, order.total)
        callback_url = reverse("payments:callback") + "?" + self.callback_query(payment)
        response = self.client.get(callback_url)
        self.assertEqual(response.url, reverse("payments:result_success", args=[order.order_number]))
        self.assertContains(self.client.get(response.url), "پرداخت با موفقیت انجام شد")
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, OrderStatus.PAID)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.assertEqual(CartItem.objects.count(), 0)
        self.client.get(callback_url)
        self.assertEqual(Payment.objects.filter(order=order, status=PaymentStatus.SUCCESSFUL).count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

    def test_checkout_timeout_preserves_attempt_and_blocks_blind_retry(self):
        self.client.post(reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1})
        self.http.side_effect = URLError("offline")
        response, order = self.checkout()
        self.assertEqual(response.url, reverse("orders:detail", args=[order.order_number]))
        self.assertEqual(order.payments.count(), 1)
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertFalse(order.is_paid)
        self.assertEqual(CartItem.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.http.side_effect = self.api_call
        response = self.client.post(reverse("orders:pay", args=[order.order_number]), {"amount": "1"})
        payment = order.payments.get()
        self.assertEqual(response.url, reverse("orders:detail", args=[order.order_number]))
        self.assertEqual(payment.creation_status, PaymentCreationStatus.UNCERTAIN)
        self.assertIsNone(payment.authority)
        self.assertEqual(payment.amount, order.total)
        self.assertEqual(len(self.api_calls("/v1/request")), 1)


class RecoveryCommandTests(PaymentFixtureMixin, PaymentTestCase):
    """Recovery is exercised only with mocked APIs and a synthetic test SQL dump."""

    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payments = [self.historical_payment() for _ in range(3)]
        self.payment = self.payments[0]
        self.temp_dir = self.enterContext(TemporaryDirectory(prefix="minikandoo-zibal-recovery-test-"))
        self.backup = Path(self.temp_dir) / "synthetic-test-only.sql.gz"
        self.dump = (
            f"-- MariaDB dump\n-- Host: localhost    Database: {connection.settings_dict['NAME']}\n"
            "CREATE TABLE `payments_payment` (id int);\n"
            "CREATE TABLE `orders_order` (id int);\n"
            "CREATE TABLE `products_productvariant` (id int);\n"
            "-- Dump completed on 2026-11-15\n"
        )
        self.backup.write_bytes(gzip.compress(self.dump.encode()))
        self.write_checksum()
        for payment, paid_at in zip(self.payments, (
            "2026-11-14T10:33:00Z", "2026-11-14T10:31:00Z", "2026-11-14T10:32:00Z",
        )):
            self.server_transactions[payment.authority].update({
                "status": 1, "paidAt": paid_at, "refNumber": "00" + str(int(payment.authority) + 100),
            })
        self.http.reset_mock()

    def write_checksum(self):
        digest = hashlib.sha256(self.backup.read_bytes()).hexdigest()
        Path(str(self.backup) + ".sha256").write_text(f"{digest}  {self.backup.name}\n")

    def reconcile(self, *, apply=False, backup=None, track_ids=None, gateway_timezone=None, request_id=None):
        self.command_output = io.StringIO()
        call_command(
            "reconcile_zibal_payments", order_number=self.order.order_number,
            track_id=track_ids or [payment.authority for payment in reversed(self.payments)],
            apply=apply, backup=str(backup) if backup else None,
            gateway_timezone=gateway_timezone, stdout=self.command_output,
            request_id=request_id,
        )
        return self.report()

    def report(self):
        return json.loads(self.command_output.getvalue())

    def assert_all_pending(self):
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.PENDING).count(), 3)
        self.assertEqual(list(self.order.payments.values_list("callback_count", flat=True)), [0, 0, 0])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_inspection_is_read_only_and_sanitizes_gateway_and_customer_data(self):
        report = self.reconcile()
        self.assertEqual(report["before"], report["after"])
        self.assertEqual(report["planned_primary"], self.payments[1].authority)
        self.assertEqual(report["stored_amount_unit"], "toman")
        self.assertEqual(report["gateway_amount_unit"], "rial")
        self.assertEqual(report["gateway"][self.payment.authority]["amount_rial"], int(self.order.total) * 10)
        self.assertEqual(report["gateway_time_order"], [self.payments[i].authority for i in (1, 2, 0)])
        self.assertEqual(len(self.api_calls("/v1/inquiry")), 3)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)
        self.assertEqual(len(self.api_calls("/v1/request")), 0)
        self.assert_all_pending()
        output = self.command_output.getvalue()
        for sensitive in ("cardNumber", "62741****44", "receiver_phone", self.customer.phone_number):
            self.assertNotIn(sensitive, output)

    def test_apply_requires_backup_before_api_calls_or_local_writes(self):
        with self.assertRaises(CommandError):
            self.reconcile(apply=True)
        self.http.assert_not_called()
        self.assert_all_pending()

    def test_invalid_backups_stop_recovery_before_api_calls(self):
        bad_dumps = (
            b"", b"not gzip", gzip.compress(self.dump.encode())[:-8],
            gzip.compress(self.dump.replace("-- Dump completed on 2026-11-15\n", "").encode()),
            gzip.compress(self.dump.replace("CREATE TABLE `orders_order` (id int);\n", "").encode()),
            gzip.compress(self.dump.replace(
                str(connection.settings_dict["NAME"]), str(connection.settings_dict["NAME"]) + "-other",
            ).encode()),
        )
        for contents in bad_dumps:
            with self.subTest(contents=contents[:30]):
                self.backup.write_bytes(contents)
                self.write_checksum()
                with self.assertRaises(CommandError):
                    self.reconcile(apply=True, backup=self.backup)
                self.http.assert_not_called()
                self.assert_all_pending()

    def test_old_or_future_backup_is_rejected(self):
        for offset in (-7200, 60):
            with self.subTest(offset=offset):
                stamp = timezone.now().timestamp() + offset
                os.utime(self.backup, (stamp, stamp))
                with self.assertRaises(CommandError):
                    self.reconcile(apply=True, backup=self.backup)
                self.http.assert_not_called()
                self.assert_all_pending()

    def test_recovery_replays_by_gateway_time_and_is_idempotent(self):
        target = "payments.management.commands.reconcile_zibal_payments.process_callback"
        with patch(target, wraps=process_callback) as replay, patch(
            "payments.services.mark_order_paid", wraps=mark_order_paid,
        ) as mark_paid:
            report = self.reconcile(apply=True, backup=self.backup)
            self.assertEqual([call.args[0].authority for call in replay.call_args_list], [
                self.payments[i].authority for i in (1, 2, 0)
            ])
            self.assertEqual(mark_paid.call_count, 1)
            repeated = self.reconcile(apply=True, backup=self.backup)
            self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(report["successful_count"], 3)
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])
        self.assertEqual(len(report["refund_required"]), 2)
        self.assertTrue(report["inventory_unchanged"])
        self.assertEqual(report["after"]["status"], OrderStatus.PAID)
        self.assertEqual(report["after"]["payment_status"], PaymentStatus.SUCCESSFUL)
        self.assertEqual(repeated["after"]["paid_at"], report["after"]["paid_at"])
        self.assertEqual(repeated["actual_primary"], report["actual_primary"])
        self.assertEqual(len(self.api_calls("/v1/verify")), 3)
        self.assertTrue(all(row["verify_result"] == 201 for row in report["replayed"]))
        self.assertTrue(all(row["reference_id"].startswith("00") for row in report["replayed"]))
        self.assertEqual(len(self.api_calls("/v1/request")), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_gateway_time_ties_require_manual_primary_selection(self):
        for payment in self.payments:
            self.server_transactions[payment.authority]["paidAt"] = "2026-11-14T10:31:00Z"
        report = self.reconcile()
        self.assertIsNone(report["planned_primary"])
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).count(), 3)


    def test_existing_valid_primary_is_preserved_and_all_new_payments_need_refunds(self):
        prior = self.historical_payment()
        process_callback(prior, self.callback_data(prior))
        self.order.refresh_from_db()
        original_paid_at = self.order.paid_at
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            report = self.reconcile(apply=True, backup=self.backup)
        mark_paid.assert_not_called()
        self.assertEqual(report["actual_primary"], [prior.authority])
        self.assertEqual(report["planned_primary"], prior.authority)
        self.assertEqual(report["successful_count"], 4)
        self.assertEqual(len(report["refund_required"]), 3)
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, original_paid_at)
        self.assertTrue(report["inventory_unchanged"])

    def test_another_gateway_paid_transaction_requires_explicit_selection(self):
        other = self.historical_payment()
        self.server_transactions[other.authority]["status"] = 1
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        report = self.report()
        self.assertTrue(report["blocking_issues"])
        self.assertEqual(report["before"], report["after"])
        self.assertEqual(report["replayed"], [])
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_inconsistent_previous_success_blocks_recovery(self):
        process_callback(self.payment, self.callback_data(self.payment))
        Payment.objects.filter(pk=self.payment.pk).update(reference_id="999999")
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        report = self.report()
        self.assertTrue(report["blocking_issues"])
        self.assertEqual(report["before"], report["after"])
        self.assertEqual(report["replayed"], [])

    def test_historical_success_is_not_accepted_as_zibal_evidence(self):
        prior = self.historical_payment()
        Payment.objects.filter(pk=prior.pk).update(gateway="legacy-provider", status=PaymentStatus.SUCCESSFUL)
        mark_order_paid(self.order)
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        report = self.report()
        self.assertTrue(report["blocking_issues"])
        self.assertEqual(report["replayed"], [])
        self.assertEqual(report["before"], report["after"])

    def test_cancelled_order_remains_cancelled_and_every_payment_needs_refund(self):
        from orders.services import cancel_order
        cancel_order(self.order)
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["actual_primary"], [])
        self.assertEqual(report["after"]["status"], OrderStatus.CANCELLED)
        self.assertEqual(len(report["refund_required"]), 3)
        self.assertTrue(report["inventory_unchanged"])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_elapsed_deadline_is_not_cleared_or_marked_paid_by_recovery(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        for payment in self.payments:
            self.server_transactions[payment.authority]["paidAt"] = timezone.now().isoformat()
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["actual_primary"], [])
        self.assertEqual(report["after"]["status"], OrderStatus.PENDING_PAYMENT)
        self.assertEqual(report["after"]["payment_status"], PaymentStatus.PENDING)
        self.assertEqual(len(report["refund_required"]), 3)
        self.assertEqual(report["before"]["expires_at"], report["after"]["expires_at"])

    def test_mismatched_target_blocks_primary_and_preserves_other_receipts(self):
        self.server_transactions[self.payments[0].authority]["amount"] += 10
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.assertEqual(Payment.objects.get(pk=self.payments[0].pk).status, PaymentStatus.PENDING)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).count(), 2)
        self.assertFalse(self.order.payments.filter(needs_refund=True).exists())


    def test_one_replay_timeout_does_not_corrupt_other_transactions(self):
        earliest = self.payments[1]

        def api_with_timeout(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify" and json.loads(request.data)["trackId"] == int(earliest.authority):
                raise TimeoutError("unit-test-sensitive-merchant")
            return self.api_call(request, timeout=timeout)

        self.http.side_effect = api_with_timeout
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        report = self.report()
        earliest.refresh_from_db()
        self.assertEqual(earliest.status, PaymentStatus.PENDING)
        self.assertEqual(earliest.callback_count, 0)
        self.assertEqual(report["successful_count"], 0)
        self.assertEqual(report["actual_primary"], [])
        self.assertEqual(len(report["manual_review_required"]), 2)
        self.assertNotIn("unit-test-sensitive-merchant", self.command_output.getvalue())
        self.assertTrue(report["inventory_unchanged"])

    def test_gateway_change_during_replay_is_reported_as_incomplete(self):
        earliest = self.payments[1]

        def api_with_changed_status(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify" and json.loads(request.data)["trackId"] == int(earliest.authority):
                self.server_transactions[earliest.authority]["status"] = 5
            return self.api_call(request, timeout=timeout)

        self.http.side_effect = api_with_changed_status
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(self.report()["successful_count"], 0)
        self.assertEqual(len(self.report()["manual_review_required"]), 2)
        earliest.refresh_from_db()
        self.assertEqual(earliest.status, PaymentStatus.FAILED)

    def test_mixed_gateway_timestamp_formats_need_a_confirmed_timezone(self):
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:33:00"
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)
        self.assertEqual(self.report()["actual_primary"], [])
        self.assertEqual(len(self.report()["manual_review_required"]), 3)
        report = self.reconcile(apply=True, backup=self.backup, gateway_timezone="Asia/Tehran")
        self.assertEqual(report["successful_count"], 3)
        self.assertEqual(report["actual_primary"], [self.payment.authority])

    def test_missing_recorded_checksum_stops_apply_before_inquiry(self):
        Path(str(self.backup) + ".sha256").unlink()
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.http.assert_not_called()
        self.assert_all_pending()

    def test_incorrect_or_mismatched_sidecar_stops_apply(self):
        values = (
            "0" * 64 + "  " + self.backup.name + "\n", "bad sidecar",
            hashlib.sha256(self.backup.read_bytes()).hexdigest() + "  another.sql.gz\n",
        )
        for contents in values:
            with self.subTest(contents=contents[:8]):
                Path(str(self.backup) + ".sha256").write_text(contents)
                with self.assertRaises(CommandError):
                    self.reconcile(apply=True, backup=self.backup)
                self.http.assert_not_called()
                self.assert_all_pending()

    def test_uniform_naive_timestamps_are_read_only_then_all_manual_on_apply(self):
        for payment in self.payments:
            self.server_transactions[payment.authority]["paidAt"] = "2026-11-14T10:30:00"
        report = self.reconcile()
        self.assertEqual(report["before"], report["after"])
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(len(report["ambiguous_timestamps"]), 3)
        self.assertEqual(report["gateway_time_order"], [])
        for _ in range(2):
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup)
            self.assertEqual(self.report()["actual_primary"], [])
            self.assertEqual(self.report()["refund_required"], [])
            self.assertEqual(len(self.report()["manual_review_required"]), 3)
            self.assertTrue(self.report()["inventory_unchanged"])

    def test_recovery_accepts_early_receipt_after_local_deadline(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertFalse(report["before"]["can_pay"])
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])
        self.assertEqual(len(report["refund_required"]), 2)
        self.assertTrue(report["inventory_unchanged"])
        self.assertTrue(all(payment.metadata["recovery_history"] for payment in self.order.payments.all()))

    def test_primary_timeout_retry_finalizes_once_and_refunds_waiting_receipts(self):
        earliest = self.payments[1]

        def fail_primary(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify" and json.loads(request.data)["trackId"] == int(earliest.authority):
                raise TimeoutError("synthetic private data")
            return self.api_call(request, timeout=timeout)

        self.http.side_effect = fail_primary
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(self.report()["actual_primary"], [])
        self.http.side_effect = self.api_call
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(report["actual_primary"], [earliest.authority])
        self.assertEqual(report["successful_count"], 3)
        self.assertEqual(len(report["refund_required"]), 2)
        again = self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(again["actual_primary"], report["actual_primary"])
        self.assertEqual(again["after"]["paid_at"], report["after"]["paid_at"])

    def test_timestamp_becoming_ambiguous_during_replay_cannot_promote_other_receipts(self):
        earliest = self.payments[1]

        def change_primary_time(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify" and json.loads(request.data)["trackId"] == int(earliest.authority):
                self.server_transactions[earliest.authority]["paidAt"] = "2026-11-14T10:31:00"
            return self.api_call(request, timeout=timeout)

        self.http.side_effect = change_primary_time
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(self.report()["actual_primary"], [])
        self.assertEqual(len(self.report()["manual_review_required"]), 3)
        self.assertTrue(self.report()["inventory_unchanged"])

    def test_invalid_timezone_is_rejected_without_queries_or_changes(self):
        with self.assertRaises(CommandError):
            self.reconcile(gateway_timezone="Not/A_Timezone")
        self.http.assert_not_called()
        self.assert_all_pending()

    def test_foreign_or_duplicate_track_ids_are_rejected_without_api_calls(self):
        for track_ids in ([self.payment.authority] * 2, ["9900"]):
            with self.subTest(track_ids=track_ids), self.assertRaises(CommandError):
                self.reconcile(track_ids=track_ids)
            self.http.assert_not_called()
            self.assert_all_pending()


    def manual_receipt(self, payment, *, keep_ambiguous=False):
        original = self.server_transactions[payment.authority]["paidAt"]
        self.server_transactions[payment.authority]["paidAt"] = original.removesuffix("Z")
        result = process_callback(payment, self.callback_data(payment))
        self.assertEqual(result.status, PaymentStatus.MANUAL_REVIEW)
        if not keep_ambiguous:
            self.server_transactions[payment.authority]["paidAt"] = original
        return result

    def test_unselected_manual_receipt_blocks_primary_even_after_valid_inquiry(self):
        self.manual_receipt(self.payments[0])
        report = self.reconcile(track_ids=[p.authority for p in self.payments[1:]])
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["unselected_manual_review_receipts"], [self.payments[0].authority])
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup, track_ids=[p.authority for p in self.payments[1:]])
        paid.assert_not_called()
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_ambiguous_manual_receipt_blocks_another_valid_primary(self):
        self.manual_receipt(self.payments[0], keep_ambiguous=True)
        report = self.reconcile()
        self.assertIsNone(report["planned_primary"])
        self.assertIn(self.payments[0].authority, report["ambiguous_timestamps"])
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup)
        paid.assert_not_called()
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.MANUAL_REVIEW).count(), 3)
        self.assertFalse(self.order.payments.filter(needs_refund=True).exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_complete_preflight_authorizes_only_primary_and_records_plan(self):
        self.manual_receipt(self.payments[0])
        self.manual_receipt(self.payments[1])
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])
        primary = Payment.objects.get(pk=self.payments[1].pk)
        audit = primary.metadata["recovery_history"][-1]["recovery_plan"]
        self.assertEqual(audit["inspected_payment_ids"], sorted(p.pk for p in self.payments))
        self.assertEqual(audit["primary"], primary.authority)
        self.assertEqual(len(audit["receipts"]), 3)
        self.assertEqual(audit["backup_sha256"], hashlib.sha256(self.backup.read_bytes()).hexdigest())
        self.assertEqual(self.order.payments.filter(needs_refund=True).count(), 2)
        self.reconcile(apply=True, backup=self.backup)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_test_only_confirmed_timezone_recovers_existing_receipts_without_replacing_their_audits(self):
        # Asia/Tehran is synthetic confirmation here, not a claim about Zibal.
        primary = self.payments[1]
        primary.request_id = uuid4()
        primary.metadata["incident_note"] = "synthetic original audit"
        primary.save(update_fields=["request_id", "metadata"])
        originals = {}
        for payment in self.payments:
            self.server_transactions[payment.authority]["paidAt"] = self.server_transactions[payment.authority]["paidAt"].removesuffix("Z")
            stored = process_callback(payment, self.callback_data(payment))
            originals[stored.pk] = stored
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            report = self.reconcile(apply=True, backup=self.backup, gateway_timezone="Asia/Tehran")
            repeated = self.reconcile(apply=True, backup=self.backup, gateway_timezone="Asia/Tehran")
        paid.assert_called_once()
        self.assertEqual(report["actual_primary"], [primary.authority])
        self.assertEqual(repeated["actual_primary"], report["actual_primary"])
        self.assertEqual(self.order.payments.count(), 3)
        for stored in self.order.payments.all():
            original = originals[stored.pk]
            for field in ("authority", "order_id", "request_id", "amount", "reference_id", "transaction_id", "paid_at"):
                self.assertEqual(getattr(stored, field), getattr(original, field))
            self.assertEqual(stored.metadata["verification"]["paidAt"], original.metadata["verification"]["paidAt"])
            self.assertIn(original.metadata["verification"], [entry["data"] for entry in stored.metadata["verification_history"]])
            self.assertIn(original.metadata["decision"], [entry["data"] for entry in stored.metadata["decision_history"]])
            self.assertEqual(stored.metadata["verification"]["payment_time"]["source"], "configuration")
            self.assertTrue(stored.metadata["recovery_history"])
        primary.refresh_from_db()
        self.assertEqual(primary.gateway_paid_at.isoformat(), "2026-11-14T07:01:00+00:00")
        self.assertEqual(primary.metadata["incident_note"], "synthetic original audit")
        self.assertEqual(primary.status, PaymentStatus.SUCCESSFUL)
        self.assertFalse(primary.needs_refund)
        self.assertEqual(len(self.api_calls("/v1/request")), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_archived_conflict_blocks_recovery_even_if_latest_decision_is_only_timezone_review(self):
        receipt = self.manual_receipt(self.payment)
        receipt.metadata["decision_history"].append({"data": {"action": "manual_review", "reason": "receipt_identifiers_changed"}})
        receipt.save(update_fields=["metadata"])
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup)
        paid.assert_not_called()
        self.assertIsNone(self.report()["planned_primary"])
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_receipt_evidence_in_a_nonfinal_status_blocks_incomplete_inquiry(self):
        Payment.objects.filter(pk=self.payments[0].pk).update(
            status=PaymentStatus.FAILED, gateway_paid_at=timezone.now() - timedelta(minutes=1),
            metadata={"verification": {"result": 100, "status": 1}},
        )
        self.server_transactions[self.payments[0].authority]["status"] = 5
        report = self.reconcile()
        self.assertIsNone(report["planned_primary"])
        self.assertIn(self.payments[0].authority, report["potentially_successful_receipts"])
        self.assertIn(self.payments[0].authority, report["incomplete_inquiries"])
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_archived_success_evidence_cannot_be_hidden_by_a_later_failed_response(self):
        receipt = self.payments[0]
        Payment.objects.filter(pk=receipt.pk).update(
            status=PaymentStatus.FAILED, paid_at=None, gateway_paid_at=None,
            metadata={"verification": {"result": 100, "status": 5},
                      "verification_history": [{"data": {"result": 100, "status": 1}}]},
        )
        self.server_transactions[receipt.authority]["status"] = 5
        selected = [p.authority for p in self.payments[1:]]
        report = self.reconcile(track_ids=selected)
        self.assertIsNone(report["planned_primary"])
        self.assertIn(receipt.authority, report["potentially_successful_receipts"])
        self.assertTrue(any(receipt.authority in reason for reason in report["primary_selection_blockers"]))
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup, track_ids=selected)
        paid.assert_not_called()
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_unpaid_order_requires_explicit_selection_of_every_successful_receipt(self):
        receipt = self.payments[0]
        Payment.objects.filter(pk=receipt.pk).update(status=PaymentStatus.SUCCESSFUL, needs_refund=True)
        report = self.reconcile(track_ids=[p.authority for p in self.payments[1:]])
        self.assertIsNone(report["planned_primary"])
        self.assertIn(receipt.authority, report["unselected_potentially_successful_receipts"])

    def test_internal_authorization_cannot_choose_a_later_primary(self):
        from payments.management.commands.reconcile_zibal_payments import validate_backup
        from payments.services import _authorize_recovery_primary
        report = self.reconcile()
        backup = validate_backup(str(self.backup), connection.settings_dict["NAME"])
        gateway = get_gateway("zibal")
        verified = {p.authority: gateway.verify_payment(p, self.callback_data(p)) for p in self.payments}
        plan = report["recovery_plan"]
        plan["backup_sha256"] = backup["sha256"]
        plan["primary"] = self.payments[0].authority  # This receipt is later than the valid primary.
        with self.assertRaisesMessage(PaymentError, "unique earliest"):
            _authorize_recovery_primary(
                order=self.order, payments=self.payments, verified=verified,
                selected_track_ids=plan["selected_track_ids"], primary=self.payments[0],
                backup=backup, plan=plan,
            )

    def test_aware_timestamp_changing_during_verify_invalidates_primary_plan(self):
        original = self.api_call
        def changed(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify":
                authority = str(json.loads(request.data)["trackId"])
                self.server_transactions[authority]["paidAt"] = "2026-11-14T10:34:00Z"
            return original(request, timeout=timeout)
        self.http.side_effect = changed
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.assertFalse(self.order.payments.filter(needs_refund=True).exists())

    def creation_attempt(self, *, rejected=False):
        return Payment.objects.create(
            order=self.order, gateway="zibal", amount=self.order.total, request_id=uuid4(),
            creation_status=PaymentCreationStatus.REJECTED if rejected else PaymentCreationStatus.UNCERTAIN,
            status=PaymentStatus.FAILED if rejected else PaymentStatus.PENDING,
            metadata={"creation": {"responses": [{"outcome": "rejected" if rejected else "uncertain"}]}},
        )

    def remote_creation(self, attempt, *, paid_at=None):
        result = get_gateway().create_payment(self.order, request_id=attempt.request_id)
        self.server_transactions[result.authority].update(
            status=1, paidAt=paid_at or (timezone.now() - timedelta(seconds=2)).isoformat(),
        )
        return result.authority

    def test_unselected_unknown_creation_blocks_primary_and_reports_request_identity(self):
        attempt = self.creation_attempt()
        report = self.reconcile()
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["unresolved_creation_requests"][0]["request_id"], str(attempt.request_id))
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            with self.assertRaises(CommandError):
                self.reconcile(apply=True, backup=self.backup)
        paid.assert_not_called()
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.authority)

    def test_unknown_creation_inspection_does_not_bind_or_verify(self):
        attempt = self.creation_attempt()
        track_id = self.remote_creation(attempt)
        self.http.reset_mock()
        report = self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id])
        self.assertEqual(report["before"], report["after"])
        self.assertFalse(report["fulfillment_authorized"])
        attempt.refresh_from_db()
        self.assertIsNone(attempt.authority)
        self.assertEqual(len(self.api_calls("/v1/inquiry")), 1)
        self.assertEqual(len(self.api_calls("/v1/verify")), 0)

    def test_binding_request_does_not_fulfill_until_complete_recovery_preflight(self):
        attempt = self.creation_attempt()
        track_id = self.remote_creation(attempt)
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            report = self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id],
                                    apply=True, backup=self.backup)
        paid.assert_not_called()
        self.assertTrue(report["resolved"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.authority, track_id)
        self.assertEqual(attempt.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(attempt.needs_refund)
        self.assertIn("creation_resolution_plan", attempt.metadata["recovery_history"][0])
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)  # Omitted successful bound receipt still blocks.
        all_ids = [p.authority for p in self.payments] + [track_id]
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            final = self.reconcile(apply=True, backup=self.backup, track_ids=all_ids)
        paid.assert_called_once()
        self.assertEqual(final["actual_primary"], [self.payments[1].authority])
        self.assertEqual(len(final["refund_required"]), 3)
        self.assertTrue(final["inventory_unchanged"])

    def test_request_binding_preserves_ambiguous_receipt_without_automatic_refund(self):
        from orders.services import expire_due_orders
        attempt = self.creation_attempt()
        track_id = self.remote_creation(attempt, paid_at="2026-11-14T10:28:00")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id], apply=True, backup=self.backup)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentStatus.MANUAL_REVIEW)
        self.assertIsNone(attempt.gateway_paid_at)
        self.assertEqual(attempt.metadata["verification"]["paidAt"], "2026-11-14T10:28:00")
        self.assertFalse(attempt.needs_refund)
        self.assertEqual(expire_due_orders(), 0)
        all_ids = [p.authority for p in self.payments] + [track_id]
        report = self.reconcile(track_ids=all_ids)
        self.assertIsNone(report["planned_primary"])
        self.assertIn(track_id, report["ambiguous_timestamps"])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_request_binding_requires_valid_backup_before_gateway_operations(self):
        attempt = self.creation_attempt()
        track_id = self.remote_creation(attempt)
        self.http.reset_mock()
        with self.assertRaises(CommandError):
            self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id], apply=True)
        Path(str(self.backup) + ".sha256").write_text("0" * 64 + "  " + self.backup.name + "\n")
        with self.assertRaises(CommandError):
            self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id], apply=True, backup=self.backup)
        self.http.assert_not_called()
        self.assertIsNone(Payment.objects.get(pk=attempt.pk).authority)

    def test_request_binding_rejects_foreign_identity_and_multiple_selected_tracks(self):
        attempt = self.creation_attempt()
        track_id = self.remote_creation(attempt)
        for request_id, track_ids in ((str(uuid4()), [track_id]), (str(attempt.request_id), [track_id, self.payment.authority])):
            with self.subTest(request_id=request_id):
                with self.assertRaises(CommandError):
                    self.reconcile(request_id=request_id, track_ids=track_ids, apply=True, backup=self.backup)
        self.server_transactions[track_id]["orderId"] = "FOREIGN-ORDER"
        with self.assertRaises(CommandError):
            self.reconcile(request_id=str(attempt.request_id), track_ids=[track_id], apply=True, backup=self.backup)
        self.assertIsNone(Payment.objects.get(pk=attempt.pk).authority)

    def test_documented_rejected_request_does_not_block_valid_complete_receipt_recovery(self):
        attempt = self.creation_attempt(rejected=True)
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertEqual(report["unresolved_creation_requests"], [])
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])
        attempt.refresh_from_db()
        self.assertEqual(attempt.creation_status, PaymentCreationStatus.REJECTED)
        self.assertIsNone(attempt.authority)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(**TEST_SETTINGS)
class ConcurrentCreationTests(PaymentFixtureMixin, PaymentAPIStubMixin, TransactionTestCase):
    """Durable creation phases exercised with real MariaDB Order/Payment locks."""

    def setUp(self):
        super().setUp()
        self.build_payment_data()

    def creation(self):
        close_old_connections()
        try:
            return create_payment_for_order(Order.objects.get(pk=self.order.pk))
        finally:
            close_old_connections()

    def delayed_create(self, started, release, *, timeout_after_reply=False):
        def api(request, *, timeout):
            if urlsplit(request.full_url).path != "/v1/request":
                return self.api_call(request, timeout=timeout)
            self.assertFalse(connection.in_atomic_block)
            # The real remote transaction can exist before its response arrives.
            response = self.api_call(request, timeout=timeout)
            started.set()
            if not release.wait(10):
                raise AssertionError("Delayed creation test was not released")
            if timeout_after_reply:
                raise TimeoutError("synthetic uncertain creation outcome")
            return response
        return api

    def test_delayed_create_holds_neither_order_nor_payment_lock(self):
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                attempt = self.order.payments.get()
                self.assertEqual(attempt.creation_status, PaymentCreationStatus.REQUESTING)
                self.assertIsNone(attempt.authority)
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
                    Payment.objects.select_for_update(nowait=True).get(pk=attempt.pk)
            finally:
                release.set()
            self.assertEqual(future.result(20).creation_status, PaymentCreationStatus.READY)

    def test_expiration_commits_during_create_and_late_receipt_cannot_revive_order(self):
        from orders.services import expire_due_orders
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
                self.assertEqual(expire_due_orders(), 1)
            finally:
                release.set()
            with self.assertRaises(PaymentError):
                future.result(20)
        attempt = self.order.payments.get()
        self.assertEqual(attempt.creation_status, PaymentCreationStatus.REVIEW)
        self.assertIsNotNone(attempt.authority)
        self.http.side_effect = self.api_call
        receipt = process_callback(attempt, self.callback_data(attempt))
        self.assertTrue(receipt.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertFalse(self.order.is_paid)
        self.assertTrue(self.order.inventory_released)
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_two_simultaneous_submissions_send_only_one_remote_request(self):
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                second = pool.submit(self.creation)
                with self.assertRaises(PaymentError):
                    second.result(5)
                self.assertEqual(self.order.payments.count(), 1)
                self.assertEqual(len(self.api_calls("/v1/request")), 1)
            finally:
                release.set()
            payment = first.result(20)
        self.assertEqual(self.start_payment().pk, payment.pk)
        self.assertEqual(len(self.api_calls("/v1/request")), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_verified_callback_wins_before_create_finalization_and_is_not_overwritten(self):
        from orders.services import expire_due_orders
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release)
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                callback = {"trackId": track_id, "orderId": self.order.order_number,
                            "success": "1", "status": "2", "request_id": str(attempt.request_id)}
                receipt = process_callback(attempt, callback)
                saved_reference, saved_time = receipt.reference_id, receipt.paid_at
                self.assertTrue(receipt.is_successful)
                self.assertEqual(expire_due_orders(), 0)
            finally:
                release.set()
            finalized = future.result(20)
        paid.assert_called_once()
        self.assertEqual(finalized.authority, track_id)
        self.assertEqual(finalized.reference_id, saved_reference)
        self.assertEqual(finalized.paid_at, saved_time)
        self.assertEqual(finalized.callback_count, 1)
        self.assertTrue(finalized.is_successful)
        self.assertIn("creation", finalized.metadata)
        self.assertIn("verification_history", finalized.metadata)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_timeout_after_remote_create_is_durable_and_does_not_resend(self):
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release, timeout_after_reply=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
            finally:
                release.set()
            with self.assertRaises(PaymentError):
                future.result(20)
        attempt = self.order.payments.get()
        self.assertEqual(attempt.creation_status, PaymentCreationStatus.UNCERTAIN)
        with self.assertRaises(PaymentError):
            self.start_payment()
        self.assertEqual(len(self.api_calls("/v1/request")), 1)
        self.assertEqual(self.order.payments.count(), 1)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_manual_receipt_wins_while_creation_waits_and_keeps_reservation(self):
        from orders.services import expire_due_orders
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=self.order.pk)
                    Payment.objects.create(order=order, gateway="zibal", amount=order.total,
                                           authority="87654321", status=PaymentStatus.MANUAL_REVIEW,
                                           paid_at=timezone.now(), metadata={"verification": {"status": 1}})
                    order.expires_at = timezone.now() - timedelta(seconds=1)
                    order.save(update_fields=["expires_at"])
                self.assertEqual(expire_due_orders(), 0)
            finally:
                release.set()
            with self.assertRaises(PaymentError):
                future.result(20)
        self.assertFalse(Order.objects.get(pk=self.order.pk).inventory_released)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_uncertain_response_cannot_undo_a_callback_that_already_finalized(self):
        started, release = Event(), Event()
        self.http.side_effect = self.delayed_create(started, release, timeout_after_reply=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                receipt = process_callback(attempt, {
                    "trackId": track_id, "orderId": self.order.order_number,
                    "success": "1", "status": "2", "request_id": str(attempt.request_id),
                })
            finally:
                release.set()
            finalized = future.result(20)
        self.assertTrue(finalized.is_successful)
        self.assertEqual(finalized.reference_id, receipt.reference_id)
        self.assertEqual(finalized.creation_status, PaymentCreationStatus.READY)
        self.assertEqual(finalized.metadata["creation"]["responses"][0]["outcome"], "uncertain")

    def test_conflicting_create_response_preserves_already_finalized_gateway_identity(self):
        started, release = Event(), Event()
        original = self.delayed_create(started, release)
        def conflicting(request, *, timeout):
            response = original(request, timeout=timeout)
            if urlsplit(request.full_url).path == "/v1/request":
                return self.http_response({"result": 100, "trackId": 987654321})
            return response
        self.http.side_effect = conflicting
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.creation)
            try:
                self.assertTrue(started.wait(10))
                attempt = self.order.payments.get()
                track_id = next(iter(self.server_transactions))
                receipt = process_callback(attempt, {
                    "trackId": track_id, "orderId": self.order.order_number,
                    "success": "1", "status": "2", "request_id": str(attempt.request_id),
                })
            finally:
                release.set()
            with self.assertRaises(PaymentError):
                future.result(20)
        paid.assert_called_once()
        current = self.order.payments.get()
        self.assertTrue(current.is_successful)
        self.assertEqual(current.authority, track_id)
        self.assertEqual(current.transaction_id, receipt.transaction_id)
        self.assertEqual(current.reference_id, receipt.reference_id)
        self.assertEqual(current.creation_status, PaymentCreationStatus.REVIEW)
        self.assertEqual(current.metadata["creation"]["responses"][0]["track_id"], "987654321")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)


@skipUnlessDBFeature("has_select_for_update")
@override_settings(**TEST_SETTINGS)
class ConcurrentCallbackTests(PaymentFixtureMixin, PaymentAPIStubMixin, TransactionTestCase):
    """روی MariaDB/MySQL اجرا می‌شود؛ SQLite قفل ردیفی را شبیه‌سازی نمی‌کند."""

    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()

    def run_callbacks(self, payments):
        barrier = Barrier(len(payments))

        def callback(payment):
            close_old_connections()
            try:
                local = Payment.objects.select_related("order").get(pk=payment.pk)
                data = self.callback_data(local)
                barrier.wait(timeout=10)
                return process_callback(local, data)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(payments)) as executor:
            futures = [executor.submit(callback, payment) for payment in payments]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_duplicate_callbacks_apply_once(self):
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            self.run_callbacks([self.payment, self.payment])
        self.assertEqual(mark_paid.call_count, 1)
        self.assertIn(len(self.api_calls("/v1/verify")), (1, 2))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.callback_count, 2)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_concurrent_successful_attempts_only_pay_order_once(self):
        second = self.historical_payment()
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            self.run_callbacks([self.payment, second])
        self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.SUCCESSFUL, needs_refund=False).count(), 1)
        self.assertEqual(self.order.payments.filter(needs_refund=True).count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_concurrent_failed_attempt_cannot_downgrade_paid_order(self):
        second = self.historical_payment()
        self.set_outcome(second, "failed")
        self.run_callbacks([self.payment, second])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)

    def test_concurrent_identical_manual_callbacks_keep_one_order_hold_and_preserve_receipt(self):
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00"
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            self.run_callbacks([self.payment, self.payment])
        paid.assert_not_called()
        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.payment.callback_count, 2)
        self.assertEqual(self.payment.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(self.order.is_paid)
        self.assertFalse(self.payment.needs_refund)
        self.assertIsNone(self.payment.gateway_paid_at)
        self.assertEqual(self.payment.metadata["verification"]["paidAt"], "2026-11-14T10:28:00")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_failed_callback_after_concurrent_manual_winner_cannot_downgrade_order(self):
        from payments.services import _apply_verified_callback
        other = self.historical_payment()
        self.set_outcome(other, "failed")
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00"
        reviewed = Event()
        def apply(snapshot, *args, **kwargs):
            if snapshot.pk == other.pk and not reviewed.wait(10):
                raise AssertionError("Manual winner was not applied")
            result = _apply_verified_callback(snapshot, *args, **kwargs)
            if snapshot.pk == self.payment.pk:
                reviewed.set()
            return result
        with patch("payments.services._apply_verified_callback", side_effect=apply):
            self.run_callbacks([self.payment, other])
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertFalse(self.order.is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_cancellation_committing_during_verify_never_revives_order(self):
        from orders.services import cancel_order
        started, release = Event(), Event()
        def slow(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify":
                started.set()
                if not release.wait(10):
                    raise AssertionError("Cancellation race was not released")
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = slow
        def callback():
            close_old_connections()
            try:
                return process_callback(self.payment, self.callback_data(self.payment))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(callback)
            try:
                self.assertTrue(started.wait(10))
                cancel_order(self.order)
            finally:
                release.set()
            result = pending.result(20)
        self.assertTrue(result.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.assertFalse(self.order.is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def run_callback_expiry_race(self, *, callback_first):
        from orders.services import expire_due_orders, _restore_stock
        deadline = timezone.now() - timedelta(minutes=5)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.server_transactions[self.payment.authority]["paidAt"] = (deadline - timedelta(minutes=2)).isoformat()
        first_locked, release = Event(), Event()

        def slow_mark(order, *args, **kwargs):
            first_locked.set()
            if not release.wait(timeout=10):
                raise AssertionError("Test callback lock was not released.")
            return mark_order_paid(order, *args, **kwargs)

        def slow_restore(order):
            first_locked.set()
            if not release.wait(timeout=10):
                raise AssertionError("Test expiry lock was not released.")
            return _restore_stock(order)

        def callback():
            close_old_connections()
            try:
                local = Payment.objects.select_related("order").get(pk=self.payment.pk)
                return process_callback(local, self.callback_data(local))
            finally:
                close_old_connections()

        def expire():
            close_old_connections()
            try:
                return expire_due_orders()
            finally:
                close_old_connections()

        context = patch("orders.services._restore_stock", side_effect=slow_restore) if not callback_first else patch(
            "payments.services.mark_order_paid", side_effect=slow_mark,
        )
        with context, ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(callback if callback_first else expire)
            try:
                self.assertTrue(first_locked.wait(timeout=10))
                second = executor.submit(expire if callback_first else callback)
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            first_result = first.result(timeout=20)
            second_result = second.result(timeout=20)
        return (first_result, second_result) if callback_first else (second_result, first_result)

    def test_callback_lock_first_prevents_expiry_stock_release(self):
        payment, expired = self.run_callback_expiry_race(callback_first=True)
        self.assertEqual(expired, 0)
        self.assertFalse(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paid)
        self.assertFalse(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_expiry_lock_first_preserves_cancellation_and_released_stock(self):
        payment, expired = self.run_callback_expiry_race(callback_first=False)
        self.assertEqual(expired, 1)
        self.assertTrue(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_verify_wait_does_not_hold_order_or_payment_row_locks(self):
        from django.db import transaction
        started, release = Event(), Event()
        def slow(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify":
                self.assertFalse(connection.in_atomic_block)
                started.set()
                if not release.wait(10):
                    raise AssertionError("Verify test was not released")
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = slow
        def callback():
            close_old_connections()
            try:
                return process_callback(self.payment, self.callback_data(self.payment))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(callback)
            try:
                self.assertTrue(started.wait(10))
                with transaction.atomic():
                    Order.objects.select_for_update(nowait=True).get(pk=self.order.pk)
                    Payment.objects.select_for_update(nowait=True).get(pk=self.payment.pk)
            finally:
                release.set()
            self.assertTrue(pending.result(20).is_successful)

    def test_expiration_can_commit_while_external_verify_is_waiting(self):
        from orders.services import expire_due_orders
        started, release = Event(), Event()
        deadline = timezone.now() - timedelta(minutes=5)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        self.server_transactions[self.payment.authority]["paidAt"] = (deadline - timedelta(minutes=1)).isoformat()
        def slow(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify":
                started.set()
                if not release.wait(10):
                    raise AssertionError("Verify test was not released")
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = slow
        def callback():
            close_old_connections()
            try:
                return process_callback(self.payment, self.callback_data(self.payment))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(callback)
            try:
                self.assertTrue(started.wait(10))
                self.assertEqual(expire_due_orders(), 1)
            finally:
                release.set()
            result = pending.result(20)
        self.assertTrue(result.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_manual_callback_after_expiry_selection_holds_inventory(self):
        from orders.services import expire_due_orders
        selected, release = Event(), Event()
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00"
        original = Order.objects.due_for_expiry
        def candidates(now=None):
            records = list(original(now))
            selected.set()
            if not release.wait(10):
                raise AssertionError("Expiry candidate test was not released")
            return records
        def expire():
            close_old_connections()
            try:
                return expire_due_orders()
            finally:
                close_old_connections()
        with patch.object(Order.objects, "due_for_expiry", side_effect=candidates), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(expire)
            try:
                self.assertTrue(selected.wait(10))
                result = process_callback(self.payment, self.callback_data(self.payment))
                self.assertEqual(result.status, PaymentStatus.MANUAL_REVIEW)
            finally:
                release.set()
            self.assertEqual(pending.result(20), 0)
        self.assertFalse(Order.objects.get(pk=self.order.pk).inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_concurrent_manual_receipt_winner_blocks_aware_receipt(self):
        from payments.services import _apply_verified_callback
        second = self.historical_payment()
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00"
        manual_applied = Event()
        def apply(snapshot, *args, **kwargs):
            if snapshot.pk == second.pk and not manual_applied.wait(10):
                raise AssertionError("Manual receipt test was not released")
            result = _apply_verified_callback(snapshot, *args, **kwargs)
            if snapshot.pk == self.payment.pk:
                manual_applied.set()
            return result
        with patch("payments.services._apply_verified_callback", side_effect=apply), patch(
            "payments.services.mark_order_paid", wraps=mark_order_paid,
        ) as paid:
            results = self.run_callbacks([self.payment, second])
        paid.assert_not_called()
        self.assertTrue(all(r.status == PaymentStatus.MANUAL_REVIEW and not r.needs_refund for r in results))
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_finalized_callback_cannot_be_overwritten_by_delayed_conflicting_reference(self):
        started, release = Event(), Event()
        delayed_thread = []
        def delayed(request, *, timeout):
            import threading
            if urlsplit(request.full_url).path == "/v1/verify" and threading.get_ident() in delayed_thread:
                started.set()
                if not release.wait(10):
                    raise AssertionError("Delayed receipt test was not released")
                data = dict(self.server_transactions[self.payment.authority], status=1, refNumber="123456789")
                return self.http_response(data)
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = delayed
        def callback():
            import threading
            close_old_connections()
            delayed_thread.append(threading.get_ident())
            try:
                return process_callback(self.payment, self.callback_data(self.payment))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(callback)
            try:
                self.assertTrue(started.wait(10))
                authoritative = process_callback(self.payment, self.callback_data(self.payment))
                original_reference = authoritative.reference_id
            finally:
                release.set()
            delayed_result = pending.result(20)
        self.assertEqual(delayed_result.reference_id, original_reference)
        current = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(current.reference_id, original_reference)
        self.assertEqual(current.callback_count, 2)
        self.assertEqual(len(current.metadata["verification_history"]), 1)

    def test_network_failure_is_retryable_without_locked_state_mutation(self):
        def failure(request, *, timeout):
            if urlsplit(request.full_url).path == "/v1/verify":
                self.assertFalse(connection.in_atomic_block)
                raise TimeoutError("synthetic gateway timeout")
            return self.api_call(request, timeout=timeout)
        self.http.side_effect = failure
        with self.assertRaises(PaymentError):
            process_callback(self.payment, self.callback_data(self.payment))
        current = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(current.status, PaymentStatus.PENDING)
        self.assertEqual(current.callback_count, 0)
        self.assertEqual(current.metadata, self.payment.metadata)
        self.http.side_effect = self.api_call
        self.assertTrue(process_callback(current, self.callback_data(current)).is_successful)



class ReceiptSafetyTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.first, self.second = self.historical_payment(), self.historical_payment()

    def test_ambiguous_first_receipt_cannot_be_bypassed_by_second_callback(self):
        self.server_transactions[self.first.authority]["paidAt"] = "2026-11-14T10:28:00"
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            first = process_callback(self.first, self.callback_data(self.first))
            second = process_callback(self.second, self.callback_data(self.second))
        paid.assert_not_called()
        self.assertEqual(first.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(second.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(second.metadata["decision"]["reason"], "another_receipt_requires_review")
        for receipt in (first, second):
            self.assertFalse(receipt.needs_refund)
            self.assertEqual(receipt.transaction_id, receipt.authority)
            self.assertTrue(receipt.reference_id)
            self.assertTrue(receipt.metadata["verification_history"])
            self.assertEqual(receipt.metadata["verification"]["paidAt"], self.server_transactions[receipt.authority]["paidAt"])
        self.assertIsNotNone(second.gateway_paid_at)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paid)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_manual_hold_prevents_late_valid_receipt_from_automatic_refund(self):
        self.server_transactions[self.first.authority]["paidAt"] = "2026-11-14T10:28:00"
        process_callback(self.first, self.callback_data(self.first))
        deadline = timezone.now() - timedelta(minutes=5)
        Order.objects.filter(pk=self.order.pk).update(expires_at=deadline)
        result = process_callback(self.second, self.callback_data(self.second))
        self.assertEqual(result.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(result.needs_refund)
        from orders.services import expire_due_orders
        self.assertEqual(expire_due_orders(), 0)
        self.assertFalse(Order.objects.get(pk=self.order.pk).inventory_released)

    def test_boolean_recovery_override_is_rejected_before_gateway_calls(self):
        self.http.reset_mock()
        with self.assertRaises(PaymentError):
            process_callback(self.first, self.callback_data(self.first), recovery_primary_authorized=True,
                             recovery_audit={"source": "reconcile_zibal_payments"})
        self.http.assert_not_called()

    def test_incomplete_internal_recovery_plan_cannot_issue_authorization(self):
        from payments.services import _authorize_recovery_primary
        with self.assertRaises(PaymentError):
            _authorize_recovery_primary(order=self.order, payments=[self.first, self.second],
                                       verified={}, selected_track_ids=[self.first.authority],
                                       primary=self.first, backup=None, plan={})

    def test_conflicting_reference_preserves_original_receipt(self):
        self.server_transactions[self.first.authority]["paidAt"] = "2026-11-14T10:28:00"
        first = process_callback(self.first, self.callback_data(self.first))
        reference = first.reference_id
        self.server_transactions[first.authority].update(paidAt="2026-11-14T10:28:00Z", refNumber="123456789")
        result = process_callback(first, self.callback_data(first))
        self.assertEqual(result.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(result.reference_id, reference)
        self.assertEqual(result.metadata["decision"]["reason"], "receipt_identifiers_changed")
        self.assertEqual(len(result.metadata["verification_history"]), 2)

    def test_conflicting_transaction_id_is_preserved_even_without_a_reference(self):
        Payment.objects.filter(pk=self.first.pk).update(transaction_id="999999999", reference_id="")
        receipt = process_callback(self.first, self.callback_data(self.first))
        self.assertEqual(receipt.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(receipt.needs_refund)
        self.assertEqual(receipt.transaction_id, "999999999")
        self.assertEqual(receipt.reference_id, "")
        self.assertEqual(receipt.metadata["decision"]["reason"], "receipt_identifiers_changed")
        self.assertTrue(receipt.metadata["verification_history"])
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_historical_success_evidence_in_nonfinal_status_blocks_another_callback(self):
        Payment.objects.filter(pk=self.first.pk).update(
            status=PaymentStatus.FAILED, paid_at=None, gateway_paid_at=None,
            metadata={"verification": {"result": 100, "status": 1, "paidAt": "2026-11-14T10:28:00"}},
        )
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            receipt = process_callback(self.second, self.callback_data(self.second))
        paid.assert_not_called()
        self.assertEqual(receipt.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(receipt.metadata["decision"]["reason"], "another_receipt_requires_review")
        self.assertFalse(receipt.needs_refund)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_conflicting_inquiry_preserves_legacy_success_evidence_even_without_paid_at(self):
        previous = {"result": 100, "status": 1, "paidAt": "2026-11-14T10:28:00"}
        Payment.objects.filter(pk=self.first.pk).update(status=PaymentStatus.FAILED, metadata={"verification": previous})
        self.set_outcome(self.first, "failed")
        receipt = process_callback(self.first, self.callback_data(self.first, "failed"))
        self.assertEqual(receipt.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(receipt.needs_refund)
        self.assertIn(previous, [entry["data"] for entry in receipt.metadata["verification_history"]])
        self.assertEqual(receipt.metadata["verification"]["status"], 5)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)


class ManualReviewPresentationTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client.force_login(self.customer)

    def review(self, *, legacy=False):
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00.136000"
        result = process_callback(self.payment, self.callback_data(self.payment))
        if legacy:
            Order.objects.filter(pk=self.order.pk).update(payment_status=PaymentStatus.PENDING)
        self.order.refresh_from_db()
        return result

    def assert_review_page(self, response):
        self.assertContains(response, "نیازمند بررسی پشتیبانی")
        self.assertContains(response, "زیبال دریافت وجه را تأیید کرده است")
        self.assertContains(response, "لطفاً پرداخت تازه انجام ندهید")
        self.assertNotContains(response, "در انتظار پرداخت")
        self.assertNotContains(response, "پرداخت با موفقیت انجام شد")
        self.assertNotContains(response, 'action="' + reverse("orders:pay", args=[self.order.order_number]) + '"')

    def test_review_updates_payment_state_without_fulfilling_or_releasing_inventory(self):
        deadline = self.order.expires_at
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            result = self.review()
        paid.assert_not_called()
        self.assertEqual(result.status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(result.creation_status, PaymentCreationStatus.READY)
        self.assertIsNotNone(result.request_id)
        self.assertIsNotNone(result.paid_at)
        self.assertIsNotNone(result.verified_at)
        self.assertIsNone(result.gateway_paid_at)
        self.assertFalse(result.needs_refund)
        audit = result.metadata["verification"]["payment_time"]
        self.assertEqual(audit["reason"], "timezone_unconfirmed")
        self.assertEqual(audit["source"], "")
        self.assertEqual(audit["timezone"], "")
        self.assertIsNone(audit["normalized_paid_at"])
        self.assertEqual(result.metadata["decision"]["reason"], "timezone_unconfirmed")
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(self.order.is_paid)
        self.assertFalse(self.order.can_pay)
        self.assertFalse(self.order.inventory_released)
        self.assertIsNone(self.order.paid_at)
        self.assertEqual(self.order.expires_at, deadline)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_customer_detail_and_history_show_coherent_review_state(self):
        self.review()
        for url in (reverse("orders:detail", args=[self.order.order_number]), reverse("orders:list")):
            with self.subTest(url=url):
                self.assert_review_page(self.client.get(url))

    def test_legacy_order_uses_receipt_for_display_without_financial_writes(self):
        result = self.review(legacy=True)
        before = json.loads(json.dumps(result.metadata))
        for url in (reverse("orders:detail", args=[self.order.order_number]), reverse("orders:list")):
            with self.subTest(url=url):
                self.assert_review_page(self.client.get(url))
        self.order.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PENDING)
        self.assertEqual(self.order.display_payment_status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(result.metadata, before)
        self.assertEqual(result.callback_count, 1)
        self.assertFalse(self.order.can_pay)

    def test_review_older_than_the_last_five_attempts_is_still_visible_and_protects_order(self):
        self.review(legacy=True)
        for number in range(6):
            Payment.objects.create(order=self.order, gateway="zibal", amount=self.order.total,
                                   authority=str(900000000 + number), status=PaymentStatus.FAILED)
        response = self.client.get(reverse("orders:detail", args=[self.order.order_number]))
        self.assertTrue(response.context["manual_review_pending"])
        self.assertNotIn(self.payment.pk, [p.pk for p in response.context["payments"]])
        self.assertContains(response, "لطفاً پرداخت تازه انجام ندهید")
        self.assertNotContains(response, "در انتظار پرداخت")
        self.assertFalse(response.context["order"].can_pay)
        self.assertNotContains(response, 'action="' + reverse("orders:pay", args=[self.order.order_number]) + '"')

    def test_expired_review_does_not_show_an_ordinary_deadline_failure_or_expire_stock(self):
        from orders.services import expire_due_orders
        self.review()
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.client.get(reverse("orders:detail", args=[self.order.order_number]))
        self.assert_review_page(response)
        self.assertNotContains(response, "مهلت پرداخت تمام شده است")
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_pay_post_and_stale_result_urls_cannot_create_a_charge_or_claim_success(self):
        self.review(legacy=True)
        self.http.reset_mock()
        response = self.client.post(reverse("orders:pay", args=[self.order.order_number]), {"amount": "1"}, follow=True)
        self.assert_review_page(response)
        self.assertTrue(all(message.level_tag != "success" for message in response.context["messages"]))
        for financial_status in (PaymentStatus.PENDING, PaymentStatus.FAILED, PaymentStatus.CANCELLED):
            Order.objects.filter(pk=self.order.pk).update(payment_status=financial_status)
            for route in ("result_success", "result_failed", "result_cancelled"):
                with self.subTest(financial_status=financial_status, route=route):
                    response = self.client.get(reverse("payments:" + route, args=[self.order.order_number]))
                    self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.http.assert_not_called()
        self.assertEqual(self.order.payments.count(), 1)

    def test_manual_callback_emits_warning_and_never_a_final_success_message(self):
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00"
        response = self.client.get(reverse("payments:callback"), self.callback_data(self.payment), follow=True)
        self.assertEqual(response.redirect_chain, [(reverse("orders:detail", args=[self.order.order_number]), 302)])
        self.assert_review_page(response)
        messages = list(response.context["messages"])
        self.assertTrue(any(message.level_tag == "warning" for message in messages))
        self.assertFalse(any(message.level_tag == "success" for message in messages))

    def test_stale_annotated_order_cannot_bypass_new_review_during_payment_creation(self):
        stale = Order.objects.with_payment_review().get(pk=self.order.pk)
        self.assertFalse(stale.manual_review_pending)
        self.review()
        self.http.reset_mock()
        with self.assertRaises(PaymentError):
            create_payment_for_order(stale)
        self.http.assert_not_called()
        self.assertEqual(self.order.payments.count(), 1)

    def test_failed_or_cancelled_legacy_callback_preserves_other_manual_receipt(self):
        old_attempt = self.historical_payment()
        receipt = self.review()
        for outcome in ("failed", "cancelled"):
            with self.subTest(outcome=outcome):
                self.set_outcome(old_attempt, outcome)
                response = self.client.get(reverse("payments:callback"), self.callback_data(old_attempt, outcome))
                self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
                self.order.refresh_from_db()
                self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
                self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
                receipt.refresh_from_db()
                self.assertEqual(receipt.metadata["decision"]["reason"], "timezone_unconfirmed")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_conflicting_inquiry_preserves_review_order_and_original_decision(self):
        first = self.review(legacy=True)
        original_decision = json.loads(json.dumps(first.metadata["decision"]))
        self.set_outcome(self.payment, "failed")
        conflict = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(conflict.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(conflict.needs_refund)
        self.assertEqual(conflict.paid_at, first.paid_at)
        self.assertEqual(conflict.reference_id, first.reference_id)
        self.assertIn(original_decision, [entry["data"] for entry in conflict.metadata["decision_history"]])
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
        self.server_transactions[self.payment.authority].update(status=2, paidAt="2026-11-14T10:28:00Z")
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as paid:
            retried = process_callback(self.payment, self.callback_data(self.payment))
        paid.assert_not_called()
        self.assertEqual(retried.metadata["decision"]["reason"], "previous_receipt_conflict")
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_failed_attempt_cannot_clear_an_order_review_without_validated_receipt_resolution(self):
        Order.objects.filter(pk=self.order.pk).update(payment_status=PaymentStatus.MANUAL_REVIEW)
        self.set_outcome(self.payment, "failed")
        process_callback(self.payment, self.callback_data(self.payment))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.MANUAL_REVIEW)
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertFalse(self.order.can_pay)
        self.assertFalse(self.order.inventory_released)

    def test_additional_review_does_not_downgrade_a_legitimately_paid_order(self):
        primary = process_callback(self.payment, self.callback_data(self.payment))
        extra = self.historical_payment()
        self.server_transactions[extra.authority]["paidAt"] = "2026-11-14T10:28:00"
        process_callback(extra, self.callback_data(extra))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.paid_at, primary.paid_at)
        self.assertEqual(self.order.display_payment_status, PaymentStatus.SUCCESSFUL)
        response = self.client.get(reverse("orders:detail", args=[self.order.order_number]))
        self.assertContains(response, "یکی از پرداخت‌های این سفارش نیازمند بررسی پشتیبانی است")
        self.assertNotContains(response, "در انتظار پرداخت")
        self.assertFalse(self.order.can_pay)

    def test_admin_lists_filters_and_dashboard_agree_for_a_legacy_review(self):
        from common.dashboard import build_dashboard_context
        self.review(legacy=True)
        staff = User.objects.create_superuser(phone_number="09120001234", password="isolated-test-only")
        self.client.force_login(staff)
        order_url = reverse("admin:orders_order_changelist")
        response = self.client.get(order_url)
        self.assertContains(response, 'class="field-payment_status_display">نیازمند بررسی پشتیبانی</td>')
        self.assertContains(response, 'class="field-order_status_display">نیازمند بررسی پشتیبانی</td>')
        response = self.client.get(order_url, {"payment_status__exact": PaymentStatus.MANUAL_REVIEW})
        self.assertEqual(response.context["cl"].result_count, 1)
        response = self.client.get(order_url, {"payment_status__exact": PaymentStatus.PENDING})
        self.assertEqual(response.context["cl"].result_count, 0)
        response = self.client.get(reverse("admin:payments_payment_changelist"), {"status__exact": PaymentStatus.MANUAL_REVIEW})
        self.assertEqual(response.context["cl"].result_count, 1)
        self.assertContains(response, 'class="field-status_display">نیازمند بررسی پشتیبانی</td>')
        response = self.client.get(reverse("admin:orders_order_change", args=[self.order.pk]))
        self.assertContains(response, 'field-payment_status_display')
        self.assertContains(response, "نیازمند بررسی پشتیبانی")
        dashboard = build_dashboard_context(staff)["dashboard"]
        self.assertEqual(dashboard["manual_review_orders"], 1)
        self.assertEqual(dashboard["pending_payment_orders"], 0)
        self.assertEqual(dashboard["paid_orders"], 0)
        self.assertEqual(dashboard["total_sales"], 0)
        self.assertEqual(dashboard["recent_orders"][0].display_status, PaymentStatus.MANUAL_REVIEW)


class StoredEvidenceInspectionTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payment = self.start_payment()
        self.server_transactions[self.payment.authority]["paidAt"] = "2026-11-14T10:28:00.136000"
        self.payment = process_callback(self.payment, self.callback_data(self.payment))
        self.http.reset_mock()

    def inspect(self):
        output = io.StringIO()
        call_command("inspect_zibal_payment", payment_id=self.payment.pk, stdout=output)
        self.output = output.getvalue()
        return json.loads(self.output)

    def test_local_evidence_reports_missing_track_as_not_returned_and_never_calls_gateway(self):
        before = Payment.objects.values().get(pk=self.payment.pk)
        order_before = Order.objects.values().get(pk=self.order.pk)
        report = self.inspect()
        self.assertEqual(report["verification"]["result"], 100)
        self.assertEqual(report["verification"]["verify_result"], 100)
        self.assertEqual(report["verification"]["status"], 1)
        self.assertEqual(report["verification"]["paidAt"], "2026-11-14T10:28:00.136000")
        self.assertFalse(report["verification"]["track_id_present"])
        self.assertEqual(report["verification"]["track_id_comparison"], "not_returned")
        self.assertEqual(report["verification"]["transaction_id_comparison"], "matched")
        self.assertEqual(report["verification"]["reference_comparison"], "matched")
        self.assertTrue(report["verification"]["amount_matches"])
        self.assertTrue(report["verification"]["order_id_matches"])
        self.assertEqual(report["verification"]["payment_time"]["reason"], "timezone_unconfirmed")
        self.assertEqual(report["decision"]["reason"], "timezone_unconfirmed")
        self.assertNotIn(self.payment.authority, self.output)
        self.assertNotIn(str(self.payment.request_id), self.output)
        self.http.assert_not_called()
        self.assertEqual(Payment.objects.values().get(pk=self.payment.pk), before)
        self.assertEqual(Order.objects.values().get(pk=self.order.pk), order_before)

    def test_track_type_normalization_distinguishes_real_mismatch_and_invalid_values(self):
        for value, comparison in (
            (int(self.payment.authority), "matched"), (self.payment.authority, "matched"),
            (int(self.payment.authority) + 1, "mismatch"), (None, "invalid"), (True, "invalid"), ("01", "invalid"),
        ):
            with self.subTest(value=value):
                self.payment.metadata["verification"]["trackId"] = value
                self.payment.save(update_fields=["metadata"])
                report = self.inspect()
                self.assertTrue(report["verification"]["track_id_present"])
                self.assertEqual(report["verification"]["track_id_type"], type(value).__name__)
                self.assertEqual(report["verification"]["track_id_comparison"], comparison)
        self.http.assert_not_called()

    def test_unexpected_private_payload_and_history_are_never_printed(self):
        private = "synthetic-private-merchant-card-customer-payload"
        self.payment.metadata["verification"].update(merchant=private, cardNumber=private, message=private, paidAt=private)
        self.payment.metadata["verification"]["payment_time"].update(reason=private, timezone=private, normalized_paid_at=private)
        self.payment.metadata["decision"]["reason"] = private
        self.payment.metadata["verification_history"] = [{"data": {"merchant": private, "payment_time": {"reason": private}}}]
        self.payment.save(update_fields=["metadata"])
        report = self.inspect()
        self.assertNotIn(private, self.output)
        self.assertNotIn(self.customer.phone_number, self.output)
        self.assertNotIn("cardNumber", self.output)
        self.assertNotIn(self.payment.authority, self.output)
        self.assertEqual(report["history_counts"]["verification_history"], 1)
        self.http.assert_not_called()

    def test_unknown_or_historical_payment_is_rejected_without_gateway_operations(self):
        with self.assertRaises(CommandError):
            call_command("inspect_zibal_payment", payment_id=999999, stdout=io.StringIO())
        Payment.objects.filter(pk=self.payment.pk).update(gateway="legacy-provider")
        with self.assertRaises(CommandError):
            self.inspect()
        self.http.assert_not_called()


class PaymentStatusBadgeTests(SimpleTestCase):
    def test_every_payment_status_has_a_persian_badge_including_order_review(self):
        from django.template.loader import render_to_string
        labels = {"pending": "در انتظار پرداخت", "successful": "موفق", "failed": "ناموفق",
                  "cancelled": "لغو شده", "expired": "منقضی شده", "manual_review": "نیازمند بررسی پشتیبانی"}
        for status in PaymentStatus.values:
            with self.subTest(status=status):
                content = render_to_string("partials/status_badge.html", {"kind": "payment", "status": status})
                self.assertIn(labels[status], content)
        content = render_to_string("partials/status_badge.html", {"kind": "order", "status": PaymentStatus.MANUAL_REVIEW})
        self.assertIn(labels["manual_review"], content)
        self.assertNotIn("در انتظار پرداخت", content)
