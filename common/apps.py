from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "common"
    verbose_name = "امکانات مشترک"

    def ready(self) -> None:
        """قالب سفارشی داشبورد مدیریت را فعال می‌کند."""
        from django.contrib import admin

        admin.site.index_template = "admin/dashboard.html"
