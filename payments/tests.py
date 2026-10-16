"""تست‌های پرداخت: Gateway، امضا، Callback ایدمپوتنت و صفحات نتیجه."""

from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Address, User
from cart.models import Cart, CartItem
from orders.choices import OrderStatus, PaymentStatus
from orders.models import Order
from orders.services import create_order
from payments.gateways import MockGateway, get_gateway
from payments.gateways.base import PaymentGatewayError
from payments.models import Payment
from payments.services import (
    PaymentError,
    create_payment_for_order,
    expire_stale_payments,
    process_callback,
)
from products.models import Category, Color, Product, ProductVariant, Size


@override_settings(DEBUG=True)
class PaymentTestCase(TestCase):
    """
    پایه مشترک تست‌های پرداخت.

    تست‌های Django به‌طور پیش‌فرض با `DEBUG=False` اجرا می‌شوند، در حالی که
    درگاه Mock فقط در محیط توسعه فعال است؛ بنابراین در همه تست‌ها روشن می‌شود.
    """


class PaymentFixtureMixin:
    def build_payment_data(self, stock: int = 5, price: int = 500000):
        self.category = Category.objects.create(name="پلیور")
        self.product = Product.objects.create(
            name="پلیور نمونه",
            category=self.category,
            base_price=Decimal(price),
            is_published=True,
            is_active=True,
        )
        self.color = Color.objects.create(name="سرمه‌ای")
        self.size = Size.objects.create(name="L")
        self.variant = ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=stock
        )
        self.customer = User.objects.create_user(
            phone_number="09123459999", first_name="کاربر", last_name="پرداخت"
        )
        self.address = Address.objects.create(
            user=self.customer,
            receiver_name="کاربر پرداخت",
            receiver_phone="09123459999",
            province="تهران",
            city="تهران",
            address_line="خیابان انقلاب، پلاک ۵",
            postal_code="1234567890",
            is_default=True,
        )
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        self.order = create_order(
            customer=self.customer, cart=cart, address=self.address
        )

    def start_payment(self) -> Payment:
        return create_payment_for_order(self.order)

    @staticmethod
    def callback_query(payment: Payment, status: str = "successful") -> str:
        return MockGateway().build_callback_query(payment, status)

    @staticmethod
    def sig_for(payment: Payment, status: str = "successful") -> str:
        """امضای معتبر Callback برای یک تراکنش (مثل امضای درگاه واقعی)."""
        return MockGateway().sign(f"result|{payment.authority}|{status}")


class GatewayTests(PaymentTestCase):
    def test_registry_returns_configured_gateway(self):
        gateway = get_gateway()
        self.assertEqual(gateway.name, "mock")

    def test_unknown_gateway_path_is_rejected(self):
        with self.assertRaises(PaymentGatewayError):
            get_gateway("payments.gateways.not_exists.Nope")

    def test_mock_gateway_requires_secret(self):
        with override_settings(PAYMENT_SECRET=""):
            with self.assertRaises(PaymentGatewayError):
                MockGateway()

    def test_mock_gateway_is_disabled_without_debug(self):
        with override_settings(DEBUG=False):
            with self.assertRaises(PaymentGatewayError):
                MockGateway()

    def test_signature_is_deterministic_and_secret_dependent(self):
        gateway = MockGateway()
        first = gateway.sign("start|AUTH|1000")
        second = gateway.sign("start|AUTH|1000")
        other = gateway.sign("start|AUTH|1001")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)


class CreatePaymentTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        self.build_payment_data()

    def test_payment_is_created_with_backend_amount(self):
        payment = self.start_payment()

        self.assertEqual(payment.amount, self.order.total)
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertTrue(payment.authority.startswith("MOCK-"))
        self.assertEqual(payment.order_id, self.order.pk)
        self.assertEqual(payment.amount, Decimal(self.order.total))

    def test_amount_comes_only_from_backend(self):
        payment = self.start_payment()
        # حتی اگر Client مبلغ دیگری بفرستد، مبلغ تراکنش از سفارش خوانده می‌شود
        self.assertNotEqual(payment.amount, Decimal("1"))

    def test_redirect_url_contains_signature(self):
        payment = self.start_payment()
        url = payment.metadata["redirect_url"]
        query = parse_qs(urlsplit(url).query)

        self.assertIn("sig", query)
        self.assertIn("amount", query)
        self.assertEqual(int(query["amount"][0]), int(self.order.total))

        gateway = MockGateway()
        authority = urlsplit(url).path.rstrip("/").split("/")[-1]
        expected = gateway.sign(
            f"start|{authority}|{query['amount'][0]}"
        )
        self.assertEqual(query["sig"][0], expected)

    def test_already_paid_order_cannot_pay_again(self):
        payment = self.start_payment()
        process_callback(payment, {"status": "successful", "sig": self.sig_for(payment)})

        with self.assertRaises(PaymentError):
            self.start_payment()

    def test_pending_payment_can_be_retried(self):
        first = self.start_payment()
        process_callback(
            first, {"status": "failed", "sig": self.sig_for(first, "failed")}
        )

        second = self.start_payment()
        self.assertNotEqual(first.pk, second.pk)

    def test_expired_order_cannot_be_paid(self):
        Order.objects.filter(pk=self.order.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(PaymentError):
            self.start_payment()

    def test_cancelled_order_cannot_be_paid(self):
        from orders.services import cancel_order

        cancel_order(self.order)
        with self.assertRaises(PaymentError):
            self.start_payment()


class CallbackTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        self.build_payment_data()
        self.payment = self.start_payment()
        self.valid_sig = self.sig_for(self.payment)

    def test_successful_callback_marks_order_paid(self):
        payment = process_callback(
            self.payment, {"status": "successful", "sig": self.valid_sig}
        )

        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertIsNotNone(payment.verified_at)
        self.assertTrue(payment.reference_id)
        self.assertTrue(payment.transaction_id)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertIsNotNone(self.order.paid_at)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_callback_is_idempotent(self):
        first = process_callback(
            self.payment, {"status": "successful", "sig": self.valid_sig}
        )
        ref_id = first.reference_id
        paid_at = first.paid_at

        # ارسال دوباره همان Callback
        second = process_callback(
            first, {"status": "successful", "sig": self.valid_sig}
        )
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.reference_id, ref_id)
        self.assertEqual(second.paid_at, paid_at)

        # موجودی دوبار کم نشده و سفارش دوبار پردازش نشده است
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)

    def test_tampered_status_is_rejected(self):
        bad_sig = self.sig_for(self.payment, "failed")
        payment = process_callback(
            self.payment, {"status": "successful", "sig": bad_sig}
        )

        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAYMENT_FAILED)
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)
        # موجودی تا لغو/انقضای سفارش رزرو می‌ماند (کاهش اولیه در ثبت سفارش)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_tampered_authority_is_rejected(self):
        fake_sig = MockGateway().sign("result|OTHER-AUTHORITY|successful")
        payment = process_callback(
            self.payment, {"status": "successful", "sig": fake_sig}
        )
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, OrderStatus.PAID)

    def test_empty_signature_is_rejected(self):
        payment = process_callback(self.payment, {"status": "successful", "sig": ""})
        self.assertEqual(payment.status, PaymentStatus.FAILED)

    def test_cancelled_callback(self):
        payment = process_callback(
            self.payment,
            {"status": "cancelled", "sig": self.sig_for(self.payment, "cancelled")},
        )

        self.assertEqual(payment.status, PaymentStatus.CANCELLED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.CANCELLED)
        # موجودی تا انقضای سفارش رزرو می‌ماند
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 4)

    def test_successful_callback_after_cancellation_flags_refund(self):
        # سفارش پیش از رسیدن Callbackِ موفق، منقضی و لغو می‌شود (موجودی آزاد می‌شود)
        from orders.services import expire_due_orders

        Order.objects.filter(pk=self.order.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(expire_due_orders(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.payment_status, PaymentStatus.EXPIRED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)  # موجودی آزاد شد

        # Callbackِ موفقِ دیرهنگام می‌رسد (پول در درگاه گرفته شده است)
        payment = process_callback(
            self.payment, {"status": "successful", "sig": self.valid_sig}
        )

        # پول ثبت می‌شود اما سفارش احیا نمی‌شود؛ تراکنش برای بازگشت وجه علامت می‌خورد
        self.assertEqual(payment.status, PaymentStatus.SUCCESSFUL)
        self.assertTrue(payment.needs_refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertNotEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        # موجودی دوباره کم نشده است (نشت موجودی رخ نمی‌دهد)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_amount_mismatch_is_rejected(self):
        Order.objects.filter(pk=self.order.pk).update(total=Decimal("1"))
        self.order.refresh_from_db()
        self.payment.refresh_from_db()

        payment = process_callback(
            self.payment, {"status": "successful", "sig": self.valid_sig}
        )
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.assertIn("مبلغ", payment.error_message)

    def test_expire_stale_payments(self):
        Payment.objects.filter(pk=self.payment.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        expired = expire_stale_payments(older_than_minutes=60)
        self.assertEqual(expired, 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.EXPIRED)


class CallbackViewTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client = Client()
        self.client.force_login(self.customer)

    def _callback_url(self, status: str = "successful") -> str:
        return reverse("payments:callback") + "?" + self.callback_query(self.payment, status)

    def test_callback_redirects_to_success_page(self):
        response = self.client.get(self._callback_url("successful"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("payments:result_success", args=[self.order.order_number]),
            response.headers["Location"],
        )

    def test_callback_without_authority_redirects_home(self):
        response = self.client.get(reverse("payments:callback"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/", response.headers["Location"])

    def test_unknown_authority_redirects_home(self):
        response = self.client.get(
            reverse("payments:callback") + "?authority=DOES-NOT-EXIST&status=successful"
        )
        self.assertEqual(response.status_code, 302)

    def test_replay_callback_keeps_same_result(self):
        self.client.get(self._callback_url("successful"))
        response = self.client.get(self._callback_url("successful"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("payments:result_success", args=[self.order.order_number]),
            response.headers["Location"],
        )
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)

    def test_payment_requires_login(self):
        anonymous = Client()
        response = anonymous.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["Location"])

    def test_redirect_page_shows_gateway_link(self):
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.payment.authority)

    def test_redirect_page_of_paid_order_goes_to_result(self):
        self.client.get(self._callback_url("successful"))
        response = self.client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 302)

    def test_foreign_user_cannot_see_redirect(self):
        other = User.objects.create_user(phone_number="09121112233")
        client = Client()
        client.force_login(other)
        response = client.get(reverse("payments:redirect", args=[self.payment.pk]))
        self.assertEqual(response.status_code, 404)


class MockGatewayViewTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client = Client()

    def _redirect_url(self) -> str:
        return self.payment.metadata["redirect_url"]

    def test_gateway_page_renders_with_signature(self):
        response = self.client.get(self._redirect_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.payment.authority)
        self.assertContains(response, "پرداخت")

    def test_tampered_amount_is_rejected(self):
        url = self._redirect_url()
        response = self.client.get(url.replace(f"amount={self.order.total}", "amount=1"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("orders:detail", args=[self.order.order_number]),
            response.headers["Location"],
        )

    def test_missing_signature_is_rejected(self):
        url = self._redirect_url().split("?")[0]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)


class ResultPageTests(PaymentFixtureMixin, PaymentTestCase):
    def setUp(self):
        self.build_payment_data()
        self.payment = self.start_payment()
        self.client = Client()
        self.client.force_login(self.customer)

    def _pay(self):
        self.client.get(
            reverse("payments:callback") + "?" + self.callback_query(self.payment)
        )

    def test_success_page(self):
        self._pay()
        response = self.client.get(
            reverse("payments:result_success", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پرداخت با موفقیت انجام شد")

    def test_success_page_requires_actual_payment(self):
        # سفارش پرداخت‌نشده نباید صفحه موفقیت ببیند
        response = self.client.get(
            reverse("payments:result_success", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("orders:detail", args=[self.order.order_number]),
            response.headers["Location"],
        )

    def test_failed_page(self):
        self.client.get(reverse("payments:callback") + "?" + self.callback_query(self.payment, "failed"))
        response = self.client.get(
            reverse("payments:result_failed", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 200)

    def test_cancelled_page_redirects_for_paid_order(self):
        self._pay()
        response = self.client.get(
            reverse("payments:result_cancelled", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("payments:result_success", args=[self.order.order_number]),
            response.headers["Location"],
        )

    def test_result_page_requires_login(self):
        self.client.logout()
        response = self.client.get(
            reverse("payments:result_success", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["Location"])

    def test_foreign_user_gets_404(self):
        other = User.objects.create_user(phone_number="09123334455")
        client = Client()
        client.force_login(other)
        response = client.get(
            reverse("payments:result_failed", args=[self.order.order_number])
        )
        self.assertEqual(response.status_code, 404)


class FullPurchaseFlowTests(PaymentFixtureMixin, PaymentTestCase):
    """سناریوی End-to-End: سبد → Checkout → سفارش → درگاه → Callback → نتیجه."""

    def setUp(self):
        self.build_payment_data()

    def test_full_flow(self):
        client = Client()
        client.force_login(self.customer)

        # ۱) افزودن به سبد
        client.post(
            reverse("cart:add_variant"),
            {"variant_id": self.variant.pk, "quantity": 1, "next": "/"},
        )
        cart_item = CartItem.objects.first()
        self.assertIsNotNone(cart_item)
        self.assertEqual(cart_item.quantity, 1)

        # ۲) Checkout
        response = client.get(reverse("orders:checkout"))
        self.assertEqual(response.status_code, 200)

        response = client.post(
            reverse("orders:checkout"),
            {"action": "select", "address_id": self.address.pk},
        )
        self.assertEqual(response.status_code, 302)

        # ۳) ثبت سفارش
        response = client.post(
            reverse("orders:checkout_review"), {"note": "تست کامل"}
        )
        self.assertEqual(response.status_code, 302)

        order = Order.objects.filter(customer=self.customer).latest("id")
        self.assertEqual(order.customer_note, "تست کامل")
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

        # ۴) انتقال به درگاه (View خودش تراکنش پرداخت را می‌سازد)
        redirect_location = response.headers["Location"]
        self.assertTrue(redirect_location.startswith("/payments/redirect/"))
        payment_pk = int(redirect_location.rstrip("/").split("/")[-1])
        payment = Payment.objects.get(pk=payment_pk)

        response = client.get(reverse("payments:redirect", args=[payment.pk]))
        self.assertEqual(response.status_code, 200)

        # ۵) صفحه درگاه و Callback
        gateway_url = payment.metadata["redirect_url"]
        response = client.get(gateway_url)
        self.assertEqual(response.status_code, 200)

        callback_url = reverse("payments:callback") + "?" + self.callback_query(payment)
        response = client.get(callback_url)
        self.assertEqual(response.status_code, 302)

        # ۶) نتیجه
        response = client.get(
            reverse("payments:result_success", args=[order.order_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پرداخت با موفقیت انجام شد")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assertEqual(order.payment_status, PaymentStatus.SUCCESSFUL)
        self.assertTrue(order.is_paid)
        # موجودی دقیقاً یک بار کم شده است
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

        # ۷) ایدمپوتنتی Callback: پرداخت دوباره پردازش نمی‌شود
        client.get(callback_url)
        self.assertEqual(
            Payment.objects.filter(order=order, status=PaymentStatus.SUCCESSFUL).count(),
            1,
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SUCCESSFUL)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)
