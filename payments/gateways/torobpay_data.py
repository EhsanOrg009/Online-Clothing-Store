"""CPG 1.3.31 data validation. See docs/torobpay-payment.md for ambiguities.

CPG does not define unit/line or discount arithmetic completely. Our explicit
contract is: item.amount = original unit price, count = quantity, cart total =
gross items + included shipping, payable = cart total - discountAmount. No tax
or external funding is invented. Historical catalog values are never queried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from django.conf import settings
from django.urls import reverse

from common.validators import normalize_phone_number, to_latin_digits

from .base import PaymentGatewayError


MIN_ELIGIBLE_RIAL = 200_000
MAX_ELIGIBLE_RIAL = 1_000_000_000
API_STATUSES = frozenset({"PENDING", "VERIFY", "SETTLE", "REVERT"})
CALLBACK_MAX_BYTES = 4096


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    title: str = ""
    description: str = ""


@dataclass(frozen=True)
class TokenResult:
    token: str = field(repr=False)
    payment_url: str = field(repr=False)
    redirect_valid: bool = True


@dataclass(frozen=True)
class RemoteStatus:
    # API status families are deliberately distinct from exact remote states.
    status: str
    amount_rial: int
    transaction_id: str


def toman_to_rial(value: Decimal | int | str) -> int:
    """Only adapter boundaries convert money; floats/bools are never accepted."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise PaymentGatewayError("مبلغ پرداخت معتبر نیست.")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise PaymentGatewayError("مبلغ پرداخت معتبر نیست.")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount.adjusted() > 14:
            raise ValueError
        with localcontext() as context:
            context.prec = 40
            rials = amount * Decimal(10)
        if rials != rials.to_integral_value():
            raise ValueError
        return int(rials)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise PaymentGatewayError("مبلغ پرداخت معتبر نیست.") from exc


def eligible_amount(value) -> int:
    amount = toman_to_rial(value)
    if not MIN_ELIGIBLE_RIAL <= amount <= MAX_ELIGIBLE_RIAL:
        raise PaymentGatewayError("مبلغ سفارش خارج از محدودهٔ خرید اعتباری ترب‌پی است.")
    return amount


def identifier(value, *, limit=64) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[A-Za-z0-9_.:-]{{1,{limit}}}", value):
        raise PaymentGatewayError("شناسهٔ گزارش ترب‌پی معتبر نیست.")
    return value


def payment_token(value) -> str:
    # This is a local defensive size limit, not a documented provider limit.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,16384}=*", value):
        raise PaymentGatewayError("شناسهٔ پرداخت ترب‌پی معتبر نیست.")
    if len(value) > 16384:
        raise PaymentGatewayError("شناسهٔ پرداخت ترب‌پی معتبر نیست.")
    return value


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(payment_token(token).encode("utf-8")).hexdigest()


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
        ord(char) < 32 and char not in "\n\t" for char in value
    ):
        raise PaymentGatewayError(f"{label} برای پرداخت با ترب‌پی کامل و معتبر نیست.")
    return value.strip()


def customer_fields(customer, address) -> dict:
    """Use owned persisted Address before checkout, Order snapshots afterwards."""
    owner_id = getattr(address, "user_id", getattr(address, "customer_id", None))
    if owner_id != customer.pk:
        raise PaymentGatewayError("نشانی پرداخت باید متعلق به مشتری سفارش باشد.")
    registered_phone = normalize_phone_number(customer.phone_number)
    receiver_phone = normalize_phone_number(address.receiver_phone)
    if not re.fullmatch(r"09[0-9]{9}", registered_phone):
        raise PaymentGatewayError("شمارهٔ ثبت‌نام مشتری برای پرداخت با ترب‌پی معتبر نیست.")
    if address.receiver_phone and not re.fullmatch(r"09[0-9]{9}", receiver_phone):
        raise PaymentGatewayError("شمارهٔ گیرنده برای پرداخت با ترب‌پی معتبر نیست.")
    postal_code = to_latin_digits(address.postal_code).strip()
    if not re.fullmatch(r"[0-9]{10}", postal_code):
        raise PaymentGatewayError("کد پستی برای پرداخت با ترب‌پی معتبر نیست.")
    full_name = _text(address.receiver_name, "نام گیرنده", 120)
    if len(full_name.split()) < 2:
        raise PaymentGatewayError("نام و نام خانوادگی گیرنده را برای پرداخت با ترب‌پی کامل کنید.")
    return {
        "address": _text(address.address_line, "نشانی", 4096),
        "postalCode": postal_code,
        "customer_full_name": full_name,
        "city": _text(address.city, "شهر", 64),
        "province": _text(address.province, "استان", 64),
        # CPG gives no alternate phone format; retain the store's 09XXXXXXXXX.
        "registration_phone_number": registered_phone,
        **({"mobile": receiver_phone} if receiver_phone else {}),
    }


def customer_fingerprint(customer, address) -> str:
    return hashlib.sha256(json.dumps(customer_fields(customer, address), sort_keys=True).encode()).hexdigest()


