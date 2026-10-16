"""تست‌های سفارش: ثبت اتمیک، Snapshot، جلوگیری از Overselling و گذار وضعیت."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from accounts.models import Address, User
from cart.models import Cart, CartItem
from orders.choices import ALLOWED_ORDER_TRANSITIONS, OrderStatus, PaymentStatus
from orders.models import Order, OrderItem
from orders.services import (
    OrderError,
    OrderNotPayable,
    cancel_order,
    create_order,
    expire_due_orders,
    mark_order_paid,
    transition_status,
)
from orders.shipping import default_shipping_option, get_shipping_options
from products.models import Category, Color, Discount, Product, ProductVariant, Size


class OrderFixtureMixin:
    def build_order_data(self, stock: int = 5, price: int = 500000):
        self.category = Category.objects.create(name="تی‌شرت")
        self.product = Product.objects.create(
            name="تی‌شرت نمونه",
            category=self.category,
            base_price=Decimal(price),
            is_published=True,
            is_active=True,
        )
        self.color = Color.objects.create(name="مشکی")
        self.size = Size.objects.create(name="M")
        self.variant = ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=stock
        )
        self.customer = User.objects.create_user(
            phone_number="09123450001", first_name="کاربر", last_name="سفارش"
        )
        self.address = Address.objects.create(
            user=self.customer,
            receiver_name="کاربر سفارش",
            receiver_phone="09123450001",
            province="تهران",
            city="تهران",
            address_line="خیابان آزادی، پلاک ۱۲، واحد ۳",
            postal_code="1234567890",
            is_default=True,
        )

    def make_cart(self, quantity: int, variant=None) -> Cart:
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(
            cart=cart, variant=variant or self.variant, quantity=quantity
        )
        return cart


class CreateOrderTests(OrderFixtureMixin, TestCase):
    def setUp(self):
        self.build_order_data()

    def test_order_is_created_with_snapshots(self):
        cart = self.make_cart(2)
        order = create_order(
            customer=self.customer, cart=cart, address=self.address, customer_note="یادداشت"
        )

        self.assertEqual(order.items.count(), 1)
        item = order.items.get()

        # Snapshot: نام/رنگ/سایز/قیمت در لحظه خرید ثبت می‌شود
        self.assertEqual(item.product_name, "تی‌شرت نمونه")
        self.assertEqual(item.color_name, "مشکی")
        self.assertEqual(item.size_name, "M")
        self.assertEqual(item.original_unit_price, Decimal("500000"))
        self.assertEqual(item.unit_price, Decimal("500000"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.line_total, Decimal("1000000"))

        # Snapshot آدرس
        self.assertEqual(order.receiver_name, "کاربر سفارش")
        self.assertEqual(order.province, "تهران")
        self.assertEqual(order.customer_note, "یادداشت")
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.PENDING)

    def test_shipping_fee_and_total_are_computed_in_backend(self):
        cart = self.make_cart(1)
        order = create_order(customer=self.customer, cart=cart, address=self.address)

        self.assertEqual(order.subtotal, Decimal("500000"))
        expected_shipping = Decimal(settings.SHIPPING_FEE)
        self.assertEqual(order.shipping_fee, expected_shipping)
        self.assertEqual(order.total, Decimal("500000") + expected_shipping)

    def test_free_shipping_threshold(self):
        option = get_shipping_options()[0]
        cart = self.make_cart(1, self._big_variant())
        order = create_order(
            customer=self.customer,
            cart=cart,
            address=self.address,
            shipping_option=option,
        )
        self.assertEqual(order.shipping_fee, Decimal("0"))

    def _big_variant(self):
        expensive = Product.objects.create(
            name="محصول گران",
            category=self.category,
            base_price=Decimal(settings.FREE_SHIPPING_MIN_AMOUNT) + Decimal(1000),
            is_published=True,
            is_active=True,
        )
        return ProductVariant.objects.create(
            product=expensive, color=self.color, size=self.size, stock=5
        )

    def test_stock_is_reserved(self):
        cart = self.make_cart(2)
        create_order(customer=self.customer, cart=cart, address=self.address)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

    def test_cart_is_cleared_after_order(self):
        cart = self.make_cart(2)
        create_order(customer=self.customer, cart=cart, address=self.address)
        self.assertEqual(cart.items.count(), 0)

    def test_discount_is_applied_at_order_time(self):
        Discount.objects.create(
            name="تخفیف",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=20,
            is_active=True,
        )
        cart = self.make_cart(2)
        order = create_order(customer=self.customer, cart=cart, address=self.address)

        self.assertEqual(order.subtotal, Decimal("1000000"))
        self.assertEqual(order.discount_total, Decimal("200000"))
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("400000"))
        self.assertEqual(item.line_total, Decimal("800000"))

    def test_empty_cart_raises(self):
        empty = Cart.objects.create(user=self.customer)
        with self.assertRaises(OrderError):
            create_order(
                customer=self.customer, cart=empty, address=self.address
            )

    def test_order_number_is_unique(self):
        first = create_order(
            customer=self.customer, cart=self.make_cart(1), address=self.address
        )
        second = create_order(
            customer=self.customer, cart=self.make_cart(1), address=self.address
        )
        self.assertNotEqual(first.order_number, second.order_number)

    def test_order_number_format(self):
        order = create_order(
            customer=self.customer, cart=self.make_cart(1), address=self.address
        )
        self.assertTrue(order.order_number.startswith("ORD-"))
        self.assertEqual(len(order.order_number), len("ORD-20261009-ABCDEF"))


class OversellingProtectionTests(OrderFixtureMixin, TestCase):
    """جلوگیری از فروش بیش از موجودی با Transaction + Row Lock + شرط SQL."""

    def setUp(self):
        self.build_order_data(stock=4)

    def test_second_order_cannot_exceed_stock(self):
        first = create_order(
            customer=self.customer, cart=self.make_cart(4), address=self.address
        )
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)

        # شبیه‌سازی درخواست همزمان: ردیف سبد بدون بررسی موجودی ساخته شده است
        second_cart = self.make_cart(3)
        with self.assertRaises(OrderError):
            create_order(
                customer=self.customer, cart=second_cart, address=self.address
            )

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        # سفارش دوم ثبت نشده است
        self.assertEqual(Order.objects.filter(status=OrderStatus.PENDING_PAYMENT).count(), 1)
        self.assertEqual(first.items.count(), 1)

    def test_partial_stock_allows_reduced_order(self):
        create_order(
            customer=self.customer, cart=self.make_cart(3), address=self.address
        )
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)

        cart = self.make_cart(1)
        order = create_order(customer=self.customer, cart=cart, address=self.address)
        self.assertEqual(order.items.get().quantity, 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)

    def test_unpublished_product_cannot_be_ordered(self):
        cart = self.make_cart(1)
        Product.objects.filter(pk=self.product.pk).update(is_published=False)
        with self.assertRaises(OrderError):
            create_order(customer=self.customer, cart=cart, address=self.address)
        self.assertEqual(Order.objects.count(), 0)

    def test_inactive_variant_cannot_be_ordered(self):
        cart = self.make_cart(1)
        ProductVariant.objects.filter(pk=self.variant.pk).update(is_active=False)
        with self.assertRaises(OrderError):
            create_order(customer=self.customer, cart=cart, address=self.address)

    def test_quantity_over_max_is_rejected(self):
        self.variant.stock = settings.MAX_QUANTITY_PER_ITEM + 5
        self.variant.save(update_fields=["stock"])
        cart = self.make_cart(settings.MAX_QUANTITY_PER_ITEM + 1)
        with self.assertRaises(OrderError):
            create_order(customer=self.customer, cart=cart, address=self.address)

    def test_decrement_stock_is_conditional(self):
        # شرط SQL: کاهش فقط اگر موجودی کافی باشد
        self.assertTrue(self.variant.decrement_stock(4))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        self.assertFalse(self.variant.decrement_stock(1))


class OrderLifecycleTests(OrderFixtureMixin, TestCase):
    def setUp(self):
        self.build_order_data()
        self.order = create_order(
            customer=self.customer, cart=self.make_cart(2), address=self.address
        )

    def test_mark_order_paid_is_idempotent(self):
        mark_order_paid(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)
        paid_at = self.order.paid_at

        mark_order_paid(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid_at, paid_at)

    def test_cancel_order_restores_stock(self):
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

        cancel_order(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_cancel_is_idempotent(self):
        cancel_order(self.order)
        self.variant.refresh_from_db()
        stock_after_first_cancel = self.variant.stock

        cancel_order(self.order)
        self.variant.refresh_from_db()
        # موجودی دوبار اضافه نمی‌شود
        self.assertEqual(self.variant.stock, stock_after_first_cancel)

    def test_transition_to_cancelled_restores_stock(self):
        # باگ قبلی: لغو از مسیر ادمین (transition_status) موجودی را آزاد نمی‌کرد
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

        transition_status(self.order, OrderStatus.CANCELLED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_mark_order_paid_refuses_cancelled_order(self):
        # باگ قبلی: Callback موفقِ دیرهنگام، سفارش لغو‌شده را «پرداخت‌شده» می‌کرد
        cancel_order(self.order)
        with self.assertRaises(OrderNotPayable):
            mark_order_paid(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertNotEqual(self.order.payment_status, PaymentStatus.SUCCESSFUL)

    def test_shipped_order_cannot_be_cancelled(self):
        mark_order_paid(self.order)
        transition_status(self.order, OrderStatus.PREPARING)
        transition_status(self.order, OrderStatus.READY_TO_SHIP)
        transition_status(self.order, OrderStatus.SHIPPED)

        with self.assertRaises(OrderError):
            cancel_order(self.order)

    def test_expire_due_orders_cancels_and_releases_stock(self):
        Order.objects.filter(pk=self.order.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        expired = expire_due_orders()

        self.assertEqual(expired, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.payment_status, PaymentStatus.EXPIRED)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)

    def test_expire_skips_already_paid_orders(self):
        mark_order_paid(self.order)
        Order.objects.filter(pk=self.order.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        expired = expire_due_orders()
        self.assertEqual(expired, 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)

    def test_valid_status_transition(self):
        mark_order_paid(self.order)
        transition_status(self.order, OrderStatus.PREPARING)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PREPARING)

    def test_invalid_status_transition_is_rejected(self):
        # از «در انتظار پرداخت» نمی‌توان مستقیم به «ارسال شده» رفت
        with self.assertRaises(OrderError):
            transition_status(self.order, OrderStatus.SHIPPED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)

    def test_status_transition_map_is_complete(self):
        # همه وضعیت‌ها باید در نقشه گذار حضور داشته باشند
        for status, _label in OrderStatus.choices:
            self.assertIn(status, ALLOWED_ORDER_TRANSITIONS)

    def test_can_pay_flag(self):
        self.assertTrue(self.order.can_pay)
        mark_order_paid(self.order)
        self.order.refresh_from_db()
        self.assertFalse(self.order.can_pay)

    def test_order_list_scope(self):
        other = User.objects.create_user(phone_number="09123450002")
        Order.objects.create(
            customer=other,
            status=OrderStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.PENDING,
            subtotal=Decimal(0),
            discount_total=Decimal(0),
            shipping_fee=Decimal(0),
            total=Decimal(0),
            receiver_name="دیگری",
            receiver_phone="09123450002",
            province="تهران",
            city="تهران",
            address_line="آدرس دیگری",
            postal_code="1234567890",
            shipping_method="پست",
        )
        self.assertEqual(Order.objects.for_customer(self.customer).count(), 1)


class ShippingTests(OrderFixtureMixin, TestCase):
    def test_single_standard_option(self):
        options = get_shipping_options()
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].code, "standard")

    def test_fee_for_below_threshold(self):
        option = get_shipping_options()[0]
        fee = option.fee_for(Decimal(1000))
        self.assertEqual(fee, Decimal(settings.SHIPPING_FEE))

    def test_fee_is_zero_above_threshold(self):
        option = get_shipping_options()[0]
        fee = option.fee_for(Decimal(settings.FREE_SHIPPING_MIN_AMOUNT))
        self.assertEqual(fee, Decimal(0))


class OrderItemCalculationTests(TestCase):
    def test_line_total_uses_decimal(self):
        total = OrderItem.compute_line_total(Decimal("123456"), 3)
        self.assertIsInstance(total, Decimal)
        self.assertEqual(total, Decimal("370368"))


class OrderAdminTests(OrderFixtureMixin, TestCase):
    """پنل مدیریت سفارش: حفظ فیلدها هنگام تغییر وضعیت و آزادسازی موجودی در لغو."""

    def setUp(self):
        self.build_order_data()
        self.order = create_order(
            customer=self.customer, cart=self.make_cart(2), address=self.address
        )

    def _admin_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        request = RequestFactory().post("/admin/orders/order/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        staff = User.objects.create_user(phone_number="09120000000")
        staff.is_staff = True
        staff.is_superuser = True
        staff.save(update_fields=["is_staff", "is_superuser"])
        request.user = staff
        return request

    def test_save_model_preserves_other_fields_on_status_change(self):
        from django.contrib.admin.sites import AdminSite
        from orders.admin import OrderAdmin

        admin_instance = OrderAdmin(Order, AdminSite())
        request = self._admin_request()

        obj = Order.objects.get(pk=self.order.pk)
        obj.tracking_number = "TRK-TEST-123"
        obj.customer_note = "یادداشت ادمین"
        obj.status = OrderStatus.PAID  # گذار مجاز از «در انتظار پرداخت»
        admin_instance.save_model(request, obj, form=None, change=True)

        self.order.refresh_from_db()
        # هم وضعیت تغییر کرده و هم فیلدهای دیگر حفظ شده‌اند (باگ قبلی: دور ریخته می‌شدند)
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.tracking_number, "TRK-TEST-123")
        self.assertEqual(self.order.customer_note, "یادداشت ادمین")

    def test_save_model_cancel_restores_stock(self):
        from django.contrib.admin.sites import AdminSite
        from orders.admin import OrderAdmin

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)  # ۵ منهای ۲ رزرو

        admin_instance = OrderAdmin(Order, AdminSite())
        request = self._admin_request()
        obj = Order.objects.get(pk=self.order.pk)
        obj.status = OrderStatus.CANCELLED
        admin_instance.save_model(request, obj, form=None, change=True)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)  # موجودی آزاد شد
