"""URL های فروشگاه.

نکته: اسلاگ دسته‌بندی‌ها و محصولات می‌تواند فارسی باشد (`allow_unicode=True`)،
بنابراین از مبدل `<str:>` استفاده می‌شود که کاراکترهای غیر ASCII را هم می‌پذیرد.
"""

from django.urls import path

from . import views

app_name = "store"

urlpatterns = [
    path("", views.home, name="home"),
    path("store/", views.product_list, name="product_list"),
    path("store/category/<str:category_slug>/", views.product_list, name="category"),
    path("categories/", views.category_index, name="categories"),
    path("search/", views.search, name="search"),
    path("product/<str:slug>/", views.product_detail, name="product_detail"),
]
