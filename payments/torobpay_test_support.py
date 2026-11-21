"""Synthetic CPG merchant/server fixtures. All HTTP is intercepted."""

import json
from threading import Lock
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import caches
from django.db import connection
from django.utils import timezone


TOROBPAY_TEST_SETTINGS = {
    "DEBUG": True, "PAYMENT_GATEWAY": "zibal", "PAYMENT_GATEWAYS": ["zibal", "torobpay"],
    "ZIBAL_MERCHANT": "zibal", "SITE_URL": "https://minikandoo.example",
    "TOROBPAY_CLIENT_ID": "synthetic-cpg-client", "TOROBPAY_CLIENT_SECRET": "synthetic-cpg-secret",
    "TOROBPAY_USERNAME": "synthetic-cpg-merchant", "TOROBPAY_PASSWORD": "synthetic-cpg-password",
    "TOROBPAY_BASE_URL": "https://cpg.torobpay.com", "TOROBPAY_TIMEOUT_SECONDS": 10,
    "TOROBPAY_TOKEN_REFRESH_BUFFER_SECONDS": 300,
    "TOROBPAY_ALLOWED_PAYMENT_HOSTS": ["cpg.torobpay.com", "torobpay.com"],
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "cpg-mocked-tests"}},
}


class CPGHTTPStubMixin:
    def setUp(self):
        super().setUp()
        caches["default"].clear()
        self.requests, self.connections, self.remote = [], [], {}
        self.responses, self.lock = {}, Lock()
        self.eligible = True
        self.title = "خرید اعتباری با ترب‌پی"
        self.description = "پرداخت در چهار قسط"
        self.jwt = "synthetic-merchant-jwt"
        self.assert_unlocked_http = False
        self.http = self.enterContext(patch("payments.gateways.torobpay_client.HTTPSConnection"))
        self.http.side_effect = self.http_connection

    @staticmethod
    def http_response(data=None, *, status=200, body=None):
        response = MagicMock()
        response.status = status
        response.read.return_value = body if body is not None else json.dumps(data, ensure_ascii=False).encode()
        return response

    def http_connection(self, host, *, port, timeout):
        self.assertEqual(host, "cpg.torobpay.com")
        client = MagicMock()
        self.connections.append(client)
        record = {}
        def request(method, path, *, body, headers):
            if self.assert_unlocked_http:
                self.assertFalse(connection.in_atomic_block)
            record.update(method=method, path=path, headers=headers,
                          payload=json.loads(body) if body is not None else None, timeout=timeout)
            self.requests.append(record.copy())
        client.request.side_effect = request
        client.getresponse.side_effect = lambda: self.api_response(record)
        return client

    def api_calls(self, operation):
        if operation == "oauth":
            return [record for record in self.requests if urlsplit(record["path"]).path == "/api/online/v1/oauth/token"]
        if operation == "token":
            return [record for record in self.requests if urlsplit(record["path"]).path == "/api/online/payment/v1/token"]
        return [record for record in self.requests if urlsplit(record["path"]).path.endswith("/" + operation)]

    def api_response(self, record):
        operation = urlsplit(record["path"]).path.rsplit("/", 1)[-1]
        if operation == "token" and "/oauth/" in record["path"]:
            operation = "oauth"
        queue = self.responses.get(operation, [])
        if queue:
            value = queue.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value(record) if callable(value) and not isinstance(value, MagicMock) else value
        return self.server_response(record, operation)

    def server_response(self, record, operation):
        payload = record["payload"] or {}
        if operation == "oauth":
            return self.http_response({"access_token": self.jwt})
        if operation == "eligible":
            return self.http_response({"successful": True, "response": {
                "eligible": self.eligible, "title_message": self.title, "description": self.description,
            }})
        if operation == "token":
            with self.lock:
                token = "synthetic-payment-token-" + str(len(self.remote) + 1) + "-" + "x" * 100
                self.remote[token] = {"amount": payload["amount"], "transactionId": payload["transactionId"],
                                      "state": "NEW", "payload": payload}
            return self.http_response({"successful": True, "response": {
                "paymentToken": token, "paymentPageUrl": "https://cpg.torobpay.com/pay?payment_token=" + token,
            }})
        token = payload.get("paymentToken") if operation != "status" else parse_qs(urlsplit(record["path"]).query)["paymentToken"][0]
        if token not in self.remote:
            return self.error(1007, status=404)
        remote = self.remote[token]
        if operation == "status":
            status = {"NEW": "PENDING", "IPG": "PENDING", "W_FOR_VERIFY": "PENDING", "W_FOR_SETTLE": "VERIFY",
                      "ONGOING": "SETTLE", "PAID": "SETTLE", "UPDATED": "SETTLE", "FAILED": "REVERT", "CANCELED": "REVERT"}[remote["state"]]
            return self.http_response({"successful": True, "response": {
                "status": status, "amount": remote["amount"], "transactionId": remote["transactionId"],
            }})
        allowed = {"verify": {"W_FOR_VERIFY"}, "settle": {"W_FOR_SETTLE"}, "revert": {"W_FOR_VERIFY"},
                   "cancel": {"W_FOR_VERIFY", "W_FOR_SETTLE", "ONGOING"}, "update": {"ONGOING"}}
        if remote["state"] not in allowed[operation]:
            return self.error(1053)
        if operation == "update":
            if payload.get("discountAmount", 0):
                return self.error(1016)
            if payload["amount"] >= remote["amount"]:
                return self.error(1005)
            remote.update(amount=payload["amount"], transactionId="updated-" + remote["transactionId"])
        remote["state"] = {"verify": "W_FOR_SETTLE", "settle": "ONGOING", "revert": "CANCELED",
                           "cancel": "CANCELED", "update": "UPDATED"}[operation]
        return self.http_response({"successful": True, "response": {"transactionId": remote["transactionId"]}})

    def error(self, code, *, status=400, message="synthetic-private-provider-text"):
        return self.http_response({"successful": False, "error": {"code": code, "message": message,
                                                                  "data": None, "user_message": message}}, status=status)

    def outcome(self, payment, state):
        self.remote[payment.provider_token]["state"] = state

    @staticmethod
    def callback_data(payment, *, state="OK"):
        return {"transactionId": str(payment.request_id), "state": state, "amount": str(int(payment.amount) * 10)}
