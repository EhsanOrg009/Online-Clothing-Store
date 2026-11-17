"""تست‌های ارائه‌دهنده پیامک SMS.ir و سازگاری با معماری OTP."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import OTPChallenge
from accounts.otp import OTPError, request_otp
from accounts.sms import (
    ConsoleSMSProvider,
    InMemorySMSProvider,
    SMSIRProvider,
    SMSProviderError,
    get_sms_provider,
    reset_provider_cache,
)


class MockHTTPResponse:
    """پاسخ شبیه‌سازی‌شده HTTP برای تست‌های urllib."""

    def __init__(self, status: int, data: dict | bytes | str):
        self.status = status
        if isinstance(data, dict):
            self._content = json.dumps(data).encode("utf-8")
        elif isinstance(data, str):
            self._content = data.encode("utf-8")
        else:
            self._content = data

    def read(self) -> bytes:
        return self._content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None


class SMSIRProviderUnitTests(SimpleTestCase):
    """تست‌های واحد برای اعتبارسنجی تنظیمات و فراخوانی وب‌سرویس SMS.ir."""

    def setUp(self):
        super().setUp()
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)
        self.valid_kwargs = {
            "api_key": "test-api-key-12345",
            "template_id": 998877,
            "parameter_name": "Code",
            "timeout": 5.0,
        }

    # 1. Successful Verify API request
    # 2. Correct API URL
    # 3. Correct X-API-KEY header
    # 4. Correct JSON request structure
    # 5. Correct mobile number
    # 6. Correct template ID
    # 7. Correct configurable OTP parameter name
    # 8. Correct OTP value
    # 9. Successful response handling
    @patch("accounts.sms.sms_ir._http_open")
    def test_successful_verify_request_and_response(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={
                "status": 1,
                "message": "موفق",
                "data": {
                    "messageId": 12345678,
                    "cost": 1.0,
                },
            },
        )

        provider = SMSIRProvider(**self.valid_kwargs)
        provider.send_otp("09123456789", "54321", "پیامک آزمایشی")

        self.assertEqual(mock_http.call_count, 1)
        req = mock_http.call_args[0][0]

        # 2. Correct API URL
        self.assertEqual(req.full_url, "https://api.sms.ir/v1/send/verify")
        self.assertEqual(req.get_method(), "POST")

        # 3. Correct X-API-KEY header
        self.assertEqual(req.headers.get("X-api-key"), "test-api-key-12345")
        self.assertEqual(req.headers.get("Content-type"), "application/json")
        self.assertEqual(req.headers.get("Accept"), "application/json")

        # 4. Correct JSON request structure
        payload = json.loads(req.data.decode("utf-8"))
        self.assertIsInstance(payload, dict)

        # 5. Correct mobile number
        self.assertEqual(payload["mobile"], "09123456789")

        # 6. Correct template ID
        self.assertEqual(payload["templateId"], 998877)

        # 7. Correct parameter name
        self.assertEqual(len(payload["parameters"]), 1)
        self.assertEqual(payload["parameters"][0]["name"], "Code")

        # 8. Correct OTP value
        self.assertEqual(payload["parameters"][0]["value"], "54321")

    # 7. Correct configurable OTP parameter name (custom parameter and stripping hash)
    @patch("accounts.sms.sms_ir._http_open")
    def test_configurable_otp_parameter_name(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={"status": 1, "message": "موفق", "data": {"messageId": 999}},
        )

        # نام پارامتر با علامت # در ورودی داده شده
        provider = SMSIRProvider(
            api_key="test-key",
            template_id=123,
            parameter_name="#VerificationCode#",
        )
        provider.send_otp("09123456789", "12345", "test")

        req = mock_http.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["parameters"][0]["name"], "VerificationCode")

    # 10. Unsuccessful SMS.ir API status
    @patch("accounts.sms.sms_ir._http_open")
    def test_unsuccessful_sms_ir_api_status(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={
                "status": 0,
                "message": "اعتبار حساب کافی نیست.",
                "data": None,
            },
        )

        provider = SMSIRProvider(**self.valid_kwargs)
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")

        self.assertIn("اعتبار حساب کافی نیست", str(ctx.exception))
        self.assertIn("0", str(ctx.exception))

    # 11. HTTP errors
    @patch("accounts.sms.sms_ir._http_open")
    def test_http_error_handling(self, mock_http):
        fp = io.BytesIO(json.dumps({"message": "کلید API نامعتبر است"}).encode("utf-8"))
        mock_http.side_effect = HTTPError(
            url="https://api.sms.ir/v1/send/verify",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=fp,
        )

        provider = SMSIRProvider(**self.valid_kwargs)
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")

        self.assertIn("401", str(ctx.exception))
        self.assertIn("کلید API نامعتبر است", str(ctx.exception))
        # کلید در متن خطا نباید افشا شود
        self.assertNotIn("test-api-key", str(ctx.exception))

    # 12. Connection errors and timeouts
    @patch("accounts.sms.sms_ir._http_open")
    def test_connection_errors_and_timeouts(self, mock_http):
        mock_http.side_effect = URLError("Connection refused")
        provider = SMSIRProvider(**self.valid_kwargs)

        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")

        self.assertIn("اتصال به سامانه پیامک برقرار نشد", str(ctx.exception))

        mock_http.side_effect = TimeoutError("Timed out")
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")

        self.assertIn("اتصال به سامانه پیامک برقرار نشد", str(ctx.exception))

    # 13. Invalid JSON response
    @patch("accounts.sms.sms_ir._http_open")
    def test_invalid_json_response(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data="<html><body>502 Bad Gateway</body></html>",
        )

        provider = SMSIRProvider(**self.valid_kwargs)
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")

        self.assertIn("قالب JSON معتبر ندارد", str(ctx.exception))

    # 14. Missing or invalid response fields
    @patch("accounts.sms.sms_ir._http_open")
    def test_missing_or_invalid_response_fields(self, mock_http):
        provider = SMSIRProvider(**self.valid_kwargs)

        # بدون فیلد status
        mock_http.return_value = MockHTTPResponse(status=200, data={"message": "ok"})
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")
        self.assertIn("فاقد فیلد وضعیت", str(ctx.exception))

        # فیلد status از نوع غیر عددی
        mock_http.return_value = MockHTTPResponse(
            status=200, data={"status": "1", "data": {"messageId": 123}}
        )
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")
        self.assertIn("فاقد فیلد وضعیت", str(ctx.exception))

        # پاسخ موفق بدون بخش data
        mock_http.return_value = MockHTTPResponse(
            status=200, data={"status": 1, "message": "موفق"}
        )
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")
        self.assertIn("فاقد شناسه پیام", str(ctx.exception))

        # بخش data بدون messageId
        mock_http.return_value = MockHTTPResponse(
            status=200, data={"status": 1, "message": "موفق", "data": {"cost": 1.0}}
        )
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09123456789", "12345", "test")
        self.assertIn("فاقد شناسه پیام", str(ctx.exception))

    # 15. Missing API key
    def test_missing_api_key_validation(self):
        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="", template_id=123)
        self.assertIn("SMS_IR_API_KEY", str(ctx.exception))

    # 16. Missing or invalid template ID
    def test_missing_or_invalid_template_id_validation(self):
        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="valid-key", template_id=0)
        self.assertIn("SMS_IR_TEMPLATE_ID", str(ctx.exception))

        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="valid-key", template_id="invalid-id")
        self.assertIn("SMS_IR_TEMPLATE_ID", str(ctx.exception))

    # Missing or invalid parameter name or timeout
    def test_parameter_name_and_timeout_validation(self):
        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="valid-key", template_id=123, parameter_name="")
        self.assertIn("SMS_IR_OTP_PARAMETER_NAME", str(ctx.exception))

        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="valid-key", template_id=123, parameter_name="###")
        self.assertIn("SMS_IR_OTP_PARAMETER_NAME", str(ctx.exception))

        with self.assertRaises(SMSProviderError) as ctx:
            SMSIRProvider(api_key="valid-key", template_id=123, timeout=-1)
        self.assertIn("SMS_IR_TIMEOUT_SECONDS", str(ctx.exception))

    # Arbitrary text send rejection
    def test_arbitrary_send_is_rejected(self):
        provider = SMSIRProvider(**self.valid_kwargs)
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send("09123456789", "متن آزاد")
        self.assertIn("تنها از ارسال قالب‌بندی‌شده", str(ctx.exception))

    # Iranian mobile normalization
    @patch("accounts.sms.sms_ir._http_open")
    def test_mobile_number_normalization_and_validation(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={"status": 1, "message": "موفق", "data": {"messageId": 111}},
        )
        provider = SMSIRProvider(**self.valid_kwargs)

        # تبدیل +98 یا ارقام فارسی
        provider.send_otp("+989123456789", "12345", "test")
        req = mock_http.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["mobile"], "09123456789")

        # شماره نامعتبر
        with self.assertRaises(SMSProviderError):
            provider.send_otp("12345", "12345", "test")

    # Logging privacy: phone numbers masked and no secrets logged
    @patch("accounts.sms.sms_ir._http_open")
    def test_logging_masks_phone_and_excludes_otp_or_key(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={"status": 1, "message": "موفق", "data": {"messageId": 98765}},
        )
        provider = SMSIRProvider(**self.valid_kwargs)

        with self.assertLogs("shop.sms.sms_ir", level=logging.INFO) as log:
            provider.send_otp("09123456789", "99887", "test")

        output = "\n".join(log.output)
        self.assertIn("0912***6789", output)
        self.assertNotIn("test-api-key", output)
        self.assertNotIn("99887", output)


class SMSProviderRegistryAndCompatibilityTests(SimpleTestCase):
    """تست‌های رجیستری و سازگاری ارائه‌دهنده‌های مختلف پیامک."""

    def setUp(self):
        super().setUp()
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)

    # 17. Provider registry selection
    @override_settings(
        SMS_PROVIDER="sms_ir",
        SMS_IR_API_KEY="reg-key-123",
        SMS_IR_TEMPLATE_ID=112233,
        SMS_IR_OTP_PARAMETER_NAME="Code",
        SMS_IR_TIMEOUT_SECONDS=10.0,
    )
    def test_provider_registry_selects_sms_ir(self):
        provider = get_sms_provider()
        self.assertIsInstance(provider, SMSIRProvider)
        self.assertEqual(provider.name, "sms_ir")
        self.assertEqual(provider.api_key, "reg-key-123")
        self.assertEqual(provider.template_id, 112233)

    # 18. Backward compatibility with console and memory providers
    @override_settings(SMS_PROVIDER="memory")
    def test_memory_provider_send_otp_compatibility(self):
        provider = get_sms_provider()
        self.assertIsInstance(provider, InMemorySMSProvider)
        provider.clear()

        # متد send_otp باید به‌صورت خودکار پیامک را در outbox ثبت کند
        provider.send_otp("09121111111", "12345", "کد شما 12345 است")
        self.assertEqual(provider.outbox["09121111111"], ["کد شما 12345 است"])
        self.assertEqual(provider.last_message, "کد شما 12345 است")

    @override_settings(SMS_PROVIDER="console", DEBUG=True)
    def test_console_provider_send_otp_compatibility_in_development(self):
        provider = get_sms_provider()
        self.assertIsInstance(provider, ConsoleSMSProvider)

        with self.assertLogs("shop.sms.development", level=logging.INFO) as log:
            provider.send_otp("09122222222", "67890", "کد شما 67890 است")

        output = "\n".join(log.output)
        self.assertIn("09122222222", output)
        self.assertIn("کد شما 67890 است", output)

    @override_settings(SMS_PROVIDER="console", DEBUG=False)
    def test_console_provider_fails_in_production(self):
        provider = get_sms_provider()
        with self.assertRaises(SMSProviderError) as ctx:
            provider.send_otp("09122222222", "67890", "کد")
        self.assertIn("در Production یک Provider واقعی تنظیم کنید", str(ctx.exception))


class OTPServiceIntegrationTests(TestCase):
    """19. تست یکپارچگی سرویس OTP با ارائه‌دهنده پیامک SMS.ir از طریق send_otp()."""

    PHONE = "09129876543"

    def setUp(self):
        super().setUp()
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)

    @override_settings(
        SMS_PROVIDER="sms_ir",
        SMS_IR_API_KEY="otp-integration-key",
        SMS_IR_TEMPLATE_ID=554433,
        SMS_IR_OTP_PARAMETER_NAME="Code",
        OTP_RESEND_SECONDS=0,
    )
    @patch("accounts.sms.sms_ir._http_open")
    def test_request_otp_calls_send_otp_on_sms_ir(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={
                "status": 1,
                "message": "موفق",
                "data": {"messageId": 777666, "cost": 1.0},
            },
        )

        phone, challenge = request_otp(self.PHONE, ip_address="192.0.2.1")
        self.assertEqual(phone, self.PHONE)
        self.assertIsInstance(challenge, OTPChallenge)
        self.assertEqual(mock_http.call_count, 1)

        req = mock_http.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["mobile"], self.PHONE)
        self.assertEqual(payload["templateId"], 554433)
        self.assertEqual(payload["parameters"][0]["name"], "Code")

        # کد ارسالی باید دقیقاً ۵ رقم باشد و مقدار خام در challenge ذخیره نشده باشد
        code = payload["parameters"][0]["value"]
        self.assertEqual(len(code), 5)
        self.assertTrue(code.isdigit())
        self.assertNotIn(code, challenge.code_hash)

    @override_settings(
        SMS_PROVIDER="sms_ir",
        SMS_IR_API_KEY="otp-integration-key",
        SMS_IR_TEMPLATE_ID=554433,
        SMS_IR_OTP_PARAMETER_NAME="Code",
        OTP_RESEND_SECONDS=0,
    )
    @patch("accounts.sms.sms_ir._http_open")
    def test_request_otp_rolls_back_on_provider_failure(self, mock_http):
        mock_http.return_value = MockHTTPResponse(
            status=200,
            data={"status": 0, "message": "قالب تایید نشده است"},
        )

        with self.assertRaises(OTPError) as ctx:
            request_otp(self.PHONE, ip_address="192.0.2.1")

        self.assertIn("ارسال پیامک ممکن نشد", str(ctx.exception))
        # در صورت شکست، چالش ذخیره‌شده باید حذف شده باشد
        self.assertFalse(OTPChallenge.objects.filter(phone_number=self.PHONE).exists())


class ProductionSettingsSafetyTests(SimpleTestCase):
    """تست اعتبارسنجی تنظیمات Production برای SMS.ir."""

    def _run_production_check(self, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        import os
        import subprocess
        import sys

        base_env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SECRET_KEY": "123456789012345678901234567890123456789012345678901",
            "DATABASE_PASSWORD": "strongpassword",
            "ALLOWED_HOSTS": "minikandoo.ir",
            "SITE_URL": "https://minikandoo.ir",
            "API_URL": "https://api.minikandoo.ir",
            "ZIBAL_MERCHANT": "realmerchant",
            **env_overrides,
        }
        cmd = [
            sys.executable,
            "-c",
            "import django; django.setup();",
        ]
        return subprocess.run(cmd, env=base_env, capture_output=True, text=True)

    def test_production_fails_when_sms_ir_missing_key(self):
        res = self._run_production_check({"SMS_PROVIDER": "sms_ir", "SMS_IR_API_KEY": ""})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SMS_IR_API_KEY must be set", res.stderr)

    def test_production_fails_when_sms_ir_missing_or_invalid_template(self):
        res = self._run_production_check({
            "SMS_PROVIDER": "sms_ir",
            "SMS_IR_API_KEY": "valid-key",
            "SMS_IR_TEMPLATE_ID": "0",
        })
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SMS_IR_TEMPLATE_ID must be a positive integer", res.stderr)

    def test_production_succeeds_with_valid_sms_ir_config(self):
        res = self._run_production_check({
            "SMS_PROVIDER": "sms_ir",
            "SMS_IR_API_KEY": "valid-key",
            "SMS_IR_TEMPLATE_ID": "123456",
            "SMS_IR_OTP_PARAMETER_NAME": "Code",
        })
        self.assertEqual(res.returncode, 0, res.stderr)
