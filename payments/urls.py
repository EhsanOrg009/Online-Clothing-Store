"""URL های پرداخت."""

from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("redirect/<int:pk>/", views.payment_redirect, name="redirect"),
    path("callback/", views.payment_callback, name="callback"),
    path("callback/<uuid:request_id>/", views.payment_callback, name="callback_request"),
    path("torobpay/callback/<uuid:request_id>/", views.torobpay_callback, name="torobpay_callback"),
    path(
        "result/<str:order_number>/success/",
        views.result_success,
        name="result_success",
    ),
    path(
        "result/<str:order_number>/failed/",
        views.result_failed,
        name="result_failed",
    ),
    path(
        "result/<str:order_number>/cancelled/",
        views.result_cancelled,
        name="result_cancelled",
    ),
]
