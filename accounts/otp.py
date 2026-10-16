"""
سرویس احراز هویت با کد یک‌بارمصرف (OTP).

ویژگی‌های امنیتی:
  • تولید کد با `secrets` (تصادفی امن)
  • نگهداری فقط Hash کد (با HMAC-SHA256) و نه کد خام
  • انقضای زمانی
  • محدودیت تعداد تلاش
  • محدودیت نرخ ارسال مجدد (بر اساس شماره و IP)
  • مصرف یک‌باره کد با قفل ردیف دیتابیس (جلوگیری از Race Condition)
  • هیچ کد OTP در لاگ امنیتی ثبت نمی‌شود
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from common import ratelimit
from common.validators import normalize_phone_number, to_latin_digits, validate_iranian_mobile

from .models import OTPChallenge, User
from .sms import SMSProviderError, get_sms_provider

logger = logging.getLogger("shop.security")


class OTPError(Exception):
    """خطای قابل نمایش به کاربر در فرآیند OTP."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


def _sign(phone_number: str, code: str) -> str:
    """Hash کد با کلید محرمانه پروژه (بدون ذخیره کد خام)."""
    secret = settings.SECRET_KEY.encode()
    return hmac.new(secret, f"{phone_number}:{code}".encode(), hashlib.sha256).hexdigest()