def order_items(order):
    frozen = getattr(order, "_torobpay_items", None)
    return list(frozen) if frozen is not None else list(order.items.order_by("pk"))


def cart_payload(order) -> dict:
    items = order_items(order)
    if not items:
        raise PaymentGatewayError("اقلام سفارش برای پرداخت با ترب‌پی یافت نشد.")
    cart_items, gross, discount, net = [], Decimal(0), Decimal(0), Decimal(0)
    for item in items:
        if type(item.quantity) is not int or item.quantity <= 0 or not item.pk:
            raise PaymentGatewayError("تعداد اقلام سفارش معتبر نیست.")
        original = toman_to_rial(item.original_unit_price)
        unit = toman_to_rial(item.unit_price)
        line_discount = toman_to_rial(item.discount_amount)
        line = toman_to_rial(item.line_total)
        if original < unit or line != unit * item.quantity or line_discount != (original - unit) * item.quantity:
            raise PaymentGatewayError("جمع مبالغ اقلام سفارش نیازمند بررسی است.")
        gross += item.original_unit_price * item.quantity
        discount += item.discount_amount
        net += item.line_total
        cart_items.append({
            "id": str(item.pk),
            "name": _text(item.product_name, "نام کالا", 200) + " / " + item.variant_label,
            "count": item.quantity,
            "amount": original,
            "category": _text(item.category_name, "دسته‌بندی ثبت‌شدهٔ کالا", 120),
        })
    shipping = toman_to_rial(order.shipping_fee)
    total = toman_to_rial(order.total)
    if (
        gross != order.subtotal or discount != order.discount_total
        or gross - discount != net or total != toman_to_rial(net) + shipping
    ):
        raise PaymentGatewayError("جمع مبالغ سفارش برای ترب‌پی سازگار نیست.")
    return {
        "cartId": identifier(order.order_number),
        "totalAmount": toman_to_rial(gross) + shipping,
        "shippingAmount": shipping,
        "isShipmentIncluded": True,
        "cartItems": cart_items,
    }


def callback_url(request_id) -> str:
    try:
        correlation = UUID(str(request_id))
        origin = urlsplit(settings.SITE_URL)
        if (
            origin.scheme not in ("http", "https") or not origin.hostname
            or origin.username or origin.password or origin.path or origin.query or origin.fragment
            or (not settings.DEBUG and origin.scheme != "https")
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise PaymentGatewayError("نشانی بازگشت پرداخت ترب‌پی معتبر نیست.") from exc
    return settings.SITE_URL.rstrip("/") + reverse("payments:torobpay_callback", args=[correlation])


def validate_payment_url(value) -> str:
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(
            ord(char) <= 32 or char == "\\" for char in value
        ):
            raise ValueError
        url = urlsplit(value)
        forbidden = {"accesstoken", "authorization", "jwt", "clientsecret", "password", "merchantpassword"}
        if any(key.lower().replace("_", "").replace("-", "") in forbidden for key, _ in parse_qsl(url.query)):
            raise ValueError
        hosts = settings.TOROBPAY_ALLOWED_PAYMENT_HOSTS
        if (
            url.scheme != "https" or url.hostname not in hosts or url.port not in (None, 443)
            or url.username or url.password or url.fragment
        ):
            raise ValueError
        return value
    except (ValueError, TypeError) as exc:
        raise PaymentGatewayError("نشانی صفحهٔ پرداخت ترب‌پی معتبر نیست.") from exc


def create_payload(order, *, request_id) -> dict:
    return {
        "amount": eligible_amount(order.total),
        "paymentMethodTypeDto": "ONLINE_CREDIT",
        "returnURL": callback_url(request_id),
        "transactionId": str(UUID(str(request_id))),
        "cartList": [cart_payload(order)],
        "discountAmount": toman_to_rial(order.discount_total),
        **customer_fields(order.customer, order),
    }


def parse_callback(data) -> dict[str, str]:
    if set(data) != {"transactionId", "state", "amount"}:
        raise PaymentGatewayError("اطلاعات بازگشت از ترب‌پی معتبر نیست.")
    result = {}
    for key in ("transactionId", "state", "amount"):
        values = data.getlist(key) if hasattr(data, "getlist") else [data[key]]
        if len(values) != 1 or not isinstance(values[0], str):
            raise PaymentGatewayError("اطلاعات بازگشت از ترب‌پی معتبر نیست.")
        result[key] = values[0]
    try:
        if str(UUID(result["transactionId"])) != result["transactionId"]:
            raise ValueError
    except ValueError as exc:
        raise PaymentGatewayError("شناسهٔ بازگشت از ترب‌پی معتبر نیست.") from exc
    if result["state"] not in ("OK", "FAILED") or not re.fullmatch(r"[1-9][0-9]{0,14}", result["amount"]):
        raise PaymentGatewayError("وضعیت یا مبلغ بازگشت از ترب‌پی معتبر نیست.")
    return result
