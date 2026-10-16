"""پیکربندی Django Admin برای حساب کاربران."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.utils.translation import gettext_lazy as _

from .models import Address, OTPChallenge, User


class ShopUserCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("phone_number",)


class ShopUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


class AddressInline(admin.TabularInline):
    model = Address
    extra = 0
    fields = (
        "receiver_name",
        "receiver_phone",
        "province",
        "city",
        "postal_code",
        "is_default",
    )
    readonly_fields = ("created_at",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = ShopUserChangeForm
    add_form = ShopUserCreationForm
    add_fieldsets = ((_("حساب جدید"), {"fields": ("phone_number", "usable_password", "password1", "password2")}),)
    list_display = (
        "phone_number",
        "full_name",
        "is_phone_verified",
        "has_profile",
        "is_active",
        "is_staff",
        "date_joined",
    )
    list_filter = ("is_phone_verified", "is_active", "is_staff", "date_joined")
    search_fields = ("phone_number", "first_name", "last_name")
    ordering = ("-date_joined",)
    readonly_fields = ("date_joined", "last_login", "is_phone_verified")
    inlines = [AddressInline]
    actions = ["activate_users", "deactivate_users"]
    fieldsets = (
        (
            _("اطلاعات حساب"),
            {"fields": ("phone_number", "password", "is_active", "is_staff", "is_superuser")},
        ),
        (
            _("اطلاعات شخصی"),
            {"fields": ("first_name", "last_name", "is_phone_verified")},
        ),
        (_("مجوزهای دسترسی"), {"fields": ("groups", "user_permissions")}),
        (
            _("تاریخ‌ها"),
            {"fields": ("last_login", "date_joined")},
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            fields += ("is_staff", "is_superuser", "groups", "user_permissions")
        return fields

    def has_change_permission(self, request, obj=None):
        if obj and (obj.is_staff or obj.is_superuser) and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and (obj.is_staff or obj.is_superuser) and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description=_("نام کامل"))
    def full_name(self, obj):
        return obj.full_name or "—"

    @admin.display(description=_("پروفایل کامل"), boolean=True)
    def has_profile(self, obj):
        return obj.is_profile_complete

    @admin.action(description=_("فعال‌سازی کاربران انتخاب‌شده"))
    def activate_users(self, request, queryset):
        if not request.user.is_superuser:
            queryset = queryset.filter(is_staff=False, is_superuser=False)
        updated = queryset.update(is_active=True)
        self.message_user(request, _(f"{updated} کاربر فعال شد."))

    @admin.action(description=_("غیرفعال‌سازی کاربران انتخاب‌شده"))
    def deactivate_users(self, request, queryset):
        if not request.user.is_superuser:
            queryset = queryset.filter(is_staff=False, is_superuser=False)
        updated = queryset.update(is_active=False)
        self.message_user(request, _(f"{updated} کاربر غیرفعال شد."))


@admin.register(OTPChallenge)
class OTPChallengeAdmin(admin.ModelAdmin):
    """فقط وضعیت چالش‌ها قابل مشاهده است؛ کد خام هرگز نمایش داده نمی‌شود."""

    list_display = ("phone_number", "created_at", "expires_at", "attempts", "is_used")
    list_filter = ("is_used", "created_at")
    search_fields = ("phone_number",)
    ordering = ("-created_at",)
    readonly_fields = (
        "phone_number",
        "code_hash",
        "created_at",
        "expires_at",
        "attempts",
        "is_used",
        "used_at",
        "ip_address",
    )
    actions = ["delete_expired"]

    def has_add_permission(self, request):
        return False

    @admin.action(description=_("حذف چالش‌های منقضی قدیمی"))
    def delete_expired(self, request, queryset):
        from django.utils import timezone

        deleted = queryset.filter(expires_at__lt=timezone.now()).delete()
        self.message_user(request, _("چالش‌های منقضی حذف شدند."))


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = (
        "receiver_name",
        "user",
        "province",
        "city",
        "is_default",
        "updated_at",
    )
    list_filter = ("province", "is_default", "created_at")
    search_fields = ("receiver_name", "receiver_phone", "postal_code", "user__phone_number")
    ordering = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user")
