"""Regression tests for the redesign's real workflows and security boundaries."""
import json
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.forms import AddressForm, OTPForm
from accounts.models import Address, User
from accounts.sms.registry import get_sms_provider, reset_provider_cache
from cart.models import Cart, CartItem
from common.dashboard import build_dashboard_context
from common.ratelimit import client_ip
from common.views import server_error
from orders.admin import OrderAdminForm
from orders.choices import OrderStatus, PaymentStatus
from orders.services import OrderError, create_order, expire_due_orders, mark_order_paid
from products.models import Category, Color, Product, ProductVariant, Size


class RedesignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(phone_number="09121110001", first_name="خریدار", last_name="آزمایش")
        cls.other = User.objects.create_user(phone_number="09121110002", first_name="دیگر", last_name="آزمایش")
        cls.superuser = User.objects.create_superuser(phone_number="09121110003", password="test-only-password")
        cls.staff = User.objects.create_user(phone_number="09121110004", is_staff=True)
        cls.category = Category.objects.create(name="پیراهن آزمایش")
        cls.other_category = Category.objects.create(name="دسته دیگر")
        cls.product = Product.objects.create(name="پیراهن آزمایش", category=cls.category, base_price=500000, is_published=True)
        cls.red = Color.objects.create(name="قرمز")
        cls.blue = Color.objects.create(name="آبی")
        cls.small = Size.objects.create(name="S")
        cls.large = Size.objects.create(name="L")
        cls.variant = ProductVariant.objects.create(product=cls.product, color=cls.red, size=cls.small, stock=10)
        cls.unavailable = ProductVariant.objects.create(product=cls.product, color=cls.blue, size=cls.large, stock=0)
        cls.address = Address.objects.create(user=cls.customer, receiver_name="خریدار آزمایش", receiver_phone=cls.customer.phone_number, province="تهران", city="تهران", address_line="نشانی کامل و آزمایشی گیرنده", postal_code="1234567890", is_default=True)

    def login(self, user=None):
        self.client.force_login(user or self.customer)

    def cart(self, customer=None):
        cart = Cart.objects.create(user=customer or self.customer)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)
        return cart

    def order(self):
        return create_order(customer=self.customer, cart=self.cart(), address=self.address)

    def test_all_public_templates_render(self):
        for route in ["/", "/store/", "/categories/", "/search/", "/search/?q=absent", "/accounts/login/", "/cart/", reverse("store:category", args=[self.category.slug]), reverse("store:product_detail", args=[self.product.slug])]:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'lang="fa" dir="rtl"')

    def test_product_variant_json_is_array_not_double_encoded(self):
        response = self.client.get(reverse("store:product_detail", args=[self.product.slug]))
        data = re.search(r'<script id="variants-data"[^>]*>(.*?)</script>', response.content.decode(), re.S).group(1)
        variants = json.loads(data)
        self.assertIsInstance(variants, list)
        self.assertEqual(next(v for v in variants if v["id"] == self.variant.pk)["color"], self.red.name)
        self.assertContains(response, 'name="variant_id"')
        self.assertContains(response, 'رنگ و سایز')

    def test_variant_data_escapes_script_markup(self):
        self.red.name = '</script><script>alert(1)</script>'
        self.red.save()
        response = self.client.get(reverse("store:product_detail", args=[self.product.slug]))
        self.assertNotContains(response, self.red.name)
        self.assertContains(response, r'\u003C/script\u003E')

    def test_color_and_size_must_match_same_variant(self):
        response = self.client.get("/store/", {"colors": [self.red.pk], "sizes": [self.large.pk]})
        self.assertEqual(response.context["result_count"], 0)

    def test_in_stock_filter_applies_to_selected_combination(self):
        response = self.client.get("/store/", {"colors": [self.blue.pk], "sizes": [self.large.pk], "in_stock_only": "on"})
        self.assertEqual(response.context["result_count"], 0)

    def test_persian_category_query_is_valid(self):
        response = self.client.get("/store/", {"category": self.category.slug})
        self.assertFalse(response.context["filter_form"].errors)
        self.assertEqual(response.context["result_count"], 1)

    def test_category_route_cannot_be_overridden_by_query(self):
        response = self.client.get(reverse("store:category", args=[self.category.slug]), {"category": self.other_category.slug})
        self.assertEqual(response.context["category"].pk, self.category.pk)
        self.assertEqual(response.context["result_count"], 1)

    def test_sort_is_submittable_without_javascript(self):
        self.assertContains(self.client.get("/store/"), '<button type="submit" class="btn-outline btn-sm">اعمال</button>', html=True)

    def test_filter_errors_are_associated_with_inputs(self):
        response = self.client.get("/store/", {"min_price": "invalid"})
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'id_min_price_error')

    def test_address_province_has_real_choices(self):
        form = AddressForm()
        self.assertIn(("تهران", "تهران"), list(form.fields["province"].choices))

    def test_empty_auth_post_shows_required_error(self):
        response = self.client.post("/accounts/login/", {})
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'aria-describedby="id_phone_number_error"')

    def test_account_pages_render(self):
        self.login()
        for route in ["/accounts/profile/", "/accounts/profile/edit/", "/accounts/complete-profile/", "/accounts/addresses/", "/accounts/addresses/new/", f"/accounts/addresses/{self.address.pk}/edit/", "/orders/"]:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_other_users_address_is_not_editable(self):
        self.login(self.other)
        self.assertEqual(self.client.get(f"/accounts/addresses/{self.address.pk}/edit/").status_code, 404)
        self.assertEqual(self.client.post(f"/accounts/addresses/{self.address.pk}/delete/").status_code, 404)
        self.assertTrue(Address.objects.filter(pk=self.address.pk).exists())

    def test_checkout_invalid_address_id_does_not_crash(self):
        self.login(); self.cart()
        response = self.client.post("/checkout/", {"action": "select", "address_id": "invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "آدرس انتخاب‌شده معتبر نیست")

    def test_checkout_foreign_address_is_rejected(self):
        self.login(self.other); self.cart(self.other)
        response = self.client.post("/checkout/", {"action": "select", "address_id": self.address.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "آدرس انتخاب‌شده معتبر نیست")

    def test_checkout_address_errors_open_form(self):
        self.login(); self.cart()
        response = self.client.post("/checkout/", {"action": "add_address"})
        self.assertContains(response, 'aria-invalid="true"')
        self.assertRegex(response.content.decode(), r'<details[^>]+open')

    @override_settings(DEBUG=True, PAYMENT_GATEWAY="mock")
    def test_empty_optional_note_can_submit_checkout(self):
        self.login(); self.cart()
        response = self.client.post("/checkout/review/", {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/payments/redirect/", response.url)
        self.assertEqual(self.customer.orders.count(), 1)

    def test_create_order_rejects_foreign_cart(self):
        cart = self.cart()
        with self.assertRaises(OrderError):
            create_order(customer=self.other, cart=cart, address=self.address)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 10)
        self.assertEqual(cart.items.count(), 1)

    def test_create_order_rejects_foreign_address(self):
        cart = self.cart(self.other)
        with self.assertRaises(OrderError):
            create_order(customer=self.other, cart=cart, address=self.address)
        self.assertEqual(cart.items.count(), 1)

    def test_failed_payment_reservation_expires_and_restores_once(self):
        order = self.order(); order.status = OrderStatus.PAYMENT_FAILED
        order.expires_at = timezone.now() - timedelta(minutes=1)
        order.save(update_fields=["status", "expires_at"])
        self.assertEqual(expire_due_orders(), 1)
        self.assertEqual(expire_due_orders(), 0)
        self.variant.refresh_from_db(); order.refresh_from_db()
        self.assertEqual(self.variant.stock, 10)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.EXPIRED)

    def test_staff_without_order_permission_cannot_read_order(self):
        order = self.order(); self.login(self.staff)
        self.assertEqual(self.client.get(reverse("orders:detail", args=[order.order_number])).status_code, 403)

    def test_pending_order_does_not_display_failure_or_cancellation(self):
        order = self.order(); self.login()
        for name in ["result_failed", "result_cancelled", "result_success"]:
            self.assertRedirects(self.client.get(reverse(f"payments:{name}", args=[order.order_number])), reverse("orders:detail", args=[order.order_number]))

    @override_settings(DEBUG=True, PAYMENT_GATEWAY="mock")
    def test_mock_result_replay_and_result_templates(self):
        from payments.services import create_payment_for_order, process_callback
        from payments.gateways.mock import MockGateway
        order = self.order(); self.login()
        payment = create_payment_for_order(order)
        gateway = MockGateway()
        result = process_callback(payment, {"status": "failed", "sig": gateway.sign(f"result|{payment.authority}|failed")})
        self.assertEqual(result.status, PaymentStatus.FAILED)
        response = self.client.get(reverse("payments:result_failed", args=[order.order_number]))
        self.assertContains(response, "پرداخت ناموفق بود")
        self.assertNotContains(response, "مبلغی از حساب شما کسر نشده")
        second = create_payment_for_order(order)
        process_callback(second, {"status": "successful", "sig": gateway.sign(f"result|{second.authority}|successful")})
        process_callback(second, {"status": "successful", "sig": gateway.sign(f"result|{second.authority}|successful")})
        self.variant.refresh_from_db(); self.assertEqual(self.variant.stock, 8)
        self.assertContains(self.client.get(reverse("payments:result_success", args=[order.order_number])), "پرداخت با موفقیت انجام شد")

    def test_dashboard_omits_all_data_without_model_permissions(self):
        self.assertEqual(build_dashboard_context(self.staff), {"dashboard": {}})
        self.login(self.staff)
        response = self.client.get("/admin/")
        self.assertNotContains(response, "مجموع فروش پرداخت‌شده")
        self.assertNotContains(response, 'href="/admin/orders/order/"')

    def test_dashboard_only_includes_permitted_model(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="view_product", content_type__app_label="products"))
        dashboard = build_dashboard_context(self.staff)["dashboard"]
        self.assertEqual(dashboard["published_products"], 1)
        self.assertNotIn("total_users", dashboard)
        self.assertNotIn("recent_orders", dashboard)

    def test_best_sellers_count_units_not_order_lines(self):
        order = self.order(); mark_order_paid(order)
        dashboard = build_dashboard_context(self.superuser)["dashboard"]
        self.assertEqual(dashboard["best_sellers"][0].sold, 2)

    def test_admin_lists_and_available_add_forms_render(self):
        self.login(self.superuser)
        for model, model_admin in admin.site._registry.items():
            label = f"admin:{model._meta.app_label}_{model._meta.model_name}"
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(reverse(f"{label}_changelist")).status_code, 200)
                request = RequestFactory().get("/");request.user = self.superuser
                if model_admin.has_add_permission(request):
                    self.assertEqual(self.client.get(reverse(f"{label}_add")).status_code, 200)

    def test_order_admin_cannot_fabricate_payment(self):
        order = self.order()
        form = OrderAdminForm(instance=order, data={"status":OrderStatus.PAID})
        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)
        self.assertIn("پرداخت فقط از طریق تأیید درگاه", str(form.errors["status"]))

    def test_order_admin_payment_status_is_readonly(self):
        self.login(self.superuser); order = self.order()
        response = self.client.get(reverse("admin:orders_order_change", args=[order.pk]))
        self.assertNotIn("payment_status", response.context["adminform"].form.fields)

    def test_user_admin_hashes_new_password(self):
        from accounts.admin import ShopUserCreationForm
        form = ShopUserCreationForm({"phone_number":"09121110099", "usable_password":"true", "password1":"temporary-test-password-123", "password2":"temporary-test-password-123"})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(user.check_password("temporary-test-password-123"))
        self.assertNotEqual(user.password, "temporary-test-password-123")

    def test_staff_cannot_edit_superuser(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="change_user", content_type__app_label="accounts"))
        self.login(self.staff)
        self.assertEqual(self.client.post(reverse("admin:accounts_user_change", args=[self.superuser.pk]), {"is_active": False}).status_code, 403)
        self.superuser.refresh_from_db(); self.assertTrue(self.superuser.is_active)

    def test_server_error_page_never_queries_database(self):
        with self.assertNumQueries(0):
            response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("فروشگاه موقتاً در دسترس نیست", response.content.decode())

    def test_csrf_failure_has_storefront_error_page(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/cart/add/", {"variant_id": self.variant.pk})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "اعتبار فرم پایان یافته است", status_code=403)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_untrusted_forwarded_ip_cannot_bypass_limiter(self):
        request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.9", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(client_ip(request), "198.51.100.9")

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.1"])
    def test_forwarded_chain_selects_nearest_untrusted_peer(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="spoofed, 198.51.100.9")
        self.assertEqual(client_ip(request), "198.51.100.9")

    @override_settings(SMS_PROVIDER="memory", OTP_RESEND_SECONDS=0)
    def test_guest_cart_survives_actual_otp_login_session_rotation(self):
        cache.clear();reset_provider_cache();self.addCleanup(cache.clear);self.addCleanup(reset_provider_cache)
        self.client.post("/cart/add/", {"variant_id": self.variant.pk, "quantity":2})
        old_key = self.client.session.session_key
        self.client.post("/accounts/login/", {"phone_number": self.customer.phone_number})
        code = re.search(r"کد ورود شما (\d+)", get_sms_provider().outbox[self.customer.phone_number][-1]).group(1)
        self.client.post("/accounts/verify/", {"code": code})
        self.assertNotEqual(self.client.session.session_key, old_key)
        self.assertFalse(Cart.objects.filter(user=None, session_key=old_key).exists())
        self.assertEqual(CartItem.objects.get(cart__user=self.customer).quantity, 2)

    @override_settings(OTP_LENGTH=6)
    def test_otp_form_uses_configured_length(self):
        self.assertTrue(OTPForm({"code":"۱۲۳۴۵۶"}).is_valid())
        self.assertFalse(OTPForm({"code":"12345"}).is_valid())

    @override_settings(OTP_LENGTH=10)
    def test_otp_validator_supports_lengths_above_eight(self):
        self.assertTrue(OTPForm({"code":"1234567890"}).is_valid())

    def test_admin_login_renders_for_anonymous_visitors(self):
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_cart_admin_counts_and_owners_do_not_query_per_row(self):
        self.cart()
        self.cart(self.other)
        request = RequestFactory().get("/"); request.user = self.superuser
        model_admin = admin.site.get_model_admin(Cart)
        with self.assertNumQueries(2):
            carts = list(model_admin.get_queryset(request))
            self.assertEqual([model_admin.items_count_display(cart) for cart in carts], [2, 2])
            for cart in carts:
                model_admin.owner(cart)

    def test_category_admin_product_counts_use_one_query(self):
        request = RequestFactory().get("/"); request.user = self.superuser
        model_admin = admin.site.get_model_admin(Category)
        with self.assertNumQueries(1):
            counts = {category.pk: model_admin.products_count(category) for category in model_admin.get_queryset(request)}
            self.assertEqual(counts[self.category.pk], 1)
            self.assertEqual(counts[self.other_category.pk], 0)

    def test_admin_bulk_publication_changes_catalogue_visibility(self):
        self.login(self.superuser)
        for action, published in [("unpublish_products", False), ("publish_products", True)]:
            response = self.client.post("/admin/products/product/", {"action":action, "_selected_action":[self.product.pk], "index":"0"})
            self.assertEqual(response.status_code, 302)
            self.product.refresh_from_db()
            self.assertEqual(self.product.is_published, published)
            self.assertEqual(self.client.get(reverse("store:product_detail",args=[self.product.slug])).status_code, 200 if published else 404)

    def test_admin_out_of_stock_product_renders(self):
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])
        self.login(self.superuser)
        self.assertContains(self.client.get("/admin/products/product/"), "ناموجود")

    def test_valid_image_between_two_and_three_megabytes_is_accepted(self):
        from io import BytesIO
        import random
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from common.validators import MAX_IMAGE_SIZE, validate_image_file
        image = Image.frombytes("RGB", (900, 900), random.Random(42).randbytes(900 * 900 * 3))
        output = BytesIO()
        image.save(output, format="PNG")
        upload = SimpleUploadedFile("valid.png", output.getvalue(), content_type="image/png")
        self.assertGreater(upload.size, 2 * 1024 * 1024)
        self.assertLess(upload.size, MAX_IMAGE_SIZE)
        validate_image_file(upload)
        self.assertEqual(upload.tell(), 0)

    def test_image_decompression_bomb_is_a_validation_error(self):
        from PIL import Image
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile
        from common.validators import validate_image_file
        with patch("PIL.Image.open", side_effect=Image.DecompressionBombError("too large")):
            with self.assertRaises(ValidationError):
                validate_image_file(SimpleUploadedFile("large.png", b"test", content_type="image/png"))

    def test_invalid_cart_quantity_is_not_silently_replaced(self):
        self.login();cart=self.cart();item=cart.items.get()
        response=self.client.post(reverse("cart:update", args=[item.pk]), {"quantity":"invalid"}, HTTP_HX_REQUEST="true")
        item.refresh_from_db();self.assertEqual(item.quantity, 2)
        self.assertContains(response, "تعداد باید یک عدد صحیح باشد")
        self.assertContains(response, "data-cart-feedback")

    @override_settings(FREE_SHIPPING_MIN_AMOUNT=0, SHIPPING_FEE=50000)
    def test_disabled_free_shipping_is_not_advertised(self):
        self.login();self.cart()
        response=self.client.get("/cart/")
        self.assertNotContains(response, "هزینه ارسال این سبد رایگان است")
        self.assertNotContains(response, "ارسال رایگان از")
