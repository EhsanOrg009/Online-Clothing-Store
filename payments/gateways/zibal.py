"""درگاه میزبانی‌شده زیبال؛ قرارداد API: https://help.zibal.ir/ipg/"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from orders.choices import PaymentStatus

from .base import PaymentGateway, PaymentGatewayError, PaymentRequest, VerificationResult


_STATUSES = {-2, -1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 18, 21}
_ERROR_MESSAGES = {
    102: "پذیرنده زیبال یافت نشد.",
    103: "پذیرنده زیبال غیرفعال است.",
    104: "پذیرنده زیبال نامعتبر است.",
    105: "مبلغ پرداخت کمتر از حداقل مجاز زیبال است.",
    106: "آدرس بازگشت با دامنه ثبت‌شده در زیبال مطابقت ندارد.",
    112: "موجودی کیف پول کارمزد زیبال کافی نیست.",
    113: "مبلغ پرداخت از سقف مجاز زیبال بیشتر است.",
    115: "نشانی IP سرور در پنل زیبال مجاز نشده است.",
    140: "آدرس بازگشت پرداخت ارسال نشده است.",
    203: "شناسه تراکنش زیبال نامعتبر است.",
}


class _NoAPIRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # API زیبال Redirect مستندشده‌ای ندارد؛ merchant به مقصد دیگری ارسال نمی‌شود.
        return None


_http_open = build_opener(_NoAPIRedirect()).open


def _track_id(value: str | int) -> int:
    # trackId در JSON عدد int64 و در Query String رشته است.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PaymentGatewayError("شناسه تراکنش زیبال نامعتبر است.")
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 2**63 - 1:
        raise PaymentGatewayError("شناسه تراکنش زیبال نامعتبر است.")
    return int(text)


def parse_callback(data) -> dict[str, str]:
    """فقط پارامترهای Callback عادی زیبال، بدون پارامتر تکراری، پذیرفته می‌شوند."""
    parsed = {}
    for key in ("trackId", "success", "status", "orderId"):
        values = data.getlist(key) if hasattr(data, "getlist") else [data.get(key)]
        if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise PaymentGatewayError("اطلاعات بازگشت از درگاه نامعتبر است.")
        parsed[key] = values[0]
    _track_id(parsed["trackId"])
    if parsed["success"] not in ("0", "1") or parsed["status"] not in {
        str(status) for status in _STATUSES
    }:
        raise PaymentGatewayError("وضعیت بازگشت از درگاه نامعتبر است.")
    if len(parsed["orderId"]) > 64:
        raise PaymentGatewayError("شناسه سفارش در بازگشت از درگاه نامعتبر است.")
    return parsed


class ZibalGateway(PaymentGateway):
    name = "zibal"
    api_url = "https://gateway.zibal.ir"

    def __init__(self):
        self.merchant = settings.ZIBAL_MERCHANT.strip()
        if not self.merchant:
            raise PaymentGatewayError("ZIBAL_MERCHANT تنظیم نشده است.")
        if self.merchant == "zibal" and not settings.DEBUG:
            raise PaymentGatewayError("حساب آزمایشی زیبال در Production مجاز نیست.")
        self.timeout = settings.ZIBAL_TIMEOUT_SECONDS
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise PaymentGatewayError("مهلت اتصال به زیبال باید عددی مثبت باشد.")

    def _post(self, path: str, payload: dict) -> dict:
        request = Request(
            self.api_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with _http_open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise PaymentGatewayError("پاسخ HTTP ناموفق از زیبال دریافت شد.")
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise PaymentGatewayError("پاسخ HTTP ناموفق از زیبال دریافت شد.") from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise PaymentGatewayError("اتصال به زیبال برقرار نشد یا مهلت آن پایان یافت.") from exc
        except (ValueError, UnicodeError) as exc:
            raise PaymentGatewayError("پاسخ JSON زیبال نامعتبر است.") from exc
        if not isinstance(data, dict) or type(data.get("result")) is not int:
            raise PaymentGatewayError("ساختار پاسخ زیبال نامعتبر است.")
        return data

    @staticmethod
    def _amount_in_rials(amount) -> int:
        try:
            toman = Decimal(str(amount))
        except InvalidOperation as exc:
            raise PaymentGatewayError("مبلغ تراکنش نامعتبر است.") from exc
        if not toman.is_finite() or toman != toman.to_integral_value():
            raise PaymentGatewayError("مبلغ تراکنش باید به تومان صحیح باشد.")
        rials = int(toman) * 10
        if not 1_000 <= rials <= 4_000_000_000:
            raise PaymentGatewayError("مبلغ تراکنش خارج از محدوده مجاز زیبال است.")
        return rials

    @staticmethod
    def _api_error(result: int) -> PaymentGatewayError:
        # متن پاسخ API ممکن است اطلاعات حساس داشته باشد؛ تنها کد و پیام محلی ثبت می‌شوند.
        message = _ERROR_MESSAGES.get(result, "درخواست زیبال با خطا مواجه شد.")
        return PaymentGatewayError(f"{message} (کد {result})")

    def create_payment(self, order, request=None) -> PaymentRequest:
        origin = urlsplit(settings.SITE_URL)
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or (not settings.DEBUG and origin.scheme != "https")
        ):
            raise PaymentGatewayError("SITE_URL برای بازگشت از زیبال نامعتبر است.")
        data = self._post(
            "/v1/request",
            {
                "merchant": self.merchant,
                "amount": self._amount_in_rials(order.total),
                "callbackUrl": settings.SITE_URL.rstrip("/") + reverse("payments:callback"),
                "orderId": order.order_number,
                "description": f"پرداخت سفارش {order.order_number}",
                "mobile": order.customer.phone_number,
            },
        )
        if data["result"] != 100:
            raise self._api_error(data["result"])
        track_id = str(_track_id(data.get("trackId")))
        return PaymentRequest(
            authority=track_id, redirect_url=f"{self.api_url}/start/{track_id}"
        )

    def get_payment_url(self, payment) -> str:
        # URL از شناسه معتبر ساخته می‌شود؛ داده metadata مقصد Redirect را تعیین نمی‌کند.
        return f"{self.api_url}/start/{_track_id(payment.authority)}"

    def _check_details(self, payment, data: dict) -> int:
        amount = data.get("amount")
        if type(amount) is not int or amount != self._amount_in_rials(payment.amount):
            raise PaymentGatewayError("مبلغ گزارش زیبال با مبلغ تراکنش یکسان نیست.")
        if data.get("orderId") != payment.order.order_number:
            raise PaymentGatewayError("شناسه سفارش گزارش زیبال مطابقت ندارد.")
        if "trackId" in data and _track_id(data["trackId"]) != _track_id(payment.authority):
            raise PaymentGatewayError("شناسه تراکنش گزارش زیبال مطابقت ندارد.")
        status = data.get("status")
        if type(status) is not int or status not in _STATUSES:
            raise PaymentGatewayError("وضعیت گزارش زیبال نامعتبر است.")
        return status

    @staticmethod
    def _audit_data(data: dict, verify_result: int) -> dict:
        # اطلاعات کارت، merchant و پیام/توضیحات دلخواه در پایگاه داده ذخیره نمی‌شوند.
        return {
            "verify_result": verify_result,
            **{key: data[key] for key in ("result", "status", "amount", "orderId", "refNumber") if key in data},
        }

    def _verified_result(self, payment, data: dict, verify_result: int) -> VerificationResult:
        if self._check_details(payment, data) != 1:
            raise PaymentGatewayError("زیبال پرداخت تأییدشده‌ای برای این تراکنش گزارش نکرد.")
        reference = data.get("refNumber")
        if type(reference) is not int or reference <= 0 or len(str(reference)) > 64:
            raise PaymentGatewayError("شماره مرجع گزارش زیبال نامعتبر است.")
        try:
            paid_at = parse_datetime(data.get("paidAt", ""))
        except (TypeError, ValueError) as exc:
            raise PaymentGatewayError("زمان پرداخت گزارش زیبال نامعتبر است.") from exc
        if paid_at is None:
            raise PaymentGatewayError("زمان پرداخت گزارش زیبال نامعتبر است.")
        return VerificationResult(
            successful=True,
            status=PaymentStatus.SUCCESSFUL,
            transaction_id=payment.authority,
            reference_id=str(reference),
            message="پرداخت با موفقیت تأیید شد.",
            raw=self._audit_data(data, verify_result),
        )

    def verify_payment(self, payment, callback_data: dict | None = None) -> VerificationResult:
        callback = parse_callback(callback_data or {})
        if callback["trackId"] != payment.authority or callback["orderId"] != payment.order.order_number:
            raise PaymentGatewayError("اطلاعات بازگشت با تراکنش و سفارش مطابقت ندارد.")
        if payment.amount != payment.order.total:
            raise PaymentGatewayError("مبلغ تراکنش با مبلغ سفارش یکسان نیست.")

        payload = {"merchant": self.merchant, "trackId": _track_id(payment.authority)}
        data = self._post("/v1/verify", payload)
        result = data["result"]
        if result == 100:
            return self._verified_result(payment, data, result)
        if result not in (201, 202):
            raise self._api_error(result)

        # 201 به‌تنهایی اثبات پرداخت نیست. 202 نیز لزوماً وضعیت لغو را برنمی‌گرداند.
        # در هر دو حالت اطلاعات سروری استعلام می‌شود؛ success/status Callback ملاک نیستند.
        inquiry = self._post("/v1/inquiry", payload)
        if inquiry["result"] != 100:
            raise self._api_error(inquiry["result"])
        if result == 201:
            return self._verified_result(payment, inquiry, result)

        status = self._check_details(payment, inquiry)
        if status in (1, 2):
            raise PaymentGatewayError("وضعیت تراکنش تغییر کرده است؛ تأیید را دوباره انجام دهید.")
        if status == -1:
            return VerificationResult(
                successful=False,
                status=PaymentStatus.PENDING,
                message="تراکنش هنوز در انتظار پرداخت است.",
                raw=self._audit_data(inquiry, result),
            )
        cancelled = status == 3
        return VerificationResult(
            successful=False,
            status=PaymentStatus.CANCELLED if cancelled else PaymentStatus.FAILED,
            message="پرداخت توسط کاربر لغو شد." if cancelled else "پرداخت در زیبال ناموفق بود.",
            raw=self._audit_data(inquiry, result),
        )
