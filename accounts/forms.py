"""فرم‌های فروشگاه (Validation سمت سرور برای تمام ورودی‌های کاربر)."""

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from common.choices import PROVINCES
from common.validators import (
    normalize_phone_number,
    to_latin_digits,
    validate_iranian_mobile,
    validate_postal_code,
)

from .models import Address, User

# کلاس‌های CSS مشترک ورودی‌ها (استایل در فایل CSS پروژه تعریف شده است)
WIDGET_ATTRS = {"class": "field-input"}


class PhoneForm(forms.Form):
    """دریافت شماره موبایل."""

    phone_number = forms.CharField(
        label=_("شماره موبایل"),
        max_length=20,
        widget=forms.TextInput(
            attrs={
                **WIDGET_ATTRS,
                "inputmode": "numeric",
                "autocomplete": "tel",
                "placeholder": "09123456789",
                "dir": "ltr",
            }
        ),
    )

    def clean_phone_number(self) -> str:
        value = normalize_phone_number(self.cleaned_data["phone_number"])
        validate_iranian_mobile(value)
        return value


class OTPForm(forms.Form):
    """ورود کد یک‌بارمصرف."""

    code = forms.CharField(
        label=_("کد تأیید"),
        max_length=8,
        min_length=4,
        widget=forms.TextInput(
            attrs={
                **WIDGET_ATTRS,
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "placeholder": "کد پیامک‌شده",
                "class": "field-input text-center tracking-[0.5em]",
                "dir": "ltr",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        length = max(4, settings.OTP_LENGTH)
        field = self.fields["code"]
        self.fields["code"] = forms.CharField(
            label=field.label, min_length=length, max_length=length,
            widget=field.widget,
        )
        self.fields["code"].widget.attrs["maxlength"] = length

    def clean_code(self) -> str:
        code = to_latin_digits(self.cleaned_data["code"]).strip()
        if len(code) != max(4, settings.OTP_LENGTH) or not code.isascii() or not code.isdigit():
            raise ValidationError(_("کد تأیید فقط باید شامل عدد باشد."))
        return code


class ProfileCompleteForm(forms.Form):
    """تکمیل اطلاعات کاربر جدید پس از تأیید شماره موبایل."""

    first_name = forms.CharField(
        label=_("نام"),
        max_length=100,
        widget=forms.TextInput(attrs={**WIDGET_ATTRS, "autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label=_("نام خانوادگی"),
        max_length=150,
        widget=forms.TextInput(attrs={**WIDGET_ATTRS, "autocomplete": "family-name"}),
    )
    province = forms.ChoiceField(
        label=_("استان"),
        choices=[("", _("استان را انتخاب کنید"))] + PROVINCES,
        widget=forms.Select(attrs=WIDGET_ATTRS),
    )
    city = forms.CharField(
        label=_("شهر"),
        max_length=64,
        widget=forms.TextInput(attrs=WIDGET_ATTRS),
    )
    address_line = forms.CharField(
        label=_("آدرس"),
        widget=forms.Textarea(
            attrs={**WIDGET_ATTRS, "rows": 3, "autocomplete": "street-address"}
        ),
    )
    postal_code = forms.CharField(
        label=_("کد پستی"),
        max_length=10,
        widget=forms.TextInput(
            attrs={**WIDGET_ATTRS, "inputmode": "numeric", "dir": "ltr"}
        ),
    )

    def clean_first_name(self):
        value = self.cleaned_data["first_name"].strip()
        if len(value) < 2:
            raise ValidationError(_("نام باید حداقل ۲ حرف باشد."))
        return value

    def clean_last_name(self):
        value = self.cleaned_data["last_name"].strip()
        if len(value) < 2:
            raise ValidationError(_("نام خانوادگی باید حداقل ۲ حرف باشد."))
        return value

    def clean_city(self):
        return self.cleaned_data["city"].strip()

    def clean_address_line(self):
        value = self.cleaned_data["address_line"].strip()
        if len(value) < 10:
            raise ValidationError(_("آدرس باید حداقل ۱۰ نویسه باشد."))
        return value

    def clean_postal_code(self):
        value = to_latin_digits(self.cleaned_data["postal_code"]).strip()
        validate_postal_code(value)
        return value


class AddressForm(forms.ModelForm):
    """افزودن / ویرایش آدرس."""

    province = forms.ChoiceField(
        label=_("استان"), choices=[("", _("استان را انتخاب کنید"))] + PROVINCES,
        widget=forms.Select(attrs=WIDGET_ATTRS),
    )

    class Meta:
        model = Address
        fields = [
            "receiver_name",
            "receiver_phone",
            "province",
            "city",
            "address_line",
            "postal_code",
            "is_default",
        ]
        widgets = {
            "receiver_name": forms.TextInput(attrs=WIDGET_ATTRS),
            "receiver_phone": forms.TextInput(
                attrs={**WIDGET_ATTRS, "inputmode": "numeric", "dir": "ltr"}
            ),
            "province": forms.Select(attrs=WIDGET_ATTRS),
            "city": forms.TextInput(attrs=WIDGET_ATTRS),
            "address_line": forms.Textarea(attrs={**WIDGET_ATTRS, "rows": 3}),
            "postal_code": forms.TextInput(
                attrs={**WIDGET_ATTRS, "inputmode": "numeric", "dir": "ltr"}
            ),
            "is_default": forms.CheckboxInput(attrs={"class": "field-checkbox"}),
        }

    def clean_receiver_name(self):
        value = self.cleaned_data["receiver_name"].strip()
        if len(value) < 3:
            raise ValidationError(_("نام گیرنده باید حداقل ۳ حرف باشد."))
        return value

    def clean_receiver_phone(self):
        value = normalize_phone_number(self.cleaned_data["receiver_phone"])
        validate_iranian_mobile(value)
        return value

    def clean_city(self):
        return self.cleaned_data["city"].strip()

    def clean_address_line(self):
        value = self.cleaned_data["address_line"].strip()
        if len(value) < 10:
            raise ValidationError(_("آدرس باید حداقل ۱۰ نویسه باشد."))
        return value

    def clean_postal_code(self):
        value = to_latin_digits(self.cleaned_data["postal_code"]).strip()
        validate_postal_code(value)
        return value


class ProfileEditForm(forms.ModelForm):
    """ویرایش اطلاعات پروفایل (شماره موبایل قابل تغییر نیست)."""

    class Meta:
        model = User
        fields = ["first_name", "last_name"]
        widgets = {
            "first_name": forms.TextInput(attrs=WIDGET_ATTRS),
            "last_name": forms.TextInput(attrs=WIDGET_ATTRS),
        }

    def clean_first_name(self):
        value = self.cleaned_data["first_name"].strip()
        if len(value) < 2:
            raise ValidationError(_("نام باید حداقل ۲ حرف باشد."))
        return value

    def clean_last_name(self):
        value = self.cleaned_data["last_name"].strip()
        if len(value) < 2:
            raise ValidationError(_("نام خانوادگی باید حداقل ۲ حرف باشد."))
        return value
