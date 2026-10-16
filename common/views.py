"""صفحات خطای سفارشی (فارسی و RTL)."""

from django.shortcuts import render
from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string


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
