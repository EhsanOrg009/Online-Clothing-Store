"""تست‌های سبد خرید (سرویس‌ها و View ها)."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from accounts.models import User
from cart.models import Cart, CartItem
from cart.services import (
    CartError,
    add_item,
    cart_totals,
    get_cart,
    remove_item,
    update_item,
)
from products.models import Category, Color, Discount, Product, ProductVariant, Size


def make_request(user=None):
    """ساخت Request معتبر برای فراخوانی مستقیم سرویس‌ها در تست."""
    request = RequestFactory().get("/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request.user = user if user is not None else AnonymousUser()
    return request


class CartFixtureMixin:
    def build_cart_data(self):
        self.category = Category.objects.create(name="تی‌شرت")
        self.product = Product.objects.create(
            name="تی‌شرت نمونه",
            category=self.category,
            base_price=Decimal("500000"),
            is_published=True,
            is_active=True,
        )
        self.color = Color.objects.create(name="مشکی")
        self.size = Size.objects.create(name="M")
        self.variant = ProductVariant.objects.create(
            product=self.product, color=self.color, size=self.size, stock=5
        )


class CartServiceTests(CartFixtureMixin, TestCase):
    def setUp(self):
        self.build_cart_data()
        self.request = make_request()

    def test_add_item_creates_cart(self):
        item = add_item(self.request, self.variant, 2)

        self.assertEqual(item.quantity, 2)
        cart = get_cart(self.request, create=False)
        self.assertIsNotNone(cart)
        self.assertIsNone(cart.user)  # سبد مهمان با گره Session

    def test_add_same_variant_accumulates_quantity(self):
        add_item(self.request, self.variant, 2)
        add_item(self.request, self.variant, 1)

        cart = get_cart(self.request, create=False)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.first().quantity, 3)

    def test_quantity_is_capped_by_stock(self):
        with self.assertRaises(CartError):
            add_item(self.request, self.variant, 99)

    def test_quantity_is_capped_by_setting(self):
        ProductVariant.objects.filter(pk=self.variant.pk).update(stock=999)
        variant = ProductVariant.objects.get(pk=self.variant.pk)
        with self.assertRaises(CartError):
            add_item(self.request, variant, settings.MAX_QUANTITY_PER_ITEM + 1)

    def test_zero_or_negative_quantity_rejected(self):
        with self.assertRaises(CartError):
            add_item(self.request, self.variant, 0)
        with self.assertRaises(CartError):
            add_item(self.request, self.variant, -3)

    def test_out_of_stock_variant_rejected(self):
        ProductVariant.objects.filter(pk=self.variant.pk).update(stock=0)
        variant = ProductVariant.objects.get(pk=self.variant.pk)
        with self.assertRaises(CartError):
            add_item(self.request, variant, 1)

    def test_unpublished_product_rejected(self):
        Product.objects.filter(pk=self.product.pk).update(is_published=False)
        variant = ProductVariant.objects.select_related("product").get(
            pk=self.variant.pk
        )
        with self.assertRaises(CartError):
            add_item(self.request, variant, 1)

    def test_inactive_variant_rejected(self):
        ProductVariant.objects.filter(pk=self.variant.pk).update(is_active=False)
        variant = ProductVariant.objects.get(pk=self.variant.pk)
        with self.assertRaises(CartError):
            add_item(self.request, variant, 1)

    def test_update_item(self):
        item = add_item(self.request, self.variant, 1)
        updated = update_item(self.request, item.pk, 4)
        self.assertEqual(updated.quantity, 4)

    def test_update_item_rejects_foreign_items(self):
        item = add_item(self.request, self.variant, 1)
        other_request = make_request()
        with self.assertRaises(CartError):
            update_item(other_request, item.pk, 2)

    def test_update_item_to_zero_deletes_it(self):
        item = add_item(self.request, self.variant, 1)
        update_item(self.request, item.pk, 0)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_remove_item(self):
        item = add_item(self.request, self.variant, 1)
        remove_item(self.request, item.pk)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_totals_are_calculated_in_backend(self):
        add_item(self.request, self.variant, 2)

        totals = cart_totals(get_cart(self.request, create=False))
        self.assertEqual(totals.items_count, 2)
        self.assertEqual(totals.subtotal, Decimal("1000000"))
        self.assertEqual(totals.total, Decimal("1000000") + totals.shipping_fee)
        self.assertIsInstance(totals.subtotal, Decimal)

    def test_totals_include_discount(self):
        Discount.objects.create(
            name="تخفیف",
            product=self.product,
            discount_type=Discount.DiscountType.PERCENT,
            value=10,
            is_active=True,
        )
        add_item(self.request, self.variant, 1)
        totals = cart_totals(get_cart(self.request, create=False))

        # subtotal قبل از تخفیف است؛ تخفیف جداگانه محاسبه می‌شود
        self.assertEqual(totals.subtotal, Decimal("500000"))
        self.assertEqual(totals.discount_total, Decimal("50000"))
        self.assertEqual(totals.total, Decimal("450000") + totals.shipping_fee)

    def test_empty_cart_totals(self):
        totals = cart_totals(None)
        self.assertEqual(totals.items_count, 0)
        self.assertEqual(totals.total, Decimal("0"))


class CartViewTests(CartFixtureMixin, TestCase):
    def setUp(self):
        self.build_cart_data()
        self.client = Client()

    def test_empty_cart_page(self):
        response = self.client.get(reverse("cart:detail"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "سبد خرید شما خالی است")

    def test_add_via_post_redirects_back(self):
        response = self.client.post(
            reverse("cart:add_variant"),
            {"variant_id": self.variant.pk, "quantity": 1, "next": "/store/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/store/")

        response = self.client.get(reverse("cart:detail"))
        self.assertContains(response, "تی‌شرت نمونه")

    def test_add_without_variant_shows_friendly_error(self):
        response = self.client.post(
            reverse("cart:add_variant"), {"quantity": 1}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CartItem.objects.count(), 0)
        self.assertContains(response, "رنگ و سایز")

    def test_add_with_non_numeric_variant_is_rejected(self):
        response = self.client.post(
            reverse("cart:add_variant"), {"variant_id": "abc"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_add_requires_post(self):
        response = self.client.get(reverse("cart:add", args=[self.variant.pk]))
        self.assertEqual(response.status_code, 405)

    def test_update_via_post(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        item = CartItem.objects.get()
        response = self.client.post(
            reverse("cart:update", args=[item.pk]), {"quantity": 3}
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

    def test_update_over_stock_shows_error(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        item = CartItem.objects.get()
        response = self.client.post(
            reverse("cart:update", args=[item.pk]), {"quantity": 99}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)

    def test_remove_via_post(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        item = CartItem.objects.get()
        response = self.client.post(reverse("cart:remove", args=[item.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_hx_request_returns_partial(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        item = CartItem.objects.get()
        response = self.client.post(
            reverse("cart:update", args=[item.pk]),
            {"quantity": 2},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("خلاصه سفارش", body)
        # Partial نباید شامل تگ‌های کل صفحه باشد
        self.assertNotIn("<!DOCTYPE html>", body)

    def test_hx_remove_returns_partial_empty_state(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        item = CartItem.objects.get()
        response = self.client.post(
            reverse("cart:remove", args=[item.pk]), {}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("سبد خرید شما خالی است", response.content.decode())

    def test_guest_cart_is_isolated_from_other_guests(self):
        self.client.post(
            reverse("cart:add_variant"), {"variant_id": self.variant.pk, "quantity": 1}
        )
        other = Client()
        response = other.get(reverse("cart:detail"))
        self.assertContains(response, "سبد خرید شما خالی است")


class CartMergeTests(CartFixtureMixin, TestCase):
    """ادغام سبد مهمان با سبد کاربر پس از ورود."""

    def setUp(self):
        self.build_cart_data()

    def test_guest_cart_is_merged_on_login(self):
        from cart.services import merge_session_cart

        guest_request = make_request()
        add_item(guest_request, self.variant, 2)

        user = User.objects.create_user(phone_number="09125556677")
        merge_session_cart(guest_request, user)

        cart = Cart.objects.get(user=user)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.first().quantity, 2)
        self.assertEqual(Cart.objects.filter(user=None).count(), 0)
