"""API v1 surface (design.md §7).

Routes are added phase by phase; everything lives under /api/v1/ and appears in
the OpenAPI schema, which is what the frontend client is generated from.
"""

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
)

from common.health import HealthView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # Phase 1 stub: issues and refreshes JWTs against the custom user model.
    # Registration, verification and password reset land in Phase 2.
    path("auth/login/", TokenObtainPairView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", TokenBlacklistView.as_view(), name="auth-logout"),
]