def _generate_code(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _message_template(code: str) -> str:
    minutes = max(1, settings.OTP_TTL_SECONDS // 60)
    return f"{settings.STORE_NAME}: کد ورود شما {code} است. این کد تا {minutes} دقیقه معتبر است."


def _normalize(raw_phone: str) -> str:
    phone = normalize_phone_number(raw_phone)
    try:
        validate_iranian_mobile(phone)
    except ValidationError as exc:
        raise OTPError(_("شماره موبایل معتبر نیست. نمونه صحیح: 09123456789")) from exc
    return phone


def _check_resend_rules(phone: str, ip_address: str | None) -> None:
    if ip_address and not ratelimit.allow_request(
        "otp_request_ip", ip_address, limit=30, window_seconds=3600
    ):
        raise OTPError(_("تعداد درخواست از این دستگاه بیش از حد مجاز است. کمی بعد تلاش کنید."))

    now = timezone.now()
    last = (
        OTPChallenge.objects.filter(phone_number=phone)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if last:
        elapsed = (now - last).total_seconds()
        if elapsed < settings.OTP_RESEND_SECONDS:
            wait = int(settings.OTP_RESEND_SECONDS - elapsed)
            raise OTPError(
                _("کد جدید تا %(seconds)s ثانیه دیگر قابل درخواست است.")
                % {"seconds": wait}
            )

    hour_ago = now - timedelta(hours=1)
    recent_count = OTPChallenge.objects.filter(
        phone_number=phone, created_at__gte=hour_ago
    ).count()
    if recent_count >= settings.OTP_RESEND_LIMIT_PER_HOUR:
        raise OTPError(
            _("تعداد درخواست کد در ساعت گذشته بیش از حد مجاز بوده است. بعداً تلاش کنید.")
        )


def request_otp(raw_phone: str, ip_address: str | None = None) -> tuple[str, OTPChallenge]:
    """
    ایجاد چالش جدید و ارسال کد از طریق Provider فعال پیامک.

    خطاها: OTPError
    """
    phone = _normalize(raw_phone)
    _check_resend_rules(phone, ip_address)

    # کدهای قبلیِ مصرف‌نشده باطل می‌شوند تا فقط آخرین کد قابل استفاده باشد.
    timezone_now = timezone.now()
    OTPChallenge.objects.filter(phone_number=phone, is_used=False).update(
        is_used=True, used_at=timezone_now
    )

    code = _generate_code(max(4, settings.OTP_LENGTH))
    challenge = OTPChallenge.objects.create(
        phone_number=phone,
        code_hash=_sign(phone, code),
        expires_at=timezone_now + timedelta(seconds=settings.OTP_TTL_SECONDS),
        ip_address=ip_address,
    )

    message = _message_template(code)
    try:
        get_sms_provider().send(phone, message)
    except SMSProviderError as exc:
        challenge.delete()
        logger.warning("ارسال پیامک برای شماره %s ناموفق بود: %s", phone, exc)
        raise OTPError(_("ارسال پیامک ممکن نشد. لطفاً کمی بعد دوباره تلاش کنید.")) from exc
    except Exception as exc:  # خطای غیرمنتظره Provider
        challenge.delete()
        logger.exception("خطای غیرمنتظره Provider پیامک")
        raise OTPError(_("ارسال پیامک ممکن نشد. لطفاً کمی بعد دوباره تلاش کنید.")) from exc

    logger.info("OTP challenge ایجاد شد برای شماره %s (کد ذخیره/لاگ نشد)", phone)
    return phone, challenge


def verify_otp(
    raw_phone: str, raw_code: str, ip_address: str | None = None
) -> tuple[User, bool]:
    """
    بررسی کد واردشده و فعال‌سازی حساب کاربری.

    خروجی: (کاربر، آیا کاربر جدید بود؟)
    خطاها: OTPError
    """
    phone = _normalize(raw_phone)
    code = to_latin_digits(raw_code or "").strip()

    if not code:
        raise OTPError(_("کد تأیید را وارد کنید."))

    if ip_address and not ratelimit.allow_request(
        "otp_verify_ip", ip_address, limit=60, window_seconds=900
    ):
        raise OTPError(_("تعداد تلاش بیش از حد مجاز است. کمی بعد تلاش کنید."))

    # نکته حیاتی: هیچ Exception ای داخل بلوک atomic پرتاب نمی‌شود،
    # وگرنه Rollback انجام و شمارنده تلاش‌ها (محافظت Brute-Force) ذخیره نمی‌شود.
    failure: OTPError | None = None
    user: User | None = None
    created = False

    with transaction.atomic():
        challenge = (
            OTPChallenge.objects.select_for_update()
            .filter(phone_number=phone, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if challenge is None:
            failure = OTPError(_("کد تأییدی یافت نشد. لطفاً کد جدید درخواست کنید."))

        elif challenge.is_expired:
            challenge.is_used = True
            challenge.used_at = timezone.now()
            challenge.save(update_fields=["is_used", "used_at"])
            failure = OTPError(_("کد تأیید منقضی شده است. لطفاً کد جدید درخواست کنید."))

        elif challenge.attempts >= settings.OTP_MAX_ATTEMPTS:
            challenge.is_used = True
            challenge.used_at = timezone.now()
            challenge.save(update_fields=["is_used", "used_at"])
            failure = OTPError(
                _("تعداد تلاش مجاز به پایان رسید. لطفاً کد جدید درخواست کنید.")
            )

        elif not hmac.compare_digest(_sign(phone, code), challenge.code_hash):
            challenge.attempts += 1
            fields = ["attempts"]
            if challenge.attempts >= settings.OTP_MAX_ATTEMPTS:
                challenge.is_used = True
                challenge.used_at = timezone.now()
                fields += ["is_used", "used_at"]
            challenge.save(update_fields=fields)
            failure = OTPError(_("کد واردشده اشتباه است."))

        else:
            # کد معتبر: مصرف یک‌باره
            challenge.is_used = True
            challenge.used_at = timezone.now()
            challenge.attempts += 1
            challenge.save(update_fields=["is_used", "used_at", "attempts"])

            user, created = User.objects.get_or_create(phone_number=phone)
            if not user.is_active:
                failure = OTPError(_("حساب کاربری شما غیرفعال شده است."))
            elif not user.is_phone_verified:
                user.is_phone_verified = True
                user.save(update_fields=["is_phone_verified"])

    if failure is not None:
        raise failure

    logger.info("احراز هویت موفق برای شماره %s (کاربر %s)", phone, "جدید" if created else "فعلی")
    return user, created
