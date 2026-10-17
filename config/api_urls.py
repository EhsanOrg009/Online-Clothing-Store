"""API host boundary. Register real API endpoints here when implemented.

The existing shop is server-rendered Django, not a REST application. Never expose
its admin/cart/OTP forms on a second hostname or invent successful API responses.
"""
from django.http import JsonResponse
from django.urls import path
from common.views import healthz, readyz


def not_found(request, exception=None):
    return JsonResponse({"detail": "No API endpoint exists at this path."}, status=404)


urlpatterns = [
    path("healthz/", healthz),
    path("readyz/", readyz),
]
handler404 = not_found
