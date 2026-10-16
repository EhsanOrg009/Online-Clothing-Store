"""
قالب‌تگ‌های عمومی نمایش پول، تاریخ شمسی و درصد تخفیف.
"""

from decimal import Decimal, InvalidOperation

import jdatetime
from django import forms
from django import template
from django.utils import timezone

register = template.Library()

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian(value) -> str:
    """تبدیل ارقام لاتین به فارسی برای یکدستی با متن‌های فارسی رابط کاربری."""
    return str(value).translate(_FA_DIGITS)


def _to_int(value):
    """تبدیل امن مقدار پولی (Decimal/int/str) به عدد صحیح، بدون عبور از float."""
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


@register.filter(name="is_checkbox")
def is_checkbox(widget) -> bool:
    """آیا ویجت به‌صورت چک‌باکس رندر می‌شود؟"""
    return isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple))


@register.filter(name="is_multiple")
def is_multiple(widget) -> bool:
    """آیا ویجت چندانتخابی است (CheckboxSelectMultiple / SelectMultiple)?"""
    return isinstance(widget, (forms.CheckboxSelectMultiple, forms.SelectMultiple))


@register.filter(name="currency")
def currency(value) -> str:
    """نمایش مبلغ با جداکننده هزارگان و ارقام فارسی: ۱,۵۰۰,۰۰۰ تومان"""
    amount = _to_int(value)
    if amount is None:
        return "۰ تومان"
    return to_persian(f"{amount:,} تومان")


@register.filter(name="price_only")
def price_only(value) -> str:
    """فقط مبلغ بدون واحد (ارقام فارسی)."""
    amount = _to_int(value)
    if amount is None:
        return "۰"
    return to_persian(f"{amount:,}")


@register.filter(name="percent")
def percent(value) -> str:
    """نمایش درصد با ارقام و علامت درصد فارسی."""
    number = _to_int(value)
    if number is None:
        return "۰٪"
    return to_persian(f"{number}٪")


@register.simple_tag
def jalali_date(value, fmt: str = "%Y/%m/%d") -> str:
    """تبدیل تاریخ میلادی به شمسی."""
    if not value:
        return "-"
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return to_persian(jdatetime.datetime.fromgregorian(datetime=value).strftime(fmt))


@register.simple_tag
def jalali_datetime(value) -> str:
    """تاریخ و ساعت شمسی."""
    return jalali_date(value, fmt="%Y/%m/%d - %H:%M")


@register.filter(name="fa_num")
def fa_num(value) -> str:
    """تبدیل ارقام یک مقدار به فارسی (شماره صفحه، شمارنده‌ها و اعداد ثابت قالب)."""
    return to_persian(value)
