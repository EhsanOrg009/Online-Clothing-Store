"""View های احراز هویت (شماره موبایل + OTP) و مدیریت حساب کاربری."""

from django.contrib import messages
from django.contrib.auth import login, logout
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods, require_POST

from common.ratelimit import client_ip

from .decorators import guest_only, login_required_message
from .forms import (
    AddressForm,
    OTPForm,
    PhoneForm,
    ProfileCompleteForm,
    ProfileEditForm,
)
from .models import Address
from .otp import OTPError, request_otp, verify_otp

BACKEND = "django.contrib.auth.backends.ModelBackend"


def _safe_redirect(request, target: str | None, default_url_name: str = "store:home"):
    """هدایت فقط به URL های داخلی (جلوگیری از Open Redirect)."""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(reverse(default_url_name))


# ---------------------------------------------------------------------
# ورود با شماره موبایل
# ---------------------------------------------------------------------
@guest_only
@require_http_methods(["GET", "POST"])
def login_view(request):
    next_url = request.GET.get("next", "")
    form = PhoneForm((request.POST if request.method == "POST" else None))

    if request.method == "POST":
        next_url = request.POST.get("next", "")
        if form.is_valid():
            phone = form.cleaned_data["phone_number"]
            try:
                request_otp(phone, client_ip(request))
            except OTPError as exc:
                messages.error(request, exc.message)
            else:
                request.session["otp_phone"] = phone
                request.session["otp_next"] = next_url
                return redirect("accounts:verify")

    return render(request, "accounts/access.html", {"form": form, "next": next_url})


@guest_only
@require_http_methods(["GET", "POST"])
def verify_view(request):
    phone = request.session.get("otp_phone")
    if not phone:
        messages.info(request, _("ابتدا شماره موبایل خود را وارد کنید."))
        return redirect("accounts:login")

    form = OTPForm((request.POST if request.method == "POST" else None))
    if request.method == "POST" and form.is_valid():
        try:
            user, created = verify_otp(
                phone, form.cleaned_data["code"], client_ip(request)
            )
        except OTPError as exc:
            messages.error(request, exc.message)
        else:
            guest_session_key = request.session.session_key
            login(request, user, backend=BACKEND)
            next_url = request.session.pop("otp_next", "")
            request.session.pop("otp_phone", None)

            # ادغام سبد خرید مهمان با سبد کاربر
            from cart.services import merge_session_cart

            merge_session_cart(request, user, session_key=guest_session_key)

            if created or not user.is_profile_complete:
                if next_url:
                    request.session["profile_next"] = next_url
                messages.success(
                    request, _("شماره شما تأیید شد. حالا اطلاعات خود را تکمیل کنید.")
                )
                return redirect("accounts:complete_profile")

            messages.success(
                request, _("خوش آمدید %(name)s!") % {"name": user.full_name}
            )
            return _safe_redirect(request, next_url)

    return render(request, "accounts/confirm.html", {"form": form, "phone": phone})


@require_POST
@guest_only
def resend_otp(request):
    phone = request.session.get("otp_phone")
    if not phone:
        return redirect("accounts:login")
    try:
        request_otp(phone, client_ip(request))
    except OTPError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, _("کد جدید ارسال شد."))
    return redirect("accounts:verify")


@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, _("با موفقیت خارج شدید."))
    return redirect("store:home")


# ---------------------------------------------------------------------
# تکمیل پروفایل
# ---------------------------------------------------------------------
@login_required_message
@require_http_methods(["GET", "POST"])
def complete_profile(request):
    initial = {
        "first_name": request.user.first_name,
        "last_name": request.user.last_name,
    }
    form = ProfileCompleteForm((request.POST if request.method == "POST" else None), initial=initial)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = request.user
        with transaction.atomic():
            user.first_name = data["first_name"]
            user.last_name = data["last_name"]
            user.save(update_fields=["first_name", "last_name"])

            if not user.addresses.exists():
                Address.objects.create(
                    user=user,
                    receiver_name=user.full_name,
                    receiver_phone=user.phone_number,
                    province=data["province"],
                    city=data["city"],
                    address_line=data["address_line"],
                    postal_code=data["postal_code"],
                    is_default=True,
                )
        messages.success(request, _("اطلاعات شما با موفقیت ثبت شد."))
        next_url = request.session.pop("profile_next", "")
        return _safe_redirect(request, next_url, default_url_name="accounts:profile")

    return render(request, "accounts/complete.html", {"form": form})


# ---------------------------------------------------------------------
# پروفایل
# ---------------------------------------------------------------------
@login_required_message
def profile(request):
    from orders.models import Order

    user = request.user
    orders_count = Order.objects.filter(customer=user).count()
    addresses = user.addresses.all()
    return render(
        request,
        "accounts/overview.html",
        {"profile_user": user, "orders_count": orders_count, "addresses": addresses},
    )


@login_required_message
@require_http_methods(["GET", "POST"])
def profile_edit(request):
    form = ProfileEditForm((request.POST if request.method == "POST" else None), instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("اطلاعات پروفایل بروزرسانی شد."))
        return redirect("accounts:profile")
    return render(request, "accounts/profile_edit.html", {"form": form})


# ---------------------------------------------------------------------
# آدرس‌ها
# ---------------------------------------------------------------------
@login_required_message
def address_list(request):
    addresses = request.user.addresses.all()
    return render(request, "accounts/address_list.html", {"addresses": addresses})


@login_required_message
@require_http_methods(["GET", "POST"])
def address_create(request):
    form = AddressForm((request.POST if request.method == "POST" else None))
    if request.method == "POST" and form.is_valid():
        address = form.save(commit=False)
        address.user = request.user
        if not request.user.addresses.exists():
            address.is_default = True
        address.save()
        messages.success(request, _("آدرس جدید ذخیره شد."))
        return redirect("accounts:address_list")
    return render(
        request,
        "accounts/address_form.html",
        {"form": form, "title": _("افزودن آدرس جدید")},
    )


@login_required_message
@require_http_methods(["GET", "POST"])
def address_update(request, pk: int):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    form = AddressForm((request.POST if request.method == "POST" else None), instance=address)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("آدرس بروزرسانی شد."))
        return redirect("accounts:address_list")
    return render(
        request,
        "accounts/address_form.html",
        {"form": form, "title": _("ویرایش آدرس")},
    )


@login_required_message
@require_POST
def address_delete(request, pk: int):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.delete()
    messages.success(request, _("آدرس حذف شد."))
    return redirect("accounts:address_list")


@login_required_message
@require_POST
def address_set_default(request, pk: int):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.is_default = True
    address.save(update_fields=["is_default"])
    messages.success(request, _("آدرس پیش‌فرض تغییر کرد."))
    return redirect("accounts:address_list")
