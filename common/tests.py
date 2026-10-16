"""تست‌های ماژول common (اعتبارسنجی، فیلترهای قالب و Rate Limit)."""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from common.ratelimit import allow_request, reset, retry_after
from common.templatetags.shop_extras import currency, is_checkbox, percent, price_only
from common.validators import (
    normalize_phone_number,
    to_latin_digits,
    validate_iranian_mobile,
    validate_postal_code,
)


class DigitNormalizationTests(SimpleTestCase):
    def test_persian_digits_are_converted_to_latin(self):
        self.assertEqual(to_latin_digits("۰۱۲۳۴۵۶۷۸۹"), "0123456789")

    def test_arabic_digits_are_converted_to_latin(self):
        self.assertEqual(to_latin_digits("٠١٢٣٤٥٦٧٨٩"), "0123456789")

    def test_latin_digits_are_kept(self):
        self.assertEqual(to_latin_digits("09123456789"), "09123456789")

    def test_none_returns_empty_string(self):
        self.assertEqual(to_latin_digits(None), "")


class PhoneNormalizationTests(SimpleTestCase):
    def test_extra_characters_are_stripped(self):
        self.assertEqual(normalize_phone_number("+98 912 345 6789"), "09123456789")

    def test_zero_prefix_is_normalized(self):
        self.assertEqual(normalize_phone_number("9123456789"), "09123456789")

    def test_invalid_phone_raises(self):
        with self.assertRaises(ValidationError):
            validate_iranian_mobile("0912345")

    def test_valid_phone_passes(self):
        validate_iranian_mobile("09123456789")  # نباید خطا بدهد

    def test_landline_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_iranian_mobile("02188776655")


class PostalCodeTests(SimpleTestCase):
    def test_valid_postal_code(self):
        validate_postal_code("1234567890")

    def test_short_postal_code_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_postal_code("12345")


class TemplateTagTests(SimpleTestCase):
    def test_currency_format(self):
        self.assertEqual(currency(1_500_000), "۱,۵۰۰,۰۰۰ تومان")

    def test_currency_handles_none(self):
        self.assertEqual(currency(None), "۰ تومان")

    def test_price_only_has_no_unit(self):
        self.assertEqual(price_only(2500), "۲,۵۰۰")

    def test_percent_symbol(self):
        self.assertEqual(percent(25), "۲۵٪")

    @override_settings(USE_THOUSAND_SEPARATOR=False)
    def test_is_checkbox_detects_checkbox_input(self):
        from django import forms

        self.assertTrue(is_checkbox(forms.CheckboxInput()))
        self.assertFalse(is_checkbox(forms.TextInput()))


class RateLimitTests(TestCase):
    """Rate limiter مبتنی بر Cache."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

    def test_allows_requests_up_to_limit(self):
        reset("test_scope", "ip")
        for _ in range(3):
            self.assertTrue(allow_request("test_scope", "ip", limit=3, window_seconds=60))

    def test_blocks_request_over_limit(self):
        reset("test_scope", "ip")
        for _ in range(3):
            allow_request("test_scope", "ip", limit=3, window_seconds=60)
        self.assertFalse(allow_request("test_scope", "ip", limit=3, window_seconds=60))

    def test_retry_after_is_reported(self):
        reset("test_scope", "ip")
        allow_request("test_scope", "ip", limit=1, window_seconds=120)
        self.assertGreater(retry_after("test_scope", "ip"), 0)

    def test_zero_limit_blocks_everything(self):
        self.assertFalse(allow_request("test_scope", "ip", limit=0, window_seconds=60))

    @override_settings(TRUSTED_PROXY_IPS=["127.0.0.1", "10.0.0.1"])
    def test_client_ip_prefers_x_forwarded_for(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        from common.ratelimit import client_ip

        self.assertEqual(client_ip(request), "203.0.113.7")


class DashboardContextTests(TestCase):
    """آمار داشبورد باید فقط برای صفحه‌ی اصلی پنل ادمین محاسبه شود.

    این تست یک رگرسیون واقعی را پوشش می‌دهد: بررسی با `url_name`
    (که برابر "index" است) باعث می‌شد context processor هیچ‌گاه فعال نشود
    و داشبورد آماری همیشه خالی نمایش داده شود.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.superuser = get_user_model().objects.create_superuser(
            phone_number="09129999999", password="secret-pass-123"
        )
        self.client.force_login(self.superuser)

    def test_dashboard_shows_stats_on_admin_index(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("dashboard", response.context)
        self.assertContains(response, "داشبورد فروشگاه")

    def test_dashboard_is_absent_on_other_admin_pages(self):
        response = self.client.get("/admin/products/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("dashboard", response.context)

    def test_dashboard_is_hidden_from_non_staff_users(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            phone_number="09128888888", password="secret-pass-123"
        )
        self.client.force_login(user)
        response = self.client.get("/admin/")
        # کاربر عادی اصلاً به پنل دسترسی ندارد
        self.assertEqual(response.status_code, 302)
