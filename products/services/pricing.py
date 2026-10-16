"""
محاسبه قیمت نهایی محصول (تنها منبع حقیقت قیمت).

قوانین:
  • قیمت نهایی هرگز در Database ذخیره نمی‌شود؛ همیشه از `base_price` و
    `Discount` معتبر محاسبه می‌گردد.
  • Frontend هیچ نقشی در محاسبه قیمت ندارد.
  • تخفیف بیش از قیمت محصول اعمال نمی‌شود (بسته به Clamp).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class PriceInfo:
    """نتیجه محاسبه قیمت یک محصول."""

    base_price: Decimal
    final_price: Decimal
    discount_amount: Decimal
    discount_percent: int
    has_discount: bool

    @property
    def saved_amount(self) -> Decimal:
        return self.discount_amount


def _get_discount(product):
    try:
        return product.discount
    except ObjectDoesNotExist:
        return None


def _discount_is_active(discount, at=None) -> bool:
    if discount is None or not discount.is_active:
        return False
    moment = at or timezone.now()
    if discount.starts_at and moment < discount.starts_at:
        return False
    if discount.ends_at and moment > discount.ends_at:
        return False
    return True


def calculate_price(product, at=None) -> PriceInfo:
    """
    محاسبه قیمت نهایی یک محصول.

    نمونه:
        base_price = 2,000,000 | تخفیف 25٪
        → final_price = 1,500,000 | discount_amount = 500,000 | 25٪
    """
    base = Decimal(product.base_price or 0)
    discount = _get_discount(product)

    if base <= 0 or not _discount_is_active(discount, at):
        return PriceInfo(
            base_price=base,
            final_price=base,
            discount_amount=ZERO,
            discount_percent=0,
            has_discount=False,
        )

    from products.models import Discount  # import محلی برای جلوگیری از Circular Import

    value = Decimal(discount.value or 0)
    if discount.discount_type == Discount.DiscountType.PERCENT:
        amount = (base * value / HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        percent = int(value)
    else:
        amount = value
        percent = int((amount / base * HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if base > 0 else 0

    # بستن مقادیر غیرمنطقی
    if amount < ZERO:
        amount = ZERO
    if amount > base:
        amount = base
        percent = 100

    final = base - amount
    if final < ZERO:
        final = ZERO

    has_discount = amount > ZERO
    return PriceInfo(
        base_price=base,
        final_price=final,
        discount_amount=amount,
        discount_percent=percent if has_discount else 0,
        has_discount=has_discount,
    )
