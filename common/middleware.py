"""Middleware های اختصاصی پروژه.

نکته: Header مربوط به Content Security Policy توسط
`django.middleware.csp.ContentSecurityPolicyMiddleware` و تنظیم `SECURE_CSP`
مدیریت می‌شود و اینجا تکرار نمی‌شود.
"""

from django.conf import settings


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
        if not settings.DEBUG:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )
        return response
