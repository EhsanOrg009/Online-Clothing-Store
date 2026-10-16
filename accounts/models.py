"""
Model های حساب کاربران.

نکته مهم: از ابتدای پروژه از Custom User استفاده شده است،
زیرا افزودن USERNAME سفارشی به User پیش‌فرض پس از اولین Migration ممکن نیست.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel
from common.validators import (
    normalize_phone_number,
    to_latin_digits,
    validate_iranian_mobile,
    validate_postal_code,
)
from accounts.managers import UserManager


class User(AbstractUser):
    """کاربر فروشگاه با احراز هویت شماره موبایل (بدون Password برای مشتریان)."""

    username = None
    email = None

    phone_number = models.CharField(
        _("شماره موبایل"),
        max_length=11,
        unique=True,
        db_index=True,
        validators=[validate_iranian_mobile],
        help_text=_("نمونه: 09123456789"),
    )
    first_name = models.CharField(_("نام"), max_length=100, blank=True)
    last_name = models.CharField(_("نام خانوادگی"), max_length=150, blank=True)
    is_phone_verified = models.BooleanField(_("شماره تأیید شده"), default=False)

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta(AbstractUser.Meta):
        verbose_name = "کاربر"
        verbose_name_plural = "کاربران"
        ordering = ("-date_joined",)

    def __str__(self) -> str:
        return self.full_name or self.phone_number

    def save(self, *args, **kwargs):
        self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_profile_complete(self) -> bool:
        """آیا اطلاعات پایه پروفایل تکمیل شده است؟"""
        return bool(self.first_name and self.last_name)


class Address(TimeStampedModel):
    """آدرس کاربر (هر کاربر می‌تواند چند آدرس داشته باشد)."""

    user = models.ForeignKey(
        User,
        verbose_name=_("کاربر"),
        on_delete=models.CASCADE,
        related_name="addresses",
    )
    receiver_name = models.CharField(_("نام گیرنده"), max_length=120)
    receiver_phone = models.CharField(
        _("تلفن گیرنده"),
        max_length=11,
        validators=[validate_iranian_mobile],
    )
    province = models.CharField(_("استان"), max_length=64)
    city = models.CharField(_("شهر"), max_length=64)
    address_line = models.TextField(_("آدرس"))
    postal_code = models.CharField(
        _("کد پستی"),
        max_length=10,
        validators=[validate_postal_code],
    )
    is_default = models.BooleanField(_("آدرس پیش‌فرض"), default=False)

    class Meta:
        verbose_name = "آدرس"
        verbose_name_plural = "آدرس‌ها"
        ordering = ("-is_default", "-updated_at")
        indexes = [
            models.Index(fields=["user", "is_default"]),
        ]

    def __str__(self) -> str:
        return f"{self.receiver_name} - {self.city}"

    def save(self, *args, **kwargs):
        self.receiver_phone = normalize_phone_number(self.receiver_phone)
        self.postal_code = to_latin_digits(self.postal_code).strip()
        super().save(*args, **kwargs)
        if self.is_default:
            (
                Address.objects.filter(user=self.user)
                .exclude(pk=self.pk)
                .update(is_default=False)
            )

    @property
    def single_line(self) -> str:
        """آدرس به‌صورت یک خط برای نمایش."""
        return f"{self.province}، {self.city}، {self.address_line} (کد پستی: {self.postal_code})"


class OTPChallenge(models.Model):
    """
    چالش احراز هویت با کد یک‌بارمصرف (OTP).

    نکته امنیتی: کد خام هرگز ذخیره نمی‌شود؛ فقط Hash آن نگهداری می‌شود.
    """

    phone_number = models.CharField(
        _("شماره موبایل"), max_length=11, db_index=True
    )
    code_hash = models.CharField(_("Hash کد"), max_length=128)
    created_at = models.DateTimeField(_("تاریخ ایجاد"), auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(_("تاریخ انقضا"), db_index=True)
    attempts = models.PositiveSmallIntegerField(_("تعداد تلاش"), default=0)
    is_used = models.BooleanField(_("مصرف شده"), default=False)
    used_at = models.DateTimeField(_("زمان مصرف"), null=True, blank=True)
    ip_address = models.GenericIPAddressField(
        _("IP درخواست‌دهنده"), null=True, blank=True
    )

    class Meta:
        verbose_name = "کد یک‌بارمصرف"
        verbose_name_plural = "کدهای یک‌بارمصرف"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["phone_number", "is_used", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"OTP {self.phone_number} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone

        return timezone.now() >= self.expires_at

    def can_attempt(self, max_attempts: int) -> bool:
        return not self.is_used and not self.is_expired and self.attempts < max_attempts
