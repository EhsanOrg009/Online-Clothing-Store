from django.urls import path
from common import views
from config.urls import urlpatterns as shop_urls, handler400, handler403, handler404, handler500
from zibal_fixtures import gateway_return, mocked_api, require_isolated_database
require_isolated_database()
_payment_api = mocked_api()
_payment_api.start()
urlpatterns = [path('__qa__/400/', views.bad_request), path('__qa__/403/', views.permission_denied), path('__qa__/404/', views.page_not_found), path('__qa__/500/', views.server_error), path('__qa__/zibal/return/<str:track_id>/<str:outcome>/', gateway_return)] + shop_urls
