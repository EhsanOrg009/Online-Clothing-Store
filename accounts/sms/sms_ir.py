"""ارائه‌دهنده پیامک SMS.ir مبتنی بر REST API و قالب‌های Verify (کد یک‌بارمصرف).

مستندات رسمی:
https://sms.ir/rest-api/
"""

from __future__ import annotations

import json
import logging
import math
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError

from common.validators import normalize_phone_number, validate_iranian_mobile

from .base import SMSProvider, SMSProviderError

logger = logging.getLogger("shop.sms.sms_ir")


class _NoAPIRedirect(HTTPRedirectHandler):
    """جلوگیری از دنبال‌کردن خودکار Redirect برای جلوگیری از نشت کلید API."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_http_open = build_opener(_NoAPIRedirect()).open


def _mask_phone(phone: str) -> str:
    """ماسک‌کردن شماره موبایل در لاگ‌ها جهت حفظ حریم خصوصی کاربران."""
    cleaned = re.sub(r"\D", "", phone or "")
    if len(cleaned) >= 7:
        return f"{cleaned[:4]}***{cleaned[-4:]}"
    return "***"


class SMSIRProvider(SMSProvider):
    """
    ارائه‌دهنده پیامک SMS.ir از طریق وب‌سرویس REST v1.

    ارسال کدهای اعتبارسنجی OTP از طریق اندپوینت `/send/verify` بر مبنای
    قالب‌های تاییدشده انجام می‌شود.
    """

    name = "sms_ir"
    api_url = "https://api.sms.ir/v1"
    verify_path = "/send/verify"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        template_id: int | str | None = None,
        parameter_name: str | None = None,
        timeout: float | None = None,
        api_url: str | None = None,
    ):
        raw_key = api_key if api_key is not None else getattr(settings, "SMS_IR_API_KEY", "")
        self.api_key = str(raw_key).strip() if raw_key else ""
        if not self.api_key:
            raise SMSProviderError("کلید API سامانه SMS.ir (SMS_IR_API_KEY) تنظیم نشده است.")

        raw_template = (
            template_id if template_id is not None else getattr(settings, "SMS_IR_TEMPLATE_ID", 0)
        )
        try:
            self.template_id = int(str(raw_template).strip())
            if self.template_id <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise SMSProviderError(
                "شناسه قالب SMS.ir (SMS_IR_TEMPLATE_ID) نامعتبر یا تنظیم نشده است. شناسه باید عدد صحیح مثبت باشد."
            )

        raw_param = (
            parameter_name
            if parameter_name is not None
            else getattr(settings, "SMS_IR_OTP_PARAMETER_NAME", "Code")
        )
        cleaned_param = str(raw_param).strip().strip("#") if raw_param else ""
        if not cleaned_param:
            raise SMSProviderError(
                "نام پارامتر OTP سامانه SMS.ir (SMS_IR_OTP_PARAMETER_NAME) تنظیم نشده است."
            )
        self.parameter_name = cleaned_param

        raw_timeout = (
            timeout if timeout is not None else getattr(settings, "SMS_IR_TIMEOUT_SECONDS", 10.0)
        )
        try:
            self.timeout = float(raw_timeout)
            if not math.isfinite(self.timeout) or self.timeout <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise SMSProviderError("مهلت اتصال به SMS.ir (SMS_IR_TIMEOUT_SECONDS) باید عددی مثبت باشد.")

        if api_url is not None:
            self.api_url = api_url.rstrip("/")

    def _post(self, path: str, payload: dict) -> dict:
        """ارسال درخواست HTTPS POST به وب‌سرویس SMS.ir با مدیریت خطاهای شبکه."""
        url = f"{self.api_url}{path}"
        body_bytes = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body_bytes,
            headers={
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with _http_open(request, timeout=self.timeout) as response:
                status_code = response.status
                raw_response = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = ""
            try:
                err_body = exc.read().decode("utf-8")
                err_json = json.loads(err_body)
                if isinstance(err_json, dict) and "message" in err_json:
                    detail = f": {err_json['message']}"
            except Exception:
                pass
            logger.warning("پاسخ HTTP ناموفق از سامانه SMS.ir دریافت شد (کد %s)", exc.code)
            raise SMSProviderError(
                f"پاسخ HTTP ناموفق از سامانه پیامک دریافت شد (کد {exc.code}){detail}"
            ) from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            logger.warning("خطا در برقراری ارتباط با سامانه SMS.ir یا اتمام مهلت زمانی: %s", exc.__class__.__name__)
            raise SMSProviderError("اتصال به سامانه پیامک برقرار نشد یا مهلت زمانی پایان یافت.") from exc

        if status_code not in (200, 201):
            raise SMSProviderError(f"پاسخ HTTP ناموفق از سامانه پیامک دریافت شد (کد {status_code}).")

        try:
            data = json.loads(raw_response)
        except (ValueError, UnicodeError) as exc:
            raise SMSProviderError("پاسخ دریافتی از سامانه پیامک قالب JSON معتبر ندارد.") from exc

        if not isinstance(data, dict):
            raise SMSProviderError("ساختار پاسخ دریافتی از سامانه پیامک نامعتبر است.")

        if "status" not in data or type(data["status"]) is not int:
            raise SMSProviderError("پاسخ سامانه پیامک فاقد فیلد وضعیت معتبر است.")

        if data["status"] != 1:
            api_message = data.get("message") or "نامشخص"
            raise SMSProviderError(
                f"سامانه پیامک درخواست را نپذیرفت: {api_message} (کد وضعیت: {data['status']})."
            )

        payload_data = data.get("data")
        if not isinstance(payload_data, dict) or "messageId" not in payload_data:
            raise SMSProviderError("پاسخ موفق سامانه پیامک فاقد شناسه پیام (messageId) است.")

        return data

    def send_otp(self, phone_number: str, code: str, message: str) -> None:
        """
        ارسال کد اعتبارسنجی (OTP) با استفاده از قالب تاییدشده در SMS.ir.

        متن کامل پیام نادیده گرفته می‌شود زیرا سرویس Verify سامانه SMS.ir فقط
        شناسه قالب و مقدار پارامتر را دریافت می‌کند.
        """
        phone = normalize_phone_number(str(phone_number or ""))
        try:
            validate_iranian_mobile(phone)
        except ValidationError as exc:
            raise SMSProviderError("شماره موبایل گیرنده پیامک نامعتبر است.") from exc

        code_str = str(code).strip()
        if not code_str or len(code_str) > 100:
            raise SMSProviderError("کد اعتبارسنجی ارسال‌شده نامعتبر یا بیش از حد طولانی است.")

        payload = {
            "mobile": phone,
            "templateId": self.template_id,
            "parameters": [
                {
                    "name": self.parameter_name,
                    "value": code_str,
                }
            ],
        }

        data = self._post(self.verify_path, payload)
        message_id = data.get("data", {}).get("messageId")
        logger.info(
            "پیامک اعتبارسنجی OTP با موفقیت به شماره %s ارسال شد (شناسه پیام: %s).",
            _mask_phone(phone),
            message_id,
        )

    def send(self, phone_number: str, message: str) -> None:
        """
        ارسال مستقیم پیامک متنی آزاد.

        سامانه SMS.ir برای اعتبارسنجی OTP از الگوهای متنی استفاده می‌کند و
        متن خام را نمی‌پذیرد.
        """
        raise SMSProviderError(
            "ارائه‌دهنده SMS.ir تنها از ارسال قالب‌بندی‌شده اعتبارسنجی (send_otp) پشتیبانی می‌کند. "
            "ارسال مستقیم متن آزاد امکان‌پذیر نیست."
        )
