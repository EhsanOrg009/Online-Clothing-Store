"""فرم‌های فروشگاه: فیلتر و مرتب‌سازی محصولات."""

from django import forms
from django.utils.translation import gettext_lazy as _


class ProductFilterForm(forms.Form):
    """فیلتر فروشگاه (همه ورودی‌ها در Backend بررسی می‌شوند)."""

    SORT_CHOICES = [
        ("", _("پیش‌فرض")),
        ("newest", _("جدیدترین")),
        ("price_asc", _("ارزان‌ترین")),
        ("price_desc", _("گران‌ترین")),
    ]

    q = forms.CharField(
        label=_("جستجو"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("جستجوی محصول..."),
                "class": "field-input",
                "autocomplete": "off",
            }
        ),
    )
    sort = forms.ChoiceField(
        label=_("مرتب‌سازی"),
        required=False,
        choices=SORT_CHOICES,
        widget=forms.Select(attrs={"class": "field-input"}),
    )
    min_price = forms.IntegerField(
        label=_("حداقل قیمت"), required=False, min_value=0,
        widget=forms.NumberInput(attrs={"class": "field-input", "placeholder": _("از")}),
    )
    max_price = forms.IntegerField(
        label=_("حداکثر قیمت"), required=False, min_value=0,
        widget=forms.NumberInput(attrs={"class": "field-input", "placeholder": _("تا")}),
    )
    category = forms.SlugField(
        label=_("دسته‌بندی"), required=False,
        allow_unicode=True,
        widget=forms.HiddenInput(),
    )
    colors = forms.TypedMultipleChoiceField(
        label=_("رنگ"),
        required=False,
        coerce=int,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "field-checkgroup"}),
    )
    sizes = forms.TypedMultipleChoiceField(
        label=_("سایز"),
        required=False,
        coerce=int,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "field-checkgroup"}),
    )
    discount_only = forms.BooleanField(
        label=_("فقط محصولات تخفیف‌دار"), required=False
    )
    in_stock_only = forms.BooleanField(
        label=_("فقط موجود"), required=False
    )

    def __init__(self, *args, color_choices=None, size_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["colors"].choices = color_choices or []
        self.fields["sizes"].choices = size_choices or []

    def clean(self):
        cleaned = super().clean()
        min_price = cleaned.get("min_price")
        max_price = cleaned.get("max_price")
        if (
            min_price is not None
            and max_price is not None
            and max_price < min_price
        ):
            raise forms.ValidationError(
                _("حداکثر قیمت نمی‌تواند کمتر از حداقل قیمت باشد.")
            )
        return cleaned
