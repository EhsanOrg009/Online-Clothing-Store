"""تست چرخه زیبال با پاسخ‌های HTTP شبیه‌سازی‌شده؛ هیچ تراکنش واقعی انجام نمی‌شود."""

import gzip
import io
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit

from django.db import close_old_connections, connection
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import (
    Client, RequestFactory, TestCase, TransactionTestCase,
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
from payments.models import Payment
from payments.services import (
    PaymentError, create_payment_for_order, expire_stale_payments, process_callback,
)
from products.models import Category, Color, Product, ProductVariant, Size


TEST_SETTINGS = {
    "DEBUG": True,
    "PAYMENT_GATEWAY": "zibal",
    "ZIBAL_MERCHANT": "zibal",
    "ZIBAL_TIMEOUT_SECONDS": 10,
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
                "paidAt": "2026-11-14T10:30:00.123000",
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
        self.assertEqual(payload["callbackUrl"], "https://minikandoo.example/payments/callback/")
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
        self.assertEqual(payload["callbackUrl"], "https://minikandoo.example/payments/callback/")

    def test_development_callback_can_use_configured_http_origin(self):
        with override_settings(SITE_URL="http://localhost:8765"):
            self.start_payment()
        self.assertEqual(json.loads(self.http.call_args.args[0].data)["callbackUrl"], "http://localhost:8765/payments/callback/")

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
            with self.subTest(code=code):
                self.http.side_effect = None
                self.http.return_value = self.http_response({"result": code, "message": "sensitive remote text"})
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.count(), 0)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)

    def test_invalid_request_track_ids_are_rejected(self):
        for track_id in (None, "", 0, -1, True, 1.5, "1/redirect", "01", 2**63):
            with self.subTest(track_id=track_id):
                self.http.side_effect = None
                self.http.return_value = self.http_response({"result": 100, "trackId": track_id})
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.count(), 0)

    def test_invalid_api_responses_are_handled(self):
        for body in (b"not json", b"[]", b"null", b'{"result":true}', b'{"result":"100"}', b"{}", b"\xff"):
            with self.subTest(body=body):
                self.http.side_effect = None
                self.http.return_value = self.http_response(body=body)
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.count(), 0)

    def test_connection_timeout_and_http_errors_are_handled(self):
        for error in (
            URLError("offline"), TimeoutError("timed out"), BadStatusLine("invalid HTTP"),
            IncompleteRead(b"partial", 10),
            HTTPError("https://gateway.zibal.ir/v1/request", 302, "redirect", {}, None),
            HTTPError("https://gateway.zibal.ir/v1/request", 503, "unavailable", {}, None),
        ):
            with self.subTest(error=type(error).__name__):
                self.http.side_effect = error
                with self.assertRaises(PaymentError):
                    self.start_payment()
                self.assertEqual(Payment.objects.count(), 0)
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
        self.http.side_effect = None
        self.http.return_value = self.http_response({"result": 100, "trackId": int(first.authority)})
        with self.assertRaises(PaymentError):
            self.start_payment()
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
        payment = process_callback(self.payment, self.callback_data(self.payment))
        request = self.api_calls("/v1/verify")[0].args[0]
        self.assertEqual(json.loads(request.data), {"merchant": "zibal", "trackId": int(payment.authority)})
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(payment.transaction_id, payment.authority)
        self.assertEqual(payment.reference_id, str(int(payment.authority) + 100))
        self.assertIsNotNone(payment.verified_at)
        self.assertIsNotNone(payment.paid_at)
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
            ("amount", True),
            ("amount", str(original["amount"])),
            ("orderId", "OTHER-ORDER"),
            ("trackId", int(self.payment.authority) + 1),
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
            {"paidAt": None}, {"paidAt": "invalid-date"},
        ):
            data = {**original, "status": 1, **changes}
            self.http.side_effect = None
            self.http.return_value = self.http_response(data)
            with self.subTest(changes=changes), self.assertRaises(PaymentError):
                process_callback(self.payment, self.callback_data(self.payment))
            self.assert_unpaid()

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
                    self.assertEqual(self.payment.metadata.keys(), {"redirect_url"})

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

    def test_malformed_paid_timestamp_is_retryable(self):
        original = self.server_transactions[self.payment.authority].copy()
        for paid_at in (None, "", "invalid-date", "2026-02-30T10:30:00", 100, True, {}):
            with self.subTest(paid_at=paid_at):
                self.http.side_effect = [
                    self.http_response({**original, "status": 1, "paidAt": paid_at}),
                ]
                with self.assertRaises(PaymentError):
                    process_callback(self.payment, self.callback_data(self.payment))
                self.assert_unpaid()

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
        second = self.start_payment()
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
        payments = [self.payment, self.start_payment(), self.start_payment()]
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
        second = self.start_payment()
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

    def test_checkout_request_failure_preserves_created_order_and_retry(self):
        self.client.post(reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1})
        self.http.side_effect = URLError("offline")
        response, order = self.checkout()
        self.assertEqual(response.url, reverse("orders:detail", args=[order.order_number]))
        self.assertEqual(order.payments.count(), 0)
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertFalse(order.is_paid)
        self.assertEqual(CartItem.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        self.http.side_effect = self.api_call
        response = self.client.post(reverse("orders:pay", args=[order.order_number]), {"amount": "1"})
        payment = order.payments.get()
        self.assertEqual(response.url, reverse("payments:redirect", args=[payment.pk]))
        self.assertEqual(payment.amount, order.total)
        self.assertEqual(self.server_transactions[payment.authority]["amount"], int(order.total) * 10)


class RecoveryCommandTests(PaymentFixtureMixin, PaymentTestCase):
    """Recovery is exercised only with mocked APIs and a synthetic test SQL dump."""

    def setUp(self):
        super().setUp()
        self.build_payment_data()
        self.payments = [self.start_payment() for _ in range(3)]
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
        for payment, paid_at in zip(self.payments, (
            "2026-11-14T10:33:00", "2026-11-14T10:31:00", "2026-11-14T10:32:00",
        )):
            self.server_transactions[payment.authority].update({
                "status": 1, "paidAt": paid_at, "refNumber": "00" + str(int(payment.authority) + 100),
            })
        self.http.reset_mock()

    def reconcile(self, *, apply=False, backup=None, track_ids=None, gateway_timezone=None):
        self.command_output = io.StringIO()
        call_command(
            "reconcile_zibal_payments", order_number=self.order.order_number,
            track_id=track_ids or [payment.authority for payment in reversed(self.payments)],
            apply=apply, backup=str(backup) if backup else None,
            gateway_timezone=gateway_timezone, stdout=self.command_output,
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

    def test_gateway_time_ties_use_local_creation_time(self):
        for payment, age in zip(self.payments, (2, 1, 3)):
            self.server_transactions[payment.authority]["paidAt"] = "2026-11-14T10:30:00"
            Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(minutes=age))
        report = self.reconcile()
        self.assertEqual(report["planned_primary"], self.payments[2].authority)
        self.assertEqual(report["gateway_time_order"], [self.payments[i].authority for i in (2, 0, 1)])

    def test_existing_valid_primary_is_preserved_and_all_new_payments_need_refunds(self):
        prior = self.start_payment()
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
        other = self.start_payment()
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
        prior = self.start_payment()
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
        report = self.reconcile(apply=True, backup=self.backup)
        self.assertIsNone(report["planned_primary"])
        self.assertEqual(report["actual_primary"], [])
        self.assertEqual(report["after"]["status"], OrderStatus.PENDING_PAYMENT)
        self.assertEqual(report["after"]["payment_status"], PaymentStatus.PENDING)
        self.assertEqual(len(report["refund_required"]), 3)
        self.assertEqual(report["before"]["expires_at"], report["after"]["expires_at"])

    def test_mismatched_target_stays_pending_while_other_valid_targets_recover(self):
        self.server_transactions[self.payment.authority]["amount"] += 10
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        report = self.report()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.PENDING)
        self.assertEqual(self.payment.callback_count, 0)
        self.assertEqual(report["successful_count"], 2)
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])
        self.assertEqual(len(report["refund_required"]), 1)

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
        self.assertEqual(report["successful_count"], 2)
        self.assertEqual(report["actual_primary"], [self.payments[2].authority])
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
        self.assertEqual(self.report()["successful_count"], 2)
        earliest.refresh_from_db()
        self.assertEqual(earliest.status, PaymentStatus.FAILED)

    def test_mixed_gateway_timestamp_formats_need_a_confirmed_timezone(self):
        self.server_transactions[self.payment.authority]["paidAt"] += "Z"
        with self.assertRaises(CommandError):
            self.reconcile(apply=True, backup=self.backup)
        self.assert_all_pending()
        self.assertEqual(self.report()["replayed"], [])
        report = self.reconcile(apply=True, backup=self.backup, gateway_timezone="Asia/Tehran")
        self.assertEqual(report["successful_count"], 3)
        self.assertEqual(report["actual_primary"], [self.payments[1].authority])

    def test_foreign_or_duplicate_track_ids_are_rejected_without_api_calls(self):
        for track_ids in ([self.payment.authority] * 2, ["9900"]):
            with self.subTest(track_ids=track_ids), self.assertRaises(CommandError):
                self.reconcile(track_ids=track_ids)
            self.http.assert_not_called()
            self.assert_all_pending()


