"""Defensive TorobPay CPG HTTP/OAuth client; never retries a payment mutation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import logging
import math
from threading import Lock
import time
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache
from django.db import connection as database_connection
from redis.exceptions import RedisError

from .base import PaymentGatewayError
from .torobpay_data import API_STATUSES, Eligibility, RemoteStatus, TokenResult, identifier, payment_token, validate_payment_url


logger = logging.getLogger(__name__)
_LOCKS: dict[str, Lock] = {}
_LOCKS_GUARD = Lock()
_MAX_RESPONSE_BYTES = 65536
_PATHS = {
    "oauth": "/api/online/v1/oauth/token",
    "eligible": "/api/online/offer/v1/eligible",
    **{operation: "/api/online/payment/v1/" + operation for operation in (
        "token", "verify", "settle", "status", "revert", "cancel", "update",
    )},
}


class TorobPayError(PaymentGatewayError):
    """Only local messages/codes escape this client, never remote error text."""

    def __init__(self, *, operation, http_status=None, code=None, outcome="unknown"):
        self.operation, self.http_status, self.code, self.outcome = operation, http_status, code, outcome
        super().__init__({
            1016: "سفارش تخفیف‌دار در ترب‌پی قابل ویرایش نیست.",
            1065: "مهلت بازگشت فوری گذشته است؛ وضعیت بازپرداخت باید بررسی شود.",
            1053: "وضعیت پرداخت برای این عملیات مجاز نیست.",
            1003: "اطلاعات سفارش برای پرداخت با ترب‌پی معتبر نیست.",
        }.get(code, "ارتباط یا پاسخ ترب‌پی قابل تأیید نیست؛ لطفاً با پشتیبانی تماس بگیرید."))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def parse_response(status: int, body: bytes, *, operation: str) -> dict:
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(data, dict) or type(status) is not int:
            raise ValueError
        if operation == "oauth" and 200 <= status < 300:
            if "error" in data or data.get("successful") is False:
                raise ValueError
            token = data.get("access_token")
            payment_token(token)
            return {"access_token": token}
        if type(data.get("successful")) is not bool:
            raise ValueError
        if data["successful"] is True:
            if not 200 <= status < 300 or not isinstance(data.get("response"), dict):
                raise ValueError
            return data["response"]
        error = data.get("error")
        if (
            not isinstance(error, dict) or type(error.get("code")) is not int
            or not 0 <= error["code"] <= 99999
            or any(key in error and not isinstance(error[key], str) for key in ("message", "user_message"))
        ):
            raise ValueError
        raise TorobPayError(operation=operation, http_status=status, code=error["code"],
                            outcome="rejected" if 400 <= status < 500 else "unknown")
    except (ValueError, UnicodeError, TypeError, RecursionError, PaymentGatewayError) as exc:
        if isinstance(exc, TorobPayError):
            raise
        raise TorobPayError(operation=operation, http_status=status) from None


def validate_configuration(configuration=None):
    """Settings-only validation also usable before Django loads models."""
    config = configuration if configuration is not None else settings
    for name in ("TOROBPAY_CLIENT_ID", "TOROBPAY_CLIENT_SECRET", "TOROBPAY_USERNAME", "TOROBPAY_PASSWORD"):
        if not isinstance(getattr(config, name, None), str) or not getattr(config, name).strip():
            raise PaymentGatewayError(f"{name} برای درگاه ترب‌پی تنظیم نشده است.")
    if ":" in config.TOROBPAY_CLIENT_ID:
        raise PaymentGatewayError("شناسهٔ پذیرندهٔ ترب‌پی معتبر نیست.")
    origin = urlsplit(config.TOROBPAY_BASE_URL)
    try:
        if (origin.scheme != "https" or origin.hostname != "cpg.torobpay.com"
                or origin.username or origin.password or origin.path not in ("", "/")
                or origin.query or origin.fragment or origin.port not in (None, 443)):
            raise ValueError
        timeout = config.TOROBPAY_TIMEOUT_SECONDS
        buffer = config.TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS
        if (isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 30
                or type(buffer) is not int or not 30 <= buffer <= 900):
            raise ValueError
        hosts = config.TOROBPAY_ALLOWED_PAYMENT_HOSTS
        if not hosts or any(not isinstance(host, str) or not (
            host == "torobpay.com" or host.endswith(".torobpay.com")
        ) or not all(part and part.replace("-", "").isalnum() for part in host.split(".")) for host in hosts):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise PaymentGatewayError("نشانی یا تنظیمات اتصال ترب‌پی معتبر نیست.") from exc


@contextmanager
def _refresh_lock(backend, key, timeout):
    if isinstance(backend, RedisCache):
        # Django's RedisCache has no public lock method. The bundled redis-py
        # lock uses an owner token and atomic Lua compare/delete on release.
        redis = backend._cache.get_client(write=True)
        lock = redis.lock(backend.make_key(key + ":lock"), timeout=timeout * 2 + 10, blocking_timeout=3)
        if not lock.acquire(blocking=True):
            raise TorobPayError(operation="oauth")
        try:
            yield
        finally:
            try:
                lock.release()
            except RedisError:
                logger.warning("TorobPay OAuth refresh lease release failed")
    else:
        # Development cache is process-local; synchronize threads in that same
        # process. Production uses the shared Redis lease above.
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, Lock())
        if not lock.acquire(timeout=3):
            raise TorobPayError(operation="oauth")
        try:
            yield
        finally:
            lock.release()


class TorobPayClient:
    def __init__(self, *, order_id=None, payment_id=None, correlation_id=None):
        validate_configuration()
        self.origin = settings.TOROBPAY_BASE_URL.rstrip("/")
        self.timeout = settings.TOROBPAY_TIMEOUT_SECONDS
        self.context = (order_id, payment_id, str(correlation_id or ""))
        self._used_access_tokens = []
        # Rotation of any merchant credential isolates the cached JWT.
        material = json.dumps([self.origin, *[getattr(settings, name) for name in (
            "TOROBPAY_CLIENT_ID", "TOROBPAY_CLIENT_SECRET", "TOROBPAY_USERNAME", "TOROBPAY_PASSWORD",
        )]])
        self.cache_key = "torobpay:oauth:" + hashlib.sha256(material.encode()).hexdigest()

    def _http(self, operation, *, method="POST", payload=None, params=None, authorization):
        if database_connection.in_atomic_block and any(
            not getattr(block, "_from_testcase", False) for block in database_connection.atomic_blocks
        ):
            raise TorobPayError(operation=operation)
        origin = urlsplit(self.origin)
        connection = HTTPSConnection(origin.hostname, port=origin.port, timeout=self.timeout)
        try:
            # Explicit connect and read timeouts, no redirects and no retries.
            connection.connect()
            connection.sock.settimeout(self.timeout)
            path = _PATHS[operation] + ("?" + urlencode(params) if params else "")
            connection.request(method, path,
                body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None,
                headers={"Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise TorobPayError(operation=operation, http_status=response.status)
            return parse_response(response.status, body, operation=operation)
        except (HTTPException, OSError, ValueError, UnicodeError):
            raise TorobPayError(operation=operation) from None
        finally:
            connection.close()

    def access_token(self):
        backend = caches["default"]
        try:
            cached = backend.get(self.cache_key)
            if cached:
                return payment_token(cached)
            with _refresh_lock(backend, self.cache_key, self.timeout):
                cached = backend.get(self.cache_key)
                if cached:
                    return payment_token(cached)
                started = time.time()
                basic = base64.b64encode((settings.TOROBPAY_CLIENT_ID + ":" + settings.TOROBPAY_CLIENT_SECRET).encode("utf-8")).decode("ascii")
                data = self._http("oauth", authorization="Basic " + basic,
                    payload={"username": settings.TOROBPAY_USERNAME, "password": settings.TOROBPAY_PASSWORD})
                ttl = min(3600 - settings.TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS,
                          int(started + 3600 - settings.TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS - time.time()))
                # An exp claim received from the authenticated OAuth endpoint
                # may shorten the documented one-hour lifetime, never extend it.
                parts = data["access_token"].split(".")
                if len(parts) == 3:
                    try:
                        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                        expires = claims.get("exp")
                        if type(expires) in (int, float) and math.isfinite(expires):
                            ttl = min(ttl, int(expires - settings.TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS - time.time()))
                    except (ValueError, UnicodeError, AttributeError):
                        pass
                if ttl <= 0:
                    raise TorobPayError(operation="oauth")
                backend.set(self.cache_key, data["access_token"], timeout=ttl)
                return data["access_token"]
        except RedisError:
            raise TorobPayError(operation="oauth") from None

    def _call(self, operation, *, method="POST", payload=None, params=None):
        try:
            token = self.access_token()
            self._used_access_tokens = [*self._used_access_tokens[-1:], token]
            try:
                return self._http(operation, method=method, payload=payload, params=params, authorization="Bearer " + token)
            except TorobPayError as error:
                if error.http_status == 401 and error.code == 1000:
                    backend = caches["default"]
                    # Only clear the rejected JWT, preserving a newer refresh.
                    with _refresh_lock(backend, self.cache_key, self.timeout):
                        if backend.get(self.cache_key) == token:
                            backend.delete(self.cache_key)
                    if method == "GET":
                        refreshed = self.access_token()
                        self._used_access_tokens = [token, refreshed]
                        return self._http(operation, method=method, params=params, authorization="Bearer " + refreshed)
                raise
        except (TorobPayError, RedisError) as error:
            if not isinstance(error, TorobPayError):
                error = TorobPayError(operation=operation)
            logger.warning("TorobPay operation=%s order_id=%s payment_id=%s correlation_id=%s http=%s code=%s outcome=%s",
                           operation, *self.context, error.http_status, error.code, error.outcome)
            raise error from None

    def eligible(self, amount_rial: int) -> Eligibility:
        from .torobpay_data import MIN_ELIGIBLE_RIAL, MAX_ELIGIBLE_RIAL
        if type(amount_rial) is not int or not MIN_ELIGIBLE_RIAL <= amount_rial <= MAX_ELIGIBLE_RIAL:
            raise TorobPayError(operation="eligible", code=1011, outcome="rejected")
        data = self._call("eligible", method="GET", params={"amount": amount_rial})
        if type(data.get("eligible")) is not bool or any(
            not isinstance(data.get(key), str) or len(data[key]) > limit
            for key, limit in (("title_message", 256), ("description", 2000))
        ):
            raise TorobPayError(operation="eligible")
        if self._contains_secret(data["title_message"] + data["description"]):
            raise TorobPayError(operation="eligible")
        return Eligibility(data["eligible"], data["title_message"], data["description"])

    def _contains_secret(self, value):
        from urllib.parse import unquote
        decoded = unquote(value)
        return any(secret and (secret in value or secret in decoded) for secret in (
            *self._used_access_tokens, settings.TOROBPAY_CLIENT_SECRET, settings.TOROBPAY_PASSWORD,
        ))

    def create_token(self, payload) -> TokenResult:
        data = self._call("token", payload=payload)
        try:
            token = payment_token(data.get("paymentToken"))
            if self._contains_secret(token):
                raise PaymentGatewayError("پاسخ پرداخت شامل دادهٔ محرمانهٔ نامعتبر است.")
        except PaymentGatewayError:
            raise TorobPayError(operation="token") from None
        try:
            if self._contains_secret(data.get("paymentPageUrl", "") if isinstance(data.get("paymentPageUrl"), str) else ""):
                return TokenResult(token, "", redirect_valid=False)
            return TokenResult(token, validate_payment_url(data.get("paymentPageUrl")))
        except PaymentGatewayError:
            # Preserve a valid token for recovery even when redirect is hostile.
            return TokenResult(token, "", redirect_valid=False)

    def status(self, token) -> RemoteStatus:
        data = self._call("status", method="GET", params={"paymentToken": payment_token(token)})
        try:
            if data.get("status") not in API_STATUSES or type(data.get("amount")) is not int or data["amount"] <= 0:
                raise ValueError
            return RemoteStatus(data["status"], data["amount"], identifier(data.get("transactionId")))
        except (ValueError, TypeError, PaymentGatewayError):
            raise TorobPayError(operation="status") from None

    def operation(self, operation, token, *, payload=None) -> str:
        if operation not in ("verify", "settle", "revert", "cancel", "update"):
            raise ValueError("Unsupported CPG operation")
        data = self._call(operation, payload={**(payload or {}), "paymentToken": payment_token(token)})
        try:
            return identifier(data.get("transactionId"))
        except PaymentGatewayError:
            raise TorobPayError(operation=operation) from None
