"""URL های Checkout و سفارش‌های کاربر."""

from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("checkout/", views.checkout_address, name="checkout"),
    path("checkout/review/", views.checkout_review, name="checkout_review"),
    path("orders/", views.order_list, name="list"),
    path("orders/<str:order_number>/", views.order_detail, name="detail"),
    path("orders/<str:order_number>/pay/", views.order_pay, name="pay"),
]
