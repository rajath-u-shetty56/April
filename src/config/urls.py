from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("api/v1/", include("analytics_platform.catalog.urls")),
    path("api/v1/", include("analytics_platform.ingestion.urls")),
    path("api/v1/", include("analytics_platform.event_catalog.urls")),
]
