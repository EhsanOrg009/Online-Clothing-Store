"""صفحات خطای سفارشی (فارسی و RTL) و Healthcheck."""

from django.db import connection
from django.shortcuts import render
from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
@never_cache
def healthz(request):
    """بررسی زنده‌بودن پردازه (Liveness).

    عمداً هیچ Query یا I/O ندارد: اگر دیتابیس قطع باشد هم Gunicorn باید
    در وضعیت healthy بماند تا سرویس‌های وابسته بیهوده ری‌استارت نشوند.
    """
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


@require_GET
@never_cache
def readyz(request):
    """بررسی آمادگی برای سرو درخواست (Readiness) — شامل اتصال دیتابیس."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - فقط وضعیت را گزارش می‌کنیم
        return HttpResponse(
            "database unavailable",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


def bad_request(request, exception=None):
    return render(request, "errors/400.html", status=400)


def permission_denied(request, exception=None):
    return render(request, "errors/403.html", status=403)


def page_not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    # No request context processors: this must work when the database is down.
    content = render_to_string("errors/500.html", {"STORE_NAME": settings.STORE_NAME})
    return HttpResponse(content, status=500)


def csrf_failure(request, reason=""):
    return render(request, "errors/403.html", {"csrf_failure": True}, status=403)
