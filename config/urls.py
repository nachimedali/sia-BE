from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("config.api_urls")),
]

# Keeps unmatched /api/ paths and unhandled crashes inside the single error
# envelope (design.md A3); non-API paths keep Django's default pages.
handler404 = "common.exceptions.api_404"
handler500 = "common.exceptions.api_500"
