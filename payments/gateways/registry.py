"""انتخاب درگاه پرداخت فعال بر اساس تنظیمات."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import (
    PaymentGateway,
    PaymentGatewayError,
    PaymentRequest,
    VerificationResult,
)
from .zibal import ZibalGateway
from .torobpay import TorobPayGateway

_BUILTIN_GATEWAYS: dict[str, type[PaymentGateway]] = {
    ZibalGateway.name: ZibalGateway,
    TorobPayGateway.name: TorobPayGateway,
}


def get_gateway(name: str | None = None) -> PaymentGateway:
    """
    درگاه زیبال را با نام کوتاه یا مسیر کلاس موجود برمی‌گرداند.
    سوابق سایر درگاه‌ها قابل نمایش‌اند اما عملیات جدید برای آن‌ها انجام نمی‌شود.
    """
    key = name or settings.PAYMENT_GATEWAY
    if key in _BUILTIN_GATEWAYS:
        gateway = _BUILTIN_GATEWAYS[key]()
    else:
        # یک مسیر نادرست در `PAYMENT_GATEWAY` نباید به خطای 500 تبدیل شود
        try:
            gateway = import_string(key)()
        except (ImportError, AttributeError) as exc:
            raise PaymentGatewayError(
                f"درگاه پرداخت «{key}» پیدا نشد. مقدار PAYMENT_GATEWAY را بررسی کنید."
            ) from exc

    if (not isinstance(gateway, (ZibalGateway, TorobPayGateway)) or gateway.name not in _BUILTIN_GATEWAYS
            or not isinstance(gateway, _BUILTIN_GATEWAYS[gateway.name])):
        raise PaymentGatewayError("درگاه پرداخت با رابط پشتیبانی‌شده سازگار نیست.")
    return gateway


def enabled_gateway_keys() -> dict[str, str]:
    """Only trusted settings may contain import paths; POST accepts short names."""
    keys = getattr(settings, "PAYMENT_GATEWAYS", None) or [settings.PAYMENT_GATEWAY]
    result = {}
    for key in keys:
        try:
            cls = _BUILTIN_GATEWAYS.get(key) or import_string(key)
            if not isinstance(cls, type) or not issubclass(cls, (ZibalGateway, TorobPayGateway)):
                raise ValueError
            if cls.name not in _BUILTIN_GATEWAYS or cls.name in result or not issubclass(cls, _BUILTIN_GATEWAYS[cls.name]):
                raise ValueError
            result[cls.name] = key
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            raise PaymentGatewayError("فهرست PAYMENT_GATEWAYS معتبر نیست.") from exc
    default = _BUILTIN_GATEWAYS.get(settings.PAYMENT_GATEWAY)
    try:
        default = default or import_string(settings.PAYMENT_GATEWAY)
        if default.name not in result:
            raise ValueError
    except (ImportError, AttributeError, ValueError) as exc:
        raise PaymentGatewayError("درگاه پیش‌فرض باید در فهرست روش‌های فعال باشد.") from exc
    return result


def get_enabled_gateway(name=None):
    keys = enabled_gateway_keys()
    if name is None:
        return get_gateway()
    if name not in keys:
        raise PaymentGatewayError("روش پرداخت انتخاب‌شده فعال یا معتبر نیست.")
    return get_gateway(keys[name])


__all__ = [
    "PaymentGateway",
    "PaymentGatewayError",
    "PaymentRequest",
    "VerificationResult",
    "get_gateway",
]
