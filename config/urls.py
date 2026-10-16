"""Root URL Configuration پروژه."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

# ظاهر و عنوان‌های پنل مدیریت (فارسی و RTL)
admin.site.site_header = "پنل مدیریت فروشگاه پوشاک"
admin.site.site_title = "مدیریت فروشگاه"
admin.site.index_title = "داشبورد مدیریت"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("store.urls")),
    path("accounts/", include("accounts.urls")),
    path("cart/", include("cart.urls")),
    path("", include("orders.urls")),
    path("payments/", include("payments.urls")),
]

handler400 = "common.views.bad_request"
handler403 = "common.views.permission_denied"
handler404 = "common.views.page_not_found"
handler500 = "common.views.server_error"

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
