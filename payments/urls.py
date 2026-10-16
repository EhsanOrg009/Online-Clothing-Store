"""URL های پرداخت."""

from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("redirect/<int:pk>/", views.payment_redirect, name="redirect"),
    path("callback/", views.payment_callback, name="callback"),
    path("mock/gateway/<str:authority>/", views.mock_gateway, name="mock_gateway"),
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
