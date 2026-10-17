"""Middleware های اختصاصی پروژه.

نکته: Header مربوط به Content Security Policy توسط
`django.middleware.csp.ContentSecurityPolicyMiddleware` و تنظیم `SECURE_CSP`
مدیریت می‌شود و اینجا تکرار نمی‌شود.
"""

from django.conf import settings
from urllib.parse import urlsplit


class APIHostMiddleware:
    """Keep the API hostname separate from storefront sessions and admin routes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        api_host = urlsplit(settings.API_URL).hostname
        if api_host and request.get_host().split(":", 1)[0].lower() == api_host:
            request.urlconf = "config.api_urls"
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """افزودن Header های امنیتی که Django به‌صورت پیش‌فرض ارائه نمی‌دهد."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()"
        )
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        # SecurityMiddleware owns HSTS and only emits it on secure responses.
        return response
