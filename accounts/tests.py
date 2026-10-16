"""تست‌های احراز هویت (OTP)، پروفایل و آدرس."""

import re

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Address, OTPChallenge, User
from accounts.otp import OTPError, request_otp, verify_otp
from accounts.sms.registry import reset_provider_cache


class OTPTestCase(TestCase):
    """پایه مشترک تست‌های OTP با Provider درون‌حافظه‌ای (بدون پیامک واقعی)."""

    PHONE = "09121234567"

    def setUp(self):
        override = override_settings(SMS_PROVIDER="memory", OTP_RESEND_SECONDS=0)
        override.enable()
        self.addCleanup(override.disable)
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)
        cache.clear()
        self.addCleanup(cache.clear)

    @staticmethod
    def _provider():
        from accounts.sms.registry import get_sms_provider

        return get_sms_provider()

    def _last_code(self, phone: str) -> str:
        message = self._provider().outbox[phone][-1]
        match = re.search(r"کد ورود شما (\d+)", message)
        assert match, f"کد در پیامک پیدا نشد: {message}"
        return match.group(1)


class RequestOtpTests(OTPTestCase):
    def test_challenge_is_created_with_hashed_code(self):
        phone, challenge = request_otp(self.PHONE, ip_address="198.51.100.4")

        self.assertEqual(phone, self.PHONE)
        self.assertNotEqual(challenge.code_hash, "")
        # کد خام هرگز در دیتابیس ذخیره نمی‌شود
        code = self._last_code(phone)
        self.assertNotIn(code, challenge.code_hash)
        self.assertNotIn(code, repr(OTPChallenge.objects.values_list("code_hash", flat=True)))
        self.assertFalse(challenge.is_used)

    def test_message_contains_valid_code(self):
        phone, _ = request_otp(self.PHONE, ip_address="198.51.100.4")
        code = self._last_code(phone)
        self.assertEqual(len(code), 5)
        self.assertTrue(code.isdigit())

    def test_invalid_phone_is_rejected(self):
        with self.assertRaises(OTPError):
            request_otp("12345", ip_address="198.51.100.4")

    def test_resend_cooldown_is_enforced(self):
        override = override_settings(OTP_RESEND_SECONDS=120)
        override.enable()
        self.addCleanup(override.disable)
        request_otp(self.PHONE, ip_address="198.51.100.4")
        with self.assertRaises(OTPError):
            request_otp(self.PHONE, ip_address="198.51.100.4")

    def test_hourly_limit_is_enforced(self):
        override = override_settings(OTP_RESEND_LIMIT_PER_HOUR=2)
        override.enable()
        self.addCleanup(override.disable)
        request_otp(self.PHONE, ip_address="198.51.100.4")
        request_otp(self.PHONE, ip_address="198.51.100.4")
        with self.assertRaises(OTPError):
            request_otp(self.PHONE, ip_address="198.51.100.4")

    def test_ip_rate_limit_is_enforced(self):
        for _ in range(30):
            request_otp("09120000%03d" % (_ + 1), ip_address="203.0.113.9")
        with self.assertRaises(OTPError):
            request_otp("09129999999", ip_address="203.0.113.9")

    def test_new_code_invalidates_previous_one(self):
        _, first = request_otp(self.PHONE, ip_address="198.51.100.4")
        first_code = self._last_code(self.PHONE)

        _, second = request_otp(self.PHONE, ip_address="198.51.100.4")
        first.refresh_from_db()
        self.assertTrue(first.is_used)
        self.assertFalse(second.is_used)

        # کد قبلی دیگر قابل استفاده نیست
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, first_code, "198.51.100.5")


