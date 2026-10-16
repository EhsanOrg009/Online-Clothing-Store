"""Context Processor های عمومی قالب‌ها."""

from django.conf import settings


def store_settings(request):
    """نمایش اطلاعات فروشگاه (از Settings/Environment) در تمام صفحات."""
    return {
        "STORE_NAME": settings.STORE_NAME,
        "STORE_PHONE": settings.STORE_PHONE,
        "STORE_EMAIL": settings.STORE_EMAIL,
        "STORE_ADDRESS": settings.STORE_ADDRESS,
        "SHIPPING_FEE": settings.SHIPPING_FEE,
        "FREE_SHIPPING_MIN_AMOUNT": settings.FREE_SHIPPING_MIN_AMOUNT,
    }


def dashboard_context(request):
    """آمار داشبورد فقط برای صفحه اصلی پنل مدیریت و کاربران Staff محاسبه می‌شود."""
    match = getattr(request, "resolver_match", None)
    user = getattr(request, "user", None)
    # نکته: نام‌فضای ادمین در `url_name` نیست؛ باید از `view_name` استفاده شود
    # (در غیر این صورت `url_name` برابر "index" است و این context processor
    # هیچ‌گاه فعال نمی‌شود).
    if not match or match.view_name != "admin:index":
        return {}
    if not (user and user.is_authenticated and user.is_staff):
        return {}
    from common.dashboard import build_dashboard_context

    return build_dashboard_context(user)
