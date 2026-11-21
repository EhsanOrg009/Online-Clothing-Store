"""Server-side checkout offers; eligibility is request-local, never cached."""

from dataclasses import dataclass, field
import logging

from .gateways.base import PaymentGatewayError
from .gateways.registry import enabled_gateway_keys, get_gateway
from .services import PaymentError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentOption:
    name: str
    title: str
    description: str = ""
    authorization: object = field(default=None, repr=False)


def payment_options(*, customer, amount, address):
    result = []
    try:
        keys = enabled_gateway_keys()
    except PaymentGatewayError:
        logger.warning("Checkout gateway configuration invalid")
        return result
    for name, key in keys.items():
        try:
            gateway = get_gateway(key)
            offer = getattr(gateway, "checkout_offer", None)
            if offer is None:
                result.append(PaymentOption(name, gateway.checkout_title))
                continue
            authorization = offer(customer=customer, amount=amount, address=address)
            if authorization.eligibility.eligible is True:
                eligibility = authorization.eligibility
                result.append(PaymentOption(name, eligibility.title or gateway.checkout_title, eligibility.description, authorization))
        except PaymentGatewayError:
            logger.warning("Checkout payment offer unavailable provider=%s customer_id=%s", name, customer.pk)
    try:
        preferred = get_gateway().name
    except PaymentGatewayError:
        preferred = ""
    return sorted(result, key=lambda option: option.name != preferred)


def select_payment_option(options, data):
    values = data.getlist("payment_method") if hasattr(data, "getlist") else [data.get("payment_method")]
    if len(values) > 1:
        raise PaymentError("روش پرداخت انتخاب‌شده معتبر نیست.")
    selected = values[0] if values else None
    if not selected:
        # Legacy forms have no provider field; retain the configured default.
        try:
            selected = get_gateway().name
        except PaymentGatewayError as error:
            raise PaymentError("تنظیمات درگاه پرداخت در دسترس نیست؛ با پشتیبانی تماس بگیرید.") from error
    for option in options:
        if selected == option.name:
            return option
    raise PaymentError("روش پرداخت انتخاب‌شده برای این سفارش در دسترس نیست؛ روش دیگری انتخاب کنید.")
