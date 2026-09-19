from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path, re_path
from django.views.static import serve

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("config.api_urls")),
]

# The filesystem fake hands out bare `/media/...` URLs; the frontend forwards
# that prefix here. S3 returns signed absolute URLs and needs no route, and
# prod pins the fake off, so this never serves private media unauthenticated
# anywhere but a local checkout.
if settings.USE_FAKE_STORAGE:
    urlpatterns += [
        re_path(
            rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$",
            serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]

# Keeps unmatched /api/ paths and unhandled crashes inside the single error
# envelope (design.md A3); non-API paths keep Django's default pages.
handler404 = "common.exceptions.api_404"
handler500 = "common.exceptions.api_500"
