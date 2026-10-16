"""فرم‌های مربوط به Checkout و سفارش."""

from django import forms
from django.utils.translation import gettext_lazy as _


class CheckoutNoteForm(forms.Form):
    """یادداشت مشتری برای سفارش (اختیاری)."""

    note = forms.CharField(
        label=_("یادداشت برای فروشگاه"),
        required=False,
        max_length=500,
        widget=forms.Textarea(
            attrs={"rows": 3, "class": "field-input", "placeholder": _("اختیاری")}
        ),
    )

    def clean_note(self):
        return (self.cleaned_data.get("note") or "").strip()


class ShippingMethodForm(forms.Form):
    """انتخاب روش ارسال (در حال حاضر یک روش؛ آماده برای افزودن روش‌های جدید)."""

    shipping_method = forms.CharField(widget=forms.HiddenInput, required=False)
