"""
والیدیتورهای امنیتی و اعتبارسنجی داده‌های ورودی.

تمام ورودی‌های کاربر باید پیش از ذخیره‌سازی از اینجا عبور کنند.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# کاراکترهای فارسی و عربی ارقام → لاتین
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

MOBILE_PATTERN = re.compile(r"^09\d{9}$")
POSTAL_CODE_PATTERN = re.compile(r"^\d{10}$")

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
MAX_IMAGE_SIZE = 3 * 1024 * 1024  # 3 مگابایت


def to_latin_digits(value) -> str:
    """تبدیل ارقام فارسی/عربی به لاتین (ورودی None یا خالی → رشته خالی)."""
    if value is None:
        return ""
    return str(value).translate(_PERSIAN_DIGITS)


def normalize_phone_number(value: str) -> str:
    """
    نرمال‌سازی شماره تلفن به فرمت استاندارد ایران: 09XXXXXXXXXX

    پشتیبانی از فرمت‌های رایج:
      ۰۹۱۲... / 0912... / +98912... / 0098912... / 912... / 98912...
    """
    if not value:
        return ""
    phone = to_latin_digits(str(value)).strip()
    phone = re.sub(r"[\s\-\(\)\.]", "", phone)

    if phone.startswith("+98"):
        phone = "0" + phone[3:]
    elif phone.startswith("0098"):
        phone = "0" + phone[4:]
    elif phone.startswith("98") and len(phone) == 12:
        phone = "0" + phone[2:]
    elif phone.startswith("9") and len(phone) == 10:
        phone = "0" + phone
    return phone


def validate_iranian_mobile(value: str) -> None:
    """اعتبارسنجی شماره موبایل ایران پس از نرمال‌سازی."""
    phone = normalize_phone_number(value)
    if not MOBILE_PATTERN.match(phone):
        raise ValidationError(
            _("شماره موبایل معتبر نیست. نمونه صحیح: 09123456789"),
            code="invalid_phone",
        )


def validate_postal_code(value: str) -> None:
    """اعتبارسنجی کد پستی ۱۰ رقمی."""
    code = to_latin_digits(str(value)).strip()
    if not POSTAL_CODE_PATTERN.match(code):
        raise ValidationError(
            _("کد پستی باید ۱۰ رقم باشد."), code="invalid_postal_code"
        )


def validate_image_file(file) -> None:
    """
    بررسی امنیت فایل تصویری پیش از آپلود:
    نوع محتوا، پسوند، حجم و سلامت فایل (از طریق Pillow).
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image, UnidentifiedImageError

    if file is None:
        return

    extension = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            _("فرمت فایل مجاز نیست. فرمت‌های مجاز: jpg، png، webp، gif"),
            code="invalid_image_extension",
        )

    content_type = getattr(file, "content_type", "")
    if content_type and content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise ValidationError(_("نوع فایل ارسالی مجاز نیست."), code="invalid_image_type")

    if file.size and file.size > MAX_IMAGE_SIZE:
        raise ValidationError(
            _("حجم فایل نباید بیشتر از ۳ مگابایت باشد."), code="image_too_large"
        )

    # بررسی سلامت واقعی محتوای تصویر (جلوگیری از Upload فایل اجرایی)
    try:
        data = file.read(MAX_IMAGE_SIZE + 1)
        probe = SimpleUploadedFile(file.name, data, content_type=content_type)
        with Image.open(probe) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(
            _("فایل تصویر معتبر نیست."), code="corrupted_image"
        ) from exc
    finally:
        try:
            file.seek(0)
        except (ValueError, OSError):  # pragma: no cover - فایل‌های غیرقابل Seek
            pass
