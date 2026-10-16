"""URL های حساب کاربری."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("verify/", views.verify_view, name="verify"),
    path("resend/", views.resend_otp, name="resend"),
    path("complete-profile/", views.complete_profile, name="complete_profile"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("profile/edit/", views.profile_edit, name="profile_edit"),
    path("addresses/", views.address_list, name="address_list"),
    path("addresses/new/", views.address_create, name="address_create"),
    path("addresses/<int:pk>/edit/", views.address_update, name="address_update"),
    path("addresses/<int:pk>/delete/", views.address_delete, name="address_delete"),
    path(
        "addresses/<int:pk>/default/",
        views.address_set_default,
        name="address_set_default",
    ),
]
