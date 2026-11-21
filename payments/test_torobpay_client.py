"""OAuth, envelope, timeout, currency and URL security: no external HTTP."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from http.client import IncompleteRead
import json
from threading import Event
import time
from unittest.mock import MagicMock, patch

from django.core.cache import caches
from django.test import SimpleTestCase, override_settings

from payments.gateways.base import PaymentGatewayError
from payments.gateways.torobpay_client import TorobPayClient, TorobPayError, _refresh_lock, parse_response
from payments.gateways.torobpay_data import eligible_amount, toman_to_rial, validate_payment_url
from payments.torobpay_services import refund_route
from payments.torobpay_test_support import CPGHTTPStubMixin, TOROBPAY_TEST_SETTINGS


@override_settings(**TOROBPAY_TEST_SETTINGS)
class CPGClientTests(CPGHTTPStubMixin, SimpleTestCase):
    def test_oauth_basic_utf8_json_and_bare_access_token(self):
        with override_settings(TOROBPAY_CLIENT_ID="synthetic-الف", TOROBPAY_CLIENT_SECRET="synthetic-ب"):
            token = TorobPayClient().access_token()
        request = self.api_calls("oauth")[0]
        self.assertEqual(token, self.jwt)
        self.assertEqual(request["path"], "/api/online/v1/oauth/token")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"]["Authorization"], "Basic " + base64.b64encode("synthetic-الف:synthetic-ب".encode()).decode())
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["payload"], {"username": "synthetic-cpg-merchant", "password": "synthetic-cpg-password"})

    def test_cache_reuse_and_refresh_at_buffer_boundary(self):
        now = time.time()
        with patch("payments.gateways.torobpay_client.time.time", return_value=now):
            client = TorobPayClient()
            client.access_token()
        with patch("payments.gateways.torobpay_client.time.time", return_value=now + 3299):
            self.assertEqual(TorobPayClient().access_token(), self.jwt)
        self.assertEqual(len(self.api_calls("oauth")), 1)
        with patch("payments.gateways.torobpay_client.time.time", return_value=now + 3300):
            client.access_token()
        self.assertEqual(len(self.api_calls("oauth")), 2)

    def test_jwt_exp_can_shorten_but_never_extend_documented_lifetime(self):
        now = time.time()
        for lifetime, expected_ttl in ((500, 200), (7200, 3300)):
            caches["default"].clear()
            claims = base64.urlsafe_b64encode(json.dumps({"exp": now + lifetime}).encode()).decode().rstrip("=")
            self.jwt = "e30." + claims + ".synthetic-signature"
            with patch("payments.gateways.torobpay_client.time.time", return_value=now):
                client = TorobPayClient()
                client.access_token()
            with patch("payments.gateways.torobpay_client.time.time", return_value=now + expected_ttl):
                self.assertIsNone(caches["default"].get(client.cache_key))

    def test_credentials_rotation_isolates_cache(self):
        TorobPayClient().access_token()
        with override_settings(TOROBPAY_PASSWORD="synthetic-rotated-password"):
            TorobPayClient().access_token()
        self.assertEqual(len(self.api_calls("oauth")), 2)

    def test_concurrent_worker_threads_refresh_only_once(self):
        started, release = Event(), Event()
        def slow(record):
            started.set()
            self.assertTrue(release.wait(5))
            return self.server_response(record, "oauth")
        self.responses["oauth"] = [slow]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(TorobPayClient().access_token)
            self.assertTrue(started.wait(5))
            second = pool.submit(TorobPayClient().access_token)
            release.set()
            self.assertEqual(first.result(5), second.result(5))
        self.assertEqual(len(self.api_calls("oauth")), 1)

    def test_redis_refresh_uses_owner_checked_lock_and_releases_it(self):
        from django.core.cache.backends.redis import RedisCache
        backend = MagicMock(spec=RedisCache)
        backend._cache = MagicMock()
        backend.make_key.return_value = "isolated-cpg-lock"
        lock = backend._cache.get_client.return_value.lock.return_value
        lock.acquire.return_value = True
        with _refresh_lock(backend, "synthetic-key", 10):
            lock.acquire.assert_called_once_with(blocking=True)
        lock.release.assert_called_once_with()
        backend._cache.get_client.return_value.lock.assert_called_once_with("isolated-cpg-lock", timeout=30, blocking_timeout=3)

    def test_unavailable_redis_lock_does_not_refresh_independently(self):
        from django.core.cache.backends.redis import RedisCache
        backend = MagicMock(spec=RedisCache)
        backend._cache = MagicMock()
        backend._cache.get_client.return_value.lock.return_value.acquire.return_value = False
        with self.assertRaises(TorobPayError), _refresh_lock(backend, "key", 10):
            self.fail("Cannot enter without the shared lock")
        self.http.assert_not_called()

    def test_oauth_errors_use_only_local_codes_and_messages(self):
        for status, code in ((403, 1000), (403, 1011), (404, 1017), (403, 1099), (403, 1023)):
            with self.subTest(code=code):
                self.responses["oauth"] = [self.error(code, status=status)]
                with self.assertRaises(TorobPayError) as error:
                    TorobPayClient().access_token()
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("synthetic-private-provider-text", str(error.exception))

    def test_malformed_oauth_responses_and_timeout_are_not_cached(self):
        for response in (
            self.http_response(body=b"broken"), self.http_response({}), self.http_response({"access_token": None}),
            self.http_response({"access_token": True}), self.http_response({"successful": True, "response": {"access_token": self.jwt}}),
            self.http_response({"access_token": "bad\r\nheader"}), self.http_response(body=b"\xff"), TimeoutError("synthetic-private-text"),
        ):
            with self.subTest(response=type(response).__name__):
                self.responses["oauth"] = [response]
                client = TorobPayClient()
                with self.assertRaises(TorobPayError):
                    client.access_token()
                self.assertIsNone(caches["default"].get(client.cache_key))

    def test_business_success_requires_status_and_valid_envelope(self):
        for status, data in (
            (200, {"successful": False, "error": {"code": 1053}}), (200, {"successful": 1, "response": {}}),
            (200, {"successful": "true", "response": {}}), (200, {"successful": True, "response": []}),
            (503, {"successful": True, "response": {}}), (302, {"successful": True, "response": {}}),
            (400, {"successful": False, "error": {"code": True}}), (400, {"successful": False, "error": []}),
            (400, {"successful": False, "error": {"code": 1000, "message": {}}}),
        ):
            with self.subTest(status=status, data=data), self.assertRaises(TorobPayError):
                parse_response(status, json.dumps(data).encode(), operation="verify")

    def test_duplicate_json_members_nonfinite_and_wrong_root_are_rejected(self):
        for body in (b'[]', b'null', b'{"successful":true,"successful":true,"response":{}}',
                     b'{"successful":true,"response":{"amount":NaN}}'):
            with self.subTest(body=body), self.assertRaises(TorobPayError):
                parse_response(200, body, operation="status")

    def test_explicit_connect_read_timeout_and_closed_connection(self):
        with override_settings(TOROBPAY_TIMEOUT_SECONDS=3.5):
            TorobPayClient().eligible(200000)
        for client in self.connections:
            client.connect.assert_called_once_with()
            client.sock.settimeout.assert_called_once_with(3.5)
            client.close.assert_called_once_with()
        self.assertTrue(all(record["timeout"] == 3.5 for record in self.requests))

    def test_get_401_refreshes_once_but_post_never_retries(self):
        self.responses["eligible"] = [self.error(1000, status=401)]
        TorobPayClient().eligible(200000)
        self.assertEqual(len(self.api_calls("eligible")), 2)
        self.assertEqual(len(self.api_calls("oauth")), 2)
        self.responses["verify"] = [self.error(1000, status=401)]
        with self.assertLogs("payments.gateways.torobpay_client", level="WARNING"), self.assertRaises(TorobPayError):
            TorobPayClient().operation("verify", "synthetic-token")
        self.assertEqual(len(self.api_calls("verify")), 1)

    def test_inactive_merchant_is_not_refreshed_or_retried(self):
        self.responses["eligible"] = [self.error(1099, status=403)]
        with self.assertLogs("payments.gateways.torobpay_client", level="WARNING"), self.assertRaises(TorobPayError):
            TorobPayClient().eligible(200000)
        self.assertEqual(len(self.api_calls("eligible")), 1)

    def test_safe_logs_exclude_credentials_jwt_token_and_provider_text(self):
        self.responses["verify"] = [self.error(1005, message=self.jwt + " synthetic-cpg-password synthetic-token")]
        with self.assertLogs("payments.gateways.torobpay_client", level="WARNING") as logs:
            with self.assertRaises(TorobPayError):
                TorobPayClient(payment_id=7, order_id=3, correlation_id="local-uuid").operation("verify", "synthetic-token")
        output = "\n".join(logs.output)
        for secret in (self.jwt, "synthetic-cpg-password", "synthetic-cpg-secret", "synthetic-token"):
            self.assertNotIn(secret, output)
        self.assertIn("payment_id=7", output)
        self.assertIn("operation=verify", output)

    def test_eligibility_text_cannot_echo_bearer_or_merchant_secret(self):
        for text in (self.jwt, "synthetic-cpg-password", "synthetic-cpg-secret"):
            self.title = text
            with self.assertRaises(TorobPayError):
                TorobPayClient().eligible(200000)

    def test_eligibility_boundaries_and_exact_integer_query(self):
        for amount in (200000, 1000000000):
            self.assertTrue(TorobPayClient().eligible(amount).eligible)
            self.assertEqual(self.api_calls("eligible")[-1]["path"], "/api/online/offer/v1/eligible?amount=" + str(amount))
        calls = len(self.requests)
        for amount in (199999, 1000000001, True, "200000", 200000.0):
            with self.subTest(amount=amount), self.assertRaises(TorobPayError):
                TorobPayClient().eligible(amount)
        self.assertEqual(len(self.requests), calls)

    def test_eligibility_requires_strict_boolean_and_messages(self):
        for changes in ({"eligible": "true"}, {"eligible": 1}, {"title_message": None}, {"description": []}):
            self.responses["eligible"] = [self.http_response({"successful": True, "response": {
                "eligible": True, "title_message": "ترب‌پی", "description": "توضیح", **changes,
            }})]
            with self.subTest(changes=changes), self.assertRaises(TorobPayError):
                TorobPayClient().eligible(200000)

    def test_truncated_or_oversized_response_is_unknown(self):
        for response in (self.http_response(body=b"x" * 65537), IncompleteRead(b"partial-sensitive-data", 10)):
            self.responses["status"] = [response]
            with self.assertLogs("payments.gateways.torobpay_client", level="WARNING"), self.assertRaises(TorobPayError):
                TorobPayClient().status("synthetic-token")


@override_settings(**TOROBPAY_TEST_SETTINGS)
class CPGMoneyAndConfigurationTests(SimpleTestCase):
    def test_decimal_conversion_once_and_exact_integral_rial(self):
        for value, expected in ((Decimal("120000"), 1200000), (120000, 1200000), ("120000", 1200000),
                                (Decimal("0.1"), 1), (Decimal("0"), 0)):
            with self.subTest(value=value):
                self.assertEqual(toman_to_rial(value), expected)
                self.assertIs(type(toman_to_rial(value)), int)

    def test_bad_money_never_serializes(self):
        for value in (None, True, False, [], {}, "", " 10", "1e2", "garbage", -1, 1.0,
                      Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("0.11")):
            with self.subTest(value=str(value)), self.assertRaises(PaymentGatewayError):
                toman_to_rial(value)

    def test_toman_eligibility_range_converts_once(self):
        self.assertEqual(eligible_amount(Decimal("20000")), 200000)
        self.assertEqual(eligible_amount(Decimal("100000000")), 1000000000)
        for value in (Decimal("19999"), Decimal("100000001")):
            with self.assertRaises(PaymentGatewayError):
                eligible_amount(value)

    def test_only_reviewed_https_torobpay_hosts_can_redirect(self):
        for value in ("https://cpg.torobpay.com/pay?payment_token=opaque", "https://torobpay.com/pay"):
            self.assertEqual(validate_payment_url(value), value)
        for value in ("http://cpg.torobpay.com/pay", "javascript:alert(1)", "//cpg.torobpay.com/pay",
                      "https://cpg.torobpay.com.evil.example/pay", "https://evil.example/pay",
                      "https://user:secret@cpg.torobpay.com/pay", "https://cpg.torobpay.com:444/pay",
                      "https://cpg.torobpay.com/pay#token", "https://cpg.torobpay.com/\\evil", "https://cpg.torobpay.com/\n",
                      "https://cpg.torobpay.com/pay?access_token=secret", "https://cpg.torobpay.com/pay?password=secret"):
            with self.subTest(value=value), self.assertRaises(PaymentGatewayError):
                validate_payment_url(value)

    def test_missing_credentials_and_bad_timeouts_urls_buffer_fail_locally(self):
        for name, value in (
            ("TOROBPAY_CLIENT_ID", ""), ("TOROBPAY_CLIENT_SECRET", ""), ("TOROBPAY_USERNAME", ""), ("TOROBPAY_PASSWORD", ""),
            ("TOROBPAY_TIMEOUT_SECONDS", 0), ("TOROBPAY_TIMEOUT_SECONDS", float("nan")), ("TOROBPAY_TIMEOUT_SECONDS", 31),
            ("TOROBPAY_BASE_URL", "http://cpg.torobpay.com"), ("TOROBPAY_BASE_URL", "https://evil.example"),
            ("TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS", 0), ("TOROBPAY_ALLOWED_PAYMENT_HOSTS", ["evil.example"]),
        ):
            with self.subTest(name=name), override_settings(**{name: value}), self.assertRaises(PaymentGatewayError):
                TorobPayClient()

    def test_cancel_refund_boundary_is_provider_expected_route_not_local_refund(self):
        now = datetime(2026, 11, 20, 12, tzinfo=datetime_timezone.utc)
        self.assertEqual(refund_route(now - timedelta(minutes=30) + timedelta(microseconds=1), now=now), "sep")
        self.assertEqual(refund_route(now - timedelta(minutes=30), now=now), "sep")
        self.assertEqual(refund_route(now - timedelta(minutes=30, microseconds=1), now=now), "paya")
        self.assertEqual(refund_route(None, now=now), "provider_determined")
