"""Decorator های سفارشی حساب کاربری."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


def login_required_message(view_func):
    """مانند login_required اما با پیام فارسی برای کاربر."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib import messages

            messages.warning(
                request, _("برای دسترسی به این بخش ابتدا وارد حساب خود شوید.")
            )
            return redirect_to_login(request.get_full_path(), reverse("accounts:login"))
        return view_func(request, *args, **kwargs)

    return _wrapped


def guest_only(view_func):
    """کاربران واردشده را از صفحات ورود/ثبت‌نام دور می‌کند."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("store:home")
        return view_func(request, *args, **kwargs)

    return _wrapped
