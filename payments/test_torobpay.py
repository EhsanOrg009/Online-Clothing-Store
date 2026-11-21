"""CPG checkout, receipt, inventory and refund safety with simulated HTTP."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.core.cache import caches
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Address, User
from cart.models import Cart, CartItem
from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order, OrderItem
from orders.services import OrderError, cancel_order, create_order, expire_due_orders, mark_order_paid, transition_status
from payments.checkout import payment_options
from payments.gateways import get_gateway
from payments.gateways.base import PaymentGatewayError
from payments.gateways.torobpay import TorobPayGateway
from payments.gateways.torobpay_data import token_fingerprint
from payments.models import Payment, PaymentCreationStatus
from payments.services import PaymentError, create_payment_for_order, process_callback
from payments.tests import PaymentFixtureMixin
from payments.torobpay_services import (
    RemoteEvidence, bind_recovered_token, cancel_payment, inspect_payment, reconcile_payment, update_payment,
)
from payments.torobpay_test_support import CPGHTTPStubMixin, TOROBPAY_TEST_SETTINGS
from products.models import Category, Discount, Product


@override_settings(**TOROBPAY_TEST_SETTINGS)
class CPGTestCase(PaymentFixtureMixin, CPGHTTPStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.build_payment_data()

    def start(self):
        return create_payment_for_order(self.order, gateway_name="torobpay")

    def pay(self, payment):
        self.outcome(payment, "W_FOR_VERIFY")
        return process_callback(payment, self.callback_data(payment))

    def use_quantity(self, quantity):
        cancel_order(self.order)
        cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=quantity)
        self.order = create_order(customer=self.customer, cart=cart, address=self.address)

    def staff(self):
        return User.objects.create_superuser(phone_number="09120002222", password="synthetic-only")

    def assert_review(self, payment):
        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.MANUAL_REVIEW)
        self.assertFalse(self.order.is_paid)
        self.assertTrue(payment.reservation_hold)


class CPGCreationTests(CPGTestCase):
    def test_registry_and_legacy_global_zibal_configuration(self):
        self.assertIsInstance(get_gateway("torobpay"), TorobPayGateway)
        self.assertIsInstance(get_gateway("payments.gateways.torobpay.TorobPayGateway"), TorobPayGateway)
        self.assertEqual(get_gateway().name, "zibal")
        with override_settings(PAYMENT_GATEWAYS=[]):
            with self.assertRaises(PaymentError):
                self.start()
        self.http.assert_not_called()

    def test_disabled_or_arbitrary_selected_gateway_is_rejected_before_http(self):
        for selected in ("mock", "evil", "payments.gateways.torobpay.TorobPayGateway", "os.system"):
            with self.subTest(selected=selected), self.assertRaises(PaymentError):
                create_payment_for_order(self.order, gateway_name=selected)
        self.http.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def test_token_payload_uses_persisted_snapshots_and_single_rial_cart(self):
        self.use_quantity(2)
        payment = self.start()
        payload = self.api_calls("token")[0]["payload"]
        item = self.order.items.get()
        self.assertEqual(payload["amount"], int(self.order.total) * 10)
        self.assertEqual(payload["paymentMethodTypeDto"], "ONLINE_CREDIT")
        self.assertEqual(payload["returnURL"], "https://minikandoo.example" + reverse("payments:torobpay_callback", args=[payment.request_id]))
        self.assertEqual(payload["transactionId"], str(payment.request_id))
        self.assertEqual(payload["registration_phone_number"], self.customer.phone_number)
        self.assertEqual(payload["customer_full_name"], self.order.receiver_name)
        self.assertEqual(payload["address"], self.order.address_line)
        self.assertEqual(payload["postalCode"], self.order.postal_code)
        self.assertEqual(payload["city"], self.order.city)
        self.assertEqual(payload["province"], self.order.province)
        self.assertEqual(payload["mobile"], self.order.receiver_phone)
        self.assertEqual(len(payload["cartList"]), 1)
        cart = payload["cartList"][0]
        self.assertEqual(cart["cartId"], self.order.order_number)
        self.assertEqual(cart["shippingAmount"], int(self.order.shipping_fee) * 10)
        self.assertTrue(cart["isShipmentIncluded"])
        self.assertNotIn("taxAmount", cart)
        self.assertNotIn("isTaxIncluded", cart)
        self.assertNotIn("externalSourceAmount", payload)
        self.assertEqual(cart["cartItems"], [{"id": str(item.pk), "name": item.product_name + " / " + item.variant_label,
                                             "count": 2, "amount": int(item.original_unit_price) * 10, "category": item.category_name}])
        self.assertEqual(cart["totalAmount"] - payload["discountAmount"], payload["amount"])
        self.assertIs(type(payload["amount"]), int)
        self.assertEqual(payment.amount, self.order.total)
        self.assertGreater(len(payment.provider_token), 64)
        self.assertEqual(payment.authority, token_fingerprint(payment.provider_token))
        self.assertEqual(payment.creation_status, PaymentCreationStatus.READY)
        self.assertTrue(payment.reservation_hold)

    def test_discount_and_shipping_are_counted_exactly_once(self):
        cancel_order(self.order)
        Discount.objects.create(product=self.product, name="تخفیف", discount_type="percent", value=20)
        cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)
        self.order = create_order(customer=self.customer, cart=cart, address=self.address)
        self.start()
        payload = self.api_calls("token")[0]["payload"]
        cart = payload["cartList"][0]
        self.assertEqual(payload["discountAmount"], 2000000)
        self.assertEqual(cart["cartItems"][0]["amount"], 5000000)
        self.assertEqual(cart["totalAmount"], 10500000)
        self.assertEqual(payload["amount"], 8500000)
        self.assertEqual(sum(item["amount"] * item["count"] for item in cart["cartItems"]) + cart["shippingAmount"]
                         - payload["discountAmount"], payload["amount"])

    def test_catalog_and_address_changes_do_not_change_historical_payload(self):
        saved_item = self.order.items.get()
        new_category = Category.objects.create(name="دستهٔ تازه")
        Product.objects.filter(pk=self.product.pk).update(name="نام تازه", category=new_category, base_price=1)
        self.address.address_line = "نشانی تازه"
        self.address.receiver_name = "گیرنده تازه"
        self.address.save()
        self.start()
        payload = self.api_calls("token")[0]["payload"]
        self.assertEqual(payload["address"], self.order.address_line)
        self.assertEqual(payload["customer_full_name"], self.order.receiver_name)
        self.assertEqual(payload["cartList"][0]["cartItems"][0]["category"], saved_item.category_name)
        self.assertEqual(payload["cartList"][0]["cartItems"][0]["amount"], 5000000)
        self.assertIn(saved_item.product_name, payload["cartList"][0]["cartItems"][0]["name"])

    def test_deleted_product_does_not_destroy_category_snapshot(self):
        self.product.delete()
        self.start()
        item = self.api_calls("token")[0]["payload"]["cartList"][0]["cartItems"][0]
        self.assertEqual(item["category"], "پلیور")
        self.assertIn("پلیور نمونه", item["name"])

    def test_missing_historical_category_is_rejected_without_remote_token(self):
        OrderItem.objects.filter(order=self.order).update(category_name="")
        with self.assertRaises(PaymentError):
            self.start()
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(len(self.api_calls("token")), 0)

    def test_receiver_and_registered_phone_are_independent_server_fields(self):
        Order.objects.filter(pk=self.order.pk).update(receiver_phone="09120003333")
        User.objects.filter(pk=self.customer.pk).update(phone_number="09120004444")
        self.start()
        payload = self.api_calls("token")[0]["payload"]
        self.assertEqual(payload["mobile"], "09120003333")
        self.assertEqual(payload["registration_phone_number"], "09120004444")

    def test_bad_cart_math_or_required_details_never_create_remote_token(self):
        for field, value in (("total", 1), ("receiver_name", "ناقص"), ("postal_code", "bad"), ("province", "")):
            original = getattr(self.order, field)
            Order.objects.filter(pk=self.order.pk).update(**{field: value})
            with self.subTest(field=field), self.assertRaises(PaymentError):
                self.start()
            Order.objects.filter(pk=self.order.pk).update(**{field: original})
        self.assertEqual(len(self.api_calls("token")), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_pending_attempt_is_reused_without_duplicate_create(self):
        first = self.start()
        again = self.start()
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.request_id, again.request_id)
        self.assertEqual(len(self.api_calls("token")), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_unknown_create_outcome_preserves_uuid_and_blocks_blind_resend(self):
        def lost(record):
            self.server_response(record, "token")
            raise TimeoutError("synthetic-lost-response")
        self.responses["token"] = [lost]
        with self.assertRaises(PaymentError):
            self.start()
        payment = Payment.objects.get()
        self.assertEqual(payment.creation_status, PaymentCreationStatus.UNCERTAIN)
        self.assertIsNone(payment.authority)
        self.assertEqual(payment.provider_token, "")
        self.assertIsNotNone(payment.request_id)
        self.assertTrue(payment.reservation_hold)
        with self.assertRaises(PaymentError):
            self.start()
        self.assertEqual(len(self.api_calls("token")), 1)

    def test_callback_cannot_supply_a_missing_token_after_lost_create(self):
        self.responses["token"] = [TimeoutError("synthetic")]
        with self.assertRaises(PaymentError):
            self.start()
        payment = Payment.objects.get()
        result = process_callback(payment, self.callback_data(payment))
        self.assertEqual(result.provider_token, "")
        self.assertEqual(result.callback_count, 1)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)

    def test_documented_token_rejection_permits_new_attempt_and_preserves_history(self):
        self.responses["token"] = [self.error(1003)]
        with self.assertRaises(PaymentError):
            self.start()
        rejected = Payment.objects.get()
        self.assertEqual(rejected.creation_status, PaymentCreationStatus.REJECTED)
        self.assertFalse(rejected.reservation_hold)
        created = self.start()
        self.assertNotEqual(rejected.pk, created.pk)
        self.assertEqual(Payment.objects.get(pk=rejected.pk).creation_status, PaymentCreationStatus.REJECTED)

    def test_unknown_2xx_logical_failure_does_not_allow_duplicate_token(self):
        self.responses["token"] = [self.error(1011, status=200)]
        with self.assertRaises(PaymentError):
            self.start()
        self.assertEqual(Payment.objects.get().creation_status, PaymentCreationStatus.UNCERTAIN)

    def test_hostile_hosted_url_retains_token_for_manual_reconciliation(self):
        def hostile(record):
            response = self.server_response(record, "token")
            data = json.loads(response.read.return_value)
            data["response"]["paymentPageUrl"] = "https://evil.example/secret"
            return self.http_response(data)
        self.responses["token"] = [hostile]
        with self.assertRaises(PaymentError):
            self.start()
        payment = Payment.objects.get()
        self.assertTrue(payment.provider_token)
        self.assertEqual(payment.authority, token_fingerprint(payment.provider_token))
        self.assertEqual(payment.creation_status, PaymentCreationStatus.REVIEW)
        self.client.force_login(self.customer)
        response = self.client.get(reverse("payments:redirect", args=[payment.pk]))
        self.assertEqual(response.url, reverse("orders:detail", args=[self.order.order_number]))
        self.assertNotIn("evil.example", response.content.decode())

    def test_redirect_has_no_referrer_and_customer_pages_do_not_reveal_tokens(self):
        payment = self.start()
        self.client.force_login(self.customer)
        response = self.client.get(reverse("payments:redirect", args=[payment.pk]))
        self.assertContains(response, "https://cpg.torobpay.com/pay")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        detail = self.client.get(reverse("orders:detail", args=[self.order.order_number]))
        self.assertNotContains(detail, payment.provider_token)
        self.assertNotContains(detail, self.jwt)
        metadata = {key: value for key, value in payment.metadata.items() if key != "redirect_url"}
        self.assertNotIn(payment.provider_token, json.dumps(metadata))
        self.assertNotIn(self.jwt, json.dumps(metadata))

    def test_http_cannot_run_inside_application_atomic_transaction(self):
        with transaction.atomic(), self.assertRaises(PaymentError):
            self.start()
        self.http.assert_not_called()


class CPGCallbackTests(CPGTestCase):
    def setUp(self):
        super().setUp()
        self.payment = self.start()

    def test_verify_then_settle_then_status_is_required_before_fulfillment(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        with patch("payments.torobpay_services.mark_order_paid", wraps=mark_order_paid) as paid:
            payment = process_callback(self.payment, self.callback_data(self.payment))
            again = process_callback(payment, self.callback_data(payment))
        paid.assert_called_once()
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(again.callback_count, 2)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 1)
        operations = [record["path"].split("?")[0].rsplit("/", 1)[-1] for record in self.requests if "/payment/" in record["path"]]
        self.assertEqual(operations, ["token", "status", "verify", "status", "settle", "status"])
        self.assertEqual(self.api_calls("verify")[0]["payload"], {"paymentToken": payment.provider_token})
        self.assertEqual(self.api_calls("settle")[0]["headers"]["Authorization"], "Bearer " + self.jwt)
        self.assertEqual(payment.transaction_id, str(payment.request_id))
        self.assertIsNone(payment.gateway_paid_at)
        self.assertTrue(payment.order.is_paid)
        self.assertEqual(payment.metadata["verification"]["payment_time"]["source"], "server_confirmation_upper_bound")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_success_callback_is_advisory_when_remote_order_is_new(self):
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assertEqual(result.status, PaymentStatus.PENDING)
        self.assertFalse(result.order.is_paid)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_failed_callback_cannot_override_authoritative_success(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        result = process_callback(self.payment, self.callback_data(self.payment, state="FAILED"))
        self.assertTrue(result.is_successful)
        self.assertTrue(result.order.is_paid)

    def test_verify_adapter_alone_returns_only_intermediate_pending_result(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        result = TorobPayGateway().verify_payment(self.payment)
        self.assertFalse(result.successful)
        self.assertEqual(result.status, PaymentStatus.PENDING)
        self.assertFalse(Order.objects.get(pk=self.order.pk).is_paid)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_verify_timeout_after_remote_application_uses_status_and_settles_once(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        def lost(record):
            self.server_response(record, "verify")
            raise TimeoutError("synthetic-lost-verify")
        self.responses["verify"] = [lost]
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assertTrue(result.is_successful)
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 1)
        self.assertIn("unknown", [step["outcome"] for step in result.metadata["torobpay"]["active_operation"]["steps"]])

    def test_verify_timeout_before_application_is_not_blindly_retried(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        self.responses["verify"] = [TimeoutError("synthetic")]
        pending = process_callback(self.payment, self.callback_data(self.payment))
        self.assertFalse(pending.order.is_paid)
        self.assertTrue(pending.reservation_hold)
        process_callback(pending, self.callback_data(pending))
        self.assertEqual(len(self.api_calls("verify")), 1)
        self.assertEqual(len(self.api_calls("settle")), 0)
        self.outcome(pending, "W_FOR_SETTLE")
        recovered = reconcile_payment(pending)
        self.assertTrue(recovered.is_successful)
        self.assertEqual(len(self.api_calls("verify")), 1)

    def test_settle_timeout_after_application_reconciles_settle_without_resending(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        def lost(record):
            self.server_response(record, "settle")
            raise TimeoutError("synthetic")
        self.responses["settle"] = [lost]
        with patch("payments.torobpay_services.mark_order_paid", wraps=mark_order_paid) as paid:
            result = process_callback(self.payment, self.callback_data(self.payment))
            process_callback(result, self.callback_data(result))
        self.assertTrue(result.is_successful)
        paid.assert_called_once()
        self.assertEqual(len(self.api_calls("settle")), 1)

    def test_unapplied_settle_timeout_stays_held_until_automatic_remote_transition(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        self.responses["settle"] = [TimeoutError("synthetic")]
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        result = reconcile_payment(result)
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("settle")), 1)
        self.outcome(result, "ONGOING")
        result = reconcile_payment(result)
        self.assertTrue(result.is_successful)
        self.assertTrue(result.order.is_paid)
        self.assertEqual(len(self.api_calls("settle")), 1)

    def test_amount_or_transaction_mismatch_requires_review_before_verify(self):
        for field, value in (("amount", int(self.payment.amount) * 10 + 1), ("transactionId", str(uuid4()))):
            original = self.remote[self.payment.provider_token][field]
            self.remote[self.payment.provider_token][field] = value
            result = reconcile_payment(self.payment)
            self.assert_review(result)
            self.remote[self.payment.provider_token][field] = original
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_wrong_verify_transaction_never_calls_settle(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        self.responses["verify"] = [self.http_response({"successful": True, "response": {"transactionId": str(uuid4())}})]
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_missing_variant_stops_verify_and_fulfillment(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        OrderItem.objects.filter(order=self.order).update(variant=None)
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_inventory_lost_between_verify_and_settle_is_not_fulfilled(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        def remove_after_verify(record):
            response = self.server_response(record, "verify")
            OrderItem.objects.filter(order=self.order).update(variant=None)
            return response
        self.responses["verify"] = [remove_after_verify]
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("settle")), 0)
        self.assertTrue(any(step["operation"] == "verify" and step["outcome"] == "confirmed"
                            for step in result.metadata["torobpay"]["active_operation"]["steps"]))

    def test_late_confirmation_cannot_revive_cancelled_or_released_order(self):
        self.outcome(self.payment, "ONGOING")
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.CANCELLED, inventory_released=True,
                                                     payment_status=PaymentStatus.EXPIRED)
        self.variant.restore_stock(1)
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertTrue(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_post_deadline_pending_order_is_not_promoted_to_automatic_settlement(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        result = process_callback(self.payment, self.callback_data(self.payment))
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("settle")), 0)
        self.assertEqual(expire_due_orders(), 0)

    def test_expiration_status_pending_keeps_reservation(self):
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_orders(), 0)
        self.order.refresh_from_db()
        self.assertFalse(self.order.inventory_released)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_expiration_remote_revert_releases_stock_only_once(self):
        self.outcome(self.payment, "FAILED")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_orders(), 1)
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.reservation_hold)

    def test_failed_callback_does_not_erase_previously_seen_receipt(self):
        self.outcome(self.payment, "ONGOING")
        Order.objects.filter(pk=self.order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        reviewed = process_callback(self.payment, self.callback_data(self.payment))
        before = deepcopy(reviewed.metadata["torobpay"]["settlement_evidence"])
        self.outcome(reviewed, "FAILED")
        again = process_callback(reviewed, self.callback_data(reviewed, state="FAILED"))
        self.assert_review(again)
        self.assertEqual(again.metadata["torobpay"]["settlement_evidence"], before)

    def test_other_manual_receipt_blocks_credit_receipt_and_keeps_inventory(self):
        Payment.objects.create(order=self.order, gateway="zibal", authority="987654321", amount=self.order.total,
                               status=PaymentStatus.MANUAL_REVIEW)
        self.outcome(self.payment, "ONGOING")
        result = reconcile_payment(self.payment)
        self.assert_review(result)
        self.assertEqual(len(self.api_calls("settle")), 0)

    def test_dedicated_post_callback_uses_303_without_session_cookie_write(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("payments:torobpay_callback", args=[self.payment.request_id]),
                               urlencode(self.callback_data(self.payment)), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.url, reverse("payments:result_success", args=[self.order.order_number]))
        self.assertNotIn("sessionid", response.cookies)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn(self.payment.provider_token, response.url)

    def test_wrong_method_content_type_duplicate_invalid_and_oversized_callbacks(self):
        url = reverse("payments:torobpay_callback", args=[self.payment.request_id])
        body = urlencode(self.callback_data(self.payment))
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, self.callback_data(self.payment)).status_code, 400)
        for value in (body + "&state=OK", body + "&token=secret", body.replace("state=OK", "state=success"),
                      body.replace("amount=" + str(int(self.payment.amount) * 10), "amount=1.0"), "x" * 4097, "state=%FF"):
            with self.subTest(body=value[:50]):
                self.assertEqual(self.client.post(url, value, content_type="application/x-www-form-urlencoded").status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.callback_count, 0)
        self.assertEqual(len(self.api_calls("verify")), 0)

    def test_callback_route_and_body_uuid_amount_are_bound_to_same_attempt(self):
        url = reverse("payments:torobpay_callback", args=[self.payment.request_id])
        for changes in ({"transactionId": str(uuid4())}, {"amount": "1"}):
            response = self.client.post(url, urlencode({**self.callback_data(self.payment), **changes}),
                                        content_type="application/x-www-form-urlencoded")
            self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse("payments:torobpay_callback", args=[uuid4()]),
                                    urlencode(self.callback_data(self.payment)), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.api_calls("verify")), 0)

    def test_status_inspection_is_read_only_without_verify_settle_or_local_changes(self):
        self.outcome(self.payment, "W_FOR_VERIFY")
        before = Payment.objects.values().get(pk=self.payment.pk)
        report = inspect_payment(self.payment)
        self.assertEqual(report["api_status"], "PENDING")
        self.assertTrue(report["identity_matches"])
        self.assertEqual(Payment.objects.values().get(pk=self.payment.pk), before)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("settle")), 0)


class CPGCheckoutTests(CPGTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.customer)
        self.cart = Cart.objects.get(user=self.customer)
        CartItem.objects.create(cart=self.cart, variant=self.variant, quantity=1)
        session = self.client.session
        session["checkout_address_id"] = self.address.pk
        session.save()

    def test_eligible_checkout_shows_escaped_provider_copy_and_zibal_without_order_creation(self):
        self.title, self.description = "ترب‌پی <script>bad()</script>", "توضیح <b>آزمایشی</b>"
        before = Order.objects.count()
        response = self.client.get(reverse("orders:checkout_review"))
        self.assertContains(response, 'value="torobpay"')
        self.assertContains(response, 'value="zibal"')
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>bad()</script>")
        self.assertContains(response, "&lt;b&gt;")
        self.assertEqual(Order.objects.count(), before)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(len(self.api_calls("eligible")), 1)
        self.assertEqual(len(self.api_calls("token")), 0)
        self.assertEqual(self.api_calls("eligible")[0]["path"], "/api/online/offer/v1/eligible?amount=5500000")

    def test_ineligible_checkout_hides_torobpay_completely(self):
        self.eligible = False
        response = self.client.get(reverse("orders:checkout_review"))
        self.assertNotContains(response, 'value="torobpay"')
        self.assertContains(response, 'value="zibal"')

    def test_provider_network_auth_and_protocol_failure_hide_only_torobpay(self):
        for response in (TimeoutError("synthetic"), self.error(1099, status=403), self.http_response(body=b"bad-json")):
            self.responses["eligible"] = [response]
            with self.subTest(error=type(response).__name__):
                page = self.client.get(reverse("orders:checkout_review"))
                self.assertNotContains(page, 'value="torobpay"')
                self.assertContains(page, 'value="zibal"')
                self.assertEqual(page.status_code, 200)

    def test_missing_customer_address_details_hide_only_torobpay(self):
        for field in ("receiver_name", "province", "city", "address_line", "postal_code"):
            original = getattr(self.address, field)
            self.customer.addresses.filter(pk=self.address.pk).update(**{field: ""})
            page = self.client.get(reverse("orders:checkout_review"))
            self.assertNotContains(page, 'value="torobpay"')
            self.assertContains(page, 'value="zibal"')
            self.customer.addresses.filter(pk=self.address.pk).update(**{field: original})

    def test_optional_receiver_mobile_is_not_fabricated_when_missing(self):
        self.customer.addresses.filter(pk=self.address.pk).update(receiver_phone="")
        page = self.client.get(reverse("orders:checkout_review"))
        self.assertContains(page, 'value="torobpay"')
        self.client.post(reverse("orders:checkout_review"), {"payment_method": "torobpay"})
        self.assertNotIn("mobile", self.api_calls("token")[0]["payload"])

    def test_forged_disabled_ineligible_or_duplicate_post_selection_creates_nothing(self):
        before = Order.objects.count()
        self.eligible = False
        response = self.client.post(reverse("orders:checkout_review"), {"payment_method": "torobpay"})
        self.assertEqual(response.url, reverse("orders:checkout_review"))
        with override_settings(PAYMENT_GATEWAYS=[]):
            response = self.client.post(reverse("orders:checkout_review"), {"payment_method": "torobpay"})
            self.assertEqual(response.url, reverse("orders:checkout_review"))
        response = self.client.post(reverse("orders:checkout_review"), {"payment_method": ["zibal", "torobpay"]})
        self.assertEqual(response.url, reverse("orders:checkout_review"))
        self.assertEqual(Order.objects.count(), before)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(len(self.api_calls("token")), 0)

    def test_selected_provider_and_current_snapshot_authorization_create_one_token(self):
        response = self.client.post(reverse("orders:checkout_review"), {"payment_method": "torobpay", "amount": "1", "note": "یادداشت"})
        payment = Payment.objects.get()
        self.assertEqual(response.url, reverse("payments:redirect", args=[payment.pk]))
        self.assertEqual(payment.gateway, "torobpay")
        self.assertEqual(payment.amount, 550000)
        self.assertEqual(len(self.api_calls("eligible")), 1)
        self.assertEqual(len(self.api_calls("token")), 1)

    def test_current_amount_and_each_customer_have_fresh_eligibility(self):
        self.client.get(reverse("orders:checkout_review"))
        self.product.base_price = 600000
        self.product.save(update_fields=["base_price"])
        self.client.get(reverse("orders:checkout_review"))
        calls = self.api_calls("eligible")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["path"], calls[1]["path"])
        other = User.objects.create_user(phone_number="09120005555", first_name="مشتری", last_name="دیگر")
        other_address = Address.objects.create(user=other, receiver_name="مشتری دیگر", receiver_phone=other.phone_number,
                                               province="تهران", city="تهران", address_line="نشانی مشتری دیگر", postal_code="1234567890")
        options = payment_options(customer=other, amount=Decimal("550000"), address=other_address)
        self.assertTrue(any(option.name == "torobpay" for option in options))
        self.assertEqual(len(self.api_calls("eligible")), 3)

    def test_changed_checkout_price_is_reauthorized_for_persisted_order(self):
        original = create_order
        def change_price(**kwargs):
            Product.objects.filter(pk=self.product.pk).update(base_price=700000)
            return original(**kwargs)
        with patch("orders.views.create_order", side_effect=change_price):
            self.client.post(reverse("orders:checkout_review"), {"payment_method": "torobpay"})
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, 750000)
        self.assertEqual(self.api_calls("eligible")[-1]["path"], "/api/online/offer/v1/eligible?amount=7500000")


class CPGCancellationAndUpdateTests(CPGTestCase):
    def test_unrelated_customer_cannot_cancel_or_update_or_bind_token(self):
        payment = self.start()
        stranger = User.objects.create_user(phone_number="09120006666")
        for operation in (
            lambda: cancel_payment(payment, actor=stranger),
            lambda: update_payment(payment, quantities={self.order.items.get().pk: 0}, actor=stranger),
            lambda: bind_recovered_token(payment, payment.provider_token, actor=stranger),
        ):
            with self.assertRaises(PaymentError):
                operation()
        self.assertEqual(len(self.api_calls("cancel")), 0)
        self.assertEqual(len(self.api_calls("update")), 0)

    def test_plain_local_admin_cancellation_cannot_release_credit_inventory(self):
        self.start()
        with self.assertRaises(OrderError):
            cancel_order(self.order)
        with self.assertRaises(OrderError):
            transition_status(self.order, OrderStatus.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_cancel_requires_confirmed_provider_revert_before_stock_release(self):
        payment = self.pay(self.start())
        result = cancel_payment(payment, actor=self.customer)
        self.assertEqual(result.status, PaymentStatus.CANCELLED)
        self.assertTrue(result.metadata["torobpay"]["cancellation_confirmed"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.payment_status, PaymentStatus.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)
        calls = len(self.api_calls("cancel"))
        cancel_payment(result, actor=self.customer)
        self.assertEqual(len(self.api_calls("cancel")), calls)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_cancel_timeout_stays_unknown_and_cannot_double_refund(self):
        payment = self.pay(self.start())
        self.responses["cancel"] = [TimeoutError("synthetic")]
        result = cancel_payment(payment, actor=self.customer)
        self.assertEqual(result.metadata["torobpay"]["active_operation"]["status"], "uncertain")
        self.assertTrue(result.reservation_hold)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        cancel_payment(result, actor=self.customer)
        self.assertEqual(len(self.api_calls("cancel")), 1)
        self.outcome(result, "CANCELED")
        recovered = reconcile_payment(result)
        self.assertEqual(recovered.status, PaymentStatus.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_cancel_response_lost_after_remote_refund_reconciles_without_second_post(self):
        payment = self.pay(self.start())
        def lost(record):
            self.server_response(record, "cancel")
            raise TimeoutError("synthetic")
        self.responses["cancel"] = [lost]
        result = cancel_payment(payment, actor=self.customer)
        self.assertEqual(result.status, PaymentStatus.CANCELLED)
        self.assertEqual(len(self.api_calls("cancel")), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_pending_status_alone_does_not_authorize_revert_or_cancel(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        with self.assertRaises(PaymentError):
            cancel_payment(payment, actor=self.customer)
        self.assertEqual(len(self.api_calls("revert")), 0)
        self.assertEqual(len(self.api_calls("cancel")), 0)

    def test_revert_uses_staff_confirmed_exact_state_and_actual_time_under_30_minutes(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        evidence = RemoteEvidence("W_FOR_VERIFY", "SUP-synthetic", paid_at=timezone.now())
        result = cancel_payment(payment, actor=self.staff(), evidence=evidence)
        self.assertEqual(result.status, PaymentStatus.CANCELLED)
        self.assertEqual(len(self.api_calls("revert")), 1)
        self.assertEqual(len(self.api_calls("cancel")), 0)

    def test_revert_at_or_after_30_minute_boundary_selects_cancel(self):
        for seconds in (1800, 1801):
            if seconds == 1801:
                cart = Cart.objects.get(user=self.customer)
                CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
                self.order = create_order(customer=self.customer, cart=cart, address=self.address)
            payment = self.start()
            Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=1))
            self.outcome(payment, "W_FOR_VERIFY")
            actor = self.staff() if seconds == 1800 else User.objects.get(phone_number="09120002222")
            evidence = RemoteEvidence("W_FOR_VERIFY", "SUP-synthetic", paid_at=timezone.now() - timedelta(seconds=seconds))
            result = cancel_payment(payment, actor=actor, evidence=evidence)
            self.assertEqual(result.status, PaymentStatus.CANCELLED)
        self.assertEqual(len(self.api_calls("revert")), 0)
        self.assertEqual(len(self.api_calls("cancel")), 2)

    def test_revert_1065_refreshes_status_before_legal_cancel(self):
        payment = self.start()
        self.outcome(payment, "W_FOR_VERIFY")
        def too_late(record):
            self.outcome(payment, "W_FOR_SETTLE")
            return self.error(1065)
        self.responses["revert"] = [too_late]
        result = cancel_payment(payment, actor=self.staff(), evidence=RemoteEvidence("W_FOR_VERIFY", "SUP-synthetic", timezone.now()))
        self.assertEqual(result.status, PaymentStatus.CANCELLED)
        operations = [record["path"].split("?")[0].rsplit("/", 1)[-1] for record in self.requests]
        revert_index = operations.index("revert")
        self.assertEqual(operations[revert_index + 1:revert_index + 3], ["status", "cancel"])

    def test_adapter_revert_rejects_settled_states_unknown_future_and_old_times(self):
        payment = self.start()
        gateway = TorobPayGateway()
        now = timezone.now()
        for state, paid_at in (("ONGOING", now), ("W_FOR_SETTLE", now), ("W_FOR_VERIFY", None),
                               ("W_FOR_VERIFY", now - timedelta(minutes=30)), ("W_FOR_VERIFY", now + timedelta(seconds=1))):
            with self.subTest(state=state), self.assertRaises(PaymentGatewayError):
                gateway.revert(payment, remote_state=state, paid_at=paid_at, now=now)
        self.assertEqual(len(self.api_calls("revert")), 0)

    def test_shipped_order_rejects_refund_before_remote_mutation(self):
        payment = self.pay(self.start())
        transition_status(payment.order, OrderStatus.PREPARING)
        transition_status(payment.order, OrderStatus.READY_TO_SHIP)
        transition_status(payment.order, OrderStatus.SHIPPED)
        with self.assertRaises(PaymentError):
            cancel_payment(payment, actor=self.customer)
        self.assertEqual(len(self.api_calls("cancel")), 0)

    def test_strict_decrease_updates_saved_quantities_after_remote_confirmation(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        original = {field: getattr(payment, field) for field in ("amount", "authority", "transaction_id", "paid_at", "verified_at", "gateway_paid_at")}
        item = self.order.items.get()
        result = update_payment(payment, quantities={item.pk: 1}, actor=self.staff())
        self.order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.order.total, 550000)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(result.metadata["torobpay"]["amount_rial"], 5500000)
        self.assertTrue(result.metadata["torobpay"]["transaction_id"].startswith("updated-"))
        self.assertEqual(result.metadata["torobpay"]["last_known_order_state"], "UPDATED")
        for field, expected in original.items():
            self.assertEqual(getattr(result, field), expected)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        payload = self.api_calls("update")[0]["payload"]
        self.assertEqual(payload["amount"], 5500000)
        self.assertEqual(payload["paymentToken"], payment.provider_token)
        self.assertEqual(len(payload["cartList"]), 1)
        self.assertEqual(payload["cartList"][0]["cartItems"][0]["count"], 1)

    def test_equal_increased_foreign_or_discounted_update_never_calls_remote_update(self):
        payment = self.pay(self.start())
        item = self.order.items.get()
        for quantities in ({item.pk: 1}, {item.pk: 2}, {999999: 0}):
            with self.subTest(quantities=quantities), self.assertRaises(PaymentError):
                update_payment(payment, quantities=quantities, actor=self.staff() if not User.objects.filter(is_superuser=True).exists() else User.objects.get(is_superuser=True))
        Order.objects.filter(pk=self.order.pk).update(discount_total=1)
        with self.assertRaises(PaymentError):
            update_payment(payment, quantities={item.pk: 0}, actor=User.objects.get(is_superuser=True))
        self.assertEqual(len(self.api_calls("update")), 0)

    def test_update_timeout_keeps_local_prices_stock_and_no_duplicate_post(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        item = self.order.items.get()
        actor = self.staff()
        self.responses["update"] = [TimeoutError("synthetic")]
        result = update_payment(payment, quantities={item.pk: 1}, actor=actor)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, 1050000)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
        update_payment(result, quantities={item.pk: 1}, actor=actor)
        self.assertEqual(len(self.api_calls("update")), 1)

    def test_lost_update_response_can_reconcile_same_transaction_updated_amount_once(self):
        self.use_quantity(2)
        payment = self.pay(self.start())
        item = self.order.items.get()
        def lost(record):
            remote = self.remote[payment.provider_token]
            remote.update(amount=record["payload"]["amount"], state="UPDATED")
            raise TimeoutError("synthetic")
        self.responses["update"] = [lost]
        result = update_payment(payment, quantities={item.pk: 1}, actor=self.staff())
        self.assertEqual(result.status, PaymentStatus.SUCCESSFUL)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, 550000)
        reconcile_payment(result)
        self.assertEqual(len(self.api_calls("update")), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_settle_family_without_exact_ongoing_evidence_does_not_authorize_update(self):
        self.use_quantity(2)
        payment = self.start()
        self.outcome(payment, "ONGOING")
        payment = reconcile_payment(payment)
        self.assertTrue(payment.is_successful)
        with self.assertRaises(PaymentError):
            update_payment(payment, quantities={self.order.items.get().pk: 1}, actor=self.staff())
        self.assertEqual(len(self.api_calls("update")), 0)

    def test_recovered_create_token_is_bound_only_after_uuid_amount_status_validation(self):
        def lost(record):
            self.server_response(record, "token")
            raise TimeoutError("synthetic")
        self.responses["token"] = [lost]
        with self.assertRaises(PaymentError):
            self.start()
        payment = Payment.objects.get()
        token = next(iter(self.remote))
        result = bind_recovered_token(payment, token, actor=self.staff())
        self.assertEqual(result.provider_token, token)
        self.assertEqual(result.creation_status, PaymentCreationStatus.REVIEW)
        self.assertFalse(result.order.is_paid)
        self.assertEqual(len(self.api_calls("verify")), 0)
        self.assertEqual(len(self.api_calls("token")), 1)