class ProductionBackupScriptTests(TestCase):
    """A stub Docker executable supplies synthetic data; no database is accessed."""

    def setUp(self):
        self.temp_dir = Path(self.enterContext(TemporaryDirectory(prefix="minikandoo-backup-script-test-")))
        self.backup_dir = self.temp_dir / "backups"
        self.bin_dir = self.temp_dir / "bin"
        self.bin_dir.mkdir()
        self.environment_file = self.temp_dir / "empty-test-only.env"
        self.environment_file.write_text("")
        self.docker_stub = self.bin_dir / "docker"
        self.script = Path(__file__).resolve().parents[1] / "scripts/backup-production-db"

    def run_script(self, stub):
        self.docker_stub.write_text("#!/bin/sh\n" + stub)
        self.docker_stub.chmod(0o700)
        env = {**os.environ, "PATH": str(self.bin_dir) + ":" + os.environ["PATH"], "ENV_FILE": str(self.environment_file)}
        return subprocess.run(["bash", str(self.script), str(self.backup_dir)], env=env, capture_output=True, text=True, timeout=10)

    def test_backup_is_private_valid_and_does_not_overwrite_existing_files(self):
        self.backup_dir.mkdir()
        previous = self.backup_dir / "db-20261114-190647.sql.gz"
        previous.write_bytes(b"preserve-existing-backup")
        for _ in range(2):
            result = self.run_script("printf '%s\\n' '-- MariaDB dump' 'synthetic test SQL' '-- Dump completed on test'\n")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(previous.read_bytes(), b"preserve-existing-backup")
        created = [file for file in self.backup_dir.glob("*.sql.gz") if file != previous]
        self.assertEqual(len(created), 2)
        for file in created:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn(b"synthetic test SQL", gzip.decompress(file.read_bytes()))
            self.assertTrue(Path(str(file) + ".sha256").is_file())
        self.assertEqual(list(self.backup_dir.glob("*.partial")), [])

    def test_dump_failure_is_not_reported_as_a_valid_backup(self):
        result = self.run_script("printf '%s' 'partial test SQL'\nexit 1\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Recovery must stop", result.stderr)
        self.assertEqual(list(self.backup_dir.glob("*.sql.gz")), [])
        self.assertEqual(len(list(self.backup_dir.glob("*.partial"))), 1)


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

    def test_concurrent_duplicate_callbacks_verify_and_pay_once(self):
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            self.run_callbacks([self.payment, self.payment])
        self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(len(self.api_calls("/v1/verify")), 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.callback_count, 2)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_concurrent_successful_attempts_only_pay_order_once(self):
        second = self.start_payment()
        with patch("payments.services.mark_order_paid", wraps=mark_order_paid) as mark_paid:
            self.run_callbacks([self.payment, second])
        self.assertEqual(mark_paid.call_count, 1)
        self.assertEqual(self.order.payments.filter(status=PaymentStatus.SUCCESSFUL, needs_refund=False).count(), 1)
        self.assertEqual(self.order.payments.filter(needs_refund=True).count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_concurrent_failed_attempt_cannot_downgrade_paid_order(self):
        second = self.start_payment()
        self.set_outcome(second, "failed")
        self.run_callbacks([self.payment, second])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