class VerifyOtpTests(OTPTestCase):
    def setUp(self):
        super().setUp()
        _, self.challenge = request_otp(self.PHONE, ip_address="198.51.100.4")
        self.code = self._last_code(self.PHONE)

    def test_correct_code_creates_user(self):
        user, created = verify_otp(self.PHONE, self.code, "198.51.100.4")

        self.assertTrue(created)
        self.assertEqual(user.phone_number, self.PHONE)
        self.assertTrue(user.is_phone_verified)
        self.challenge.refresh_from_db()
        self.assertTrue(self.challenge.is_used)

    def test_code_is_single_use(self):
        verify_otp(self.PHONE, self.code, "198.51.100.4")
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, self.code, "198.51.100.4")

    def test_wrong_code_increments_attempts(self):
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, "00000", "198.51.100.4")

        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.attempts, 1)
        self.assertFalse(self.challenge.is_used)

    @override_settings(OTP_MAX_ATTEMPTS=2)
    def test_challenge_is_locked_after_max_attempts(self):
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, "00000", "198.51.100.4")
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, "00001", "198.51.100.4")

        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.attempts, 2)
        self.assertTrue(self.challenge.is_used)

        # استفاده مجدد از همان چالش مسدودشده ممکن نیست
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, self.code, "198.51.100.4")

    def test_max_attempts_reached_before_any_guess(self):
        """اگر شمارنده به حد رسیده باشد، حتی کد درست هم پذیرفته نمی‌شود."""
        OTPChallenge.objects.filter(pk=self.challenge.pk).update(
            attempts=settings.OTP_MAX_ATTEMPTS
        )
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, self.code, "198.51.100.4")

    def test_expired_code_is_rejected(self):
        from datetime import timedelta

        from django.utils import timezone

        OTPChallenge.objects.filter(pk=self.challenge.pk).update(
            expires_at=timezone.now() - timedelta(seconds=5)
        )
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, self.code, "198.51.100.4")

    def test_persian_digits_are_accepted(self):
        persian = "".join("۰۱۲۳۴۵۶۷۸۹"[int(d)] for d in self.code)
        user, created = verify_otp(self.PHONE, persian, "198.51.100.4")
        self.assertTrue(created)
        self.assertEqual(user.phone_number, self.PHONE)

    def test_empty_code_is_rejected(self):
        with self.assertRaises(OTPError):
            verify_otp(self.PHONE, "", "198.51.100.4")

    def test_unknown_phone_is_rejected(self):
        with self.assertRaises(OTPError):
            verify_otp("09120001111", self.code, "198.51.100.4")


class LoginViewTests(OTPTestCase):
    def test_login_page_renders(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "شماره موبایل")

    def test_login_posts_to_verify_page(self):
        response = self.client.post(
            reverse("accounts:login"), {"phone_number": self.PHONE}, follow=False
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:verify"), response.headers["Location"])

    def test_open_redirect_is_ignored(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"phone_number": self.PHONE, "next": "https://evil.example.com/"},
            follow=False,
        )
        self.assertNotIn("evil.example.com", response.headers["Location"])

    def test_verify_page_renders(self):
        self.client.post(reverse("accounts:login"), {"phone_number": self.PHONE})
        response = self.client.get(reverse("accounts:verify"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.PHONE)

    def test_full_login_flow_logs_user_in(self):
        self.client.post(reverse("accounts:login"), {"phone_number": self.PHONE})
        code = self._last_code(self.PHONE)
        response = self.client.post(
            reverse("accounts:verify"), {"code": code}, follow=False
        )
        self.assertEqual(response.status_code, 302)

        profile = self.client.get(reverse("accounts:profile"))
        self.assertEqual(profile.status_code, 200)

        user = User.objects.get(phone_number=self.PHONE)
        self.assertTrue(user.is_phone_verified)


class ProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone_number="09123334455",
            first_name="سارا",
            last_name="محمدی",
            is_phone_verified=True,
        )
        self.client.force_login(self.user)

    def test_profile_page_shows_user_data(self):
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "سارا محمدی")
        self.assertContains(response, "09123334455")

    def test_profile_edit_updates_names(self):
        response = self.client.post(
            reverse("accounts:profile_edit"),
            {"first_name": "نیلوفر", "last_name": "احمدی"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "نیلوفر")

    def test_profile_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.headers["Location"])


class AddressTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone_number="09127778899", first_name="رضا", last_name="کریمی"
        )
        self.client.force_login(self.user)

    def _payload(self, **extra):
        data = {
            "receiver_name": "رضا کریمی",
            "receiver_phone": "09127778899",
            "province": "تهران",
            "city": "تهران",
            "address_line": "خیابان آزادی، پلاک ۱۰",
            "postal_code": "1234567890",
            "is_default": True,
        }
        data.update(extra)
        return data

    def test_create_address(self):
        response = self.client.post(
            reverse("accounts:address_create"), self._payload(), follow=False
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Address.objects.filter(user=self.user).count(), 1)

    def test_invalid_postal_code_is_rejected(self):
        response = self.client.post(
            reverse("accounts:address_create"),
            self._payload(postal_code="12"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Address.objects.filter(user=self.user).count(), 0)
        self.assertContains(response, "کد پستی")

    def test_invalid_phone_is_rejected(self):
        response = self.client.post(
            reverse("accounts:address_create"),
            self._payload(receiver_phone="0211234"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Address.objects.filter(user=self.user).count(), 0)

    def test_only_own_addresses_are_listed(self):
        other = User.objects.create_user(phone_number="09120001111")
        Address.objects.create(
            user=other,
            receiver_name="دیگری",
            receiver_phone="09120001111",
            province="اصفهان",
            city="اصفهان",
            address_line="خیابان چهارباغ",
            postal_code="1234567890",
        )
        Address.objects.create(
            user=self.user,
            receiver_name="رضا کریمی",
            receiver_phone="09127778899",
            province="تهران",
            city="تهران",
            address_line="خیابان آزادی",
            postal_code="1234567890",
        )
        response = self.client.get(reverse("accounts:address_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "رضا کریمی")
        self.assertNotContains(response, "دیگری")
