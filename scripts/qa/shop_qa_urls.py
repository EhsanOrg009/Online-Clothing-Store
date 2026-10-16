from django.urls import path
from common import views
from config.urls import urlpatterns as shop_urls, handler400, handler403, handler404, handler500
urlpatterns = [path('__qa__/400/', views.bad_request), path('__qa__/403/', views.permission_denied), path('__qa__/404/', views.page_not_found), path('__qa__/500/', views.server_error)] + shop_urls
