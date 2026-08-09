"""API v1 surface (design.md §7).

Routes are added phase by phase; everything lives under /api/v1/ and appears in
the OpenAPI schema, which is what the frontend client is generated from.

`router` is the one place a ViewSet is registered. `test_cross_workspace_
access_returns_404_on_every_viewset` (Phase 4) walks `router.registry` and
asserts its length, so a ViewSet added anywhere else escapes the tenancy sweep
silently (design.md A52) — there is no second router.
"""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenBlacklistView, TokenRefreshView

from accounts.views import (
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegisterView,
    ResendVerificationView,
    ThrottledTokenObtainPairView,
    VerifyEmailView,
)
from billing.views import (
    BillingPortalView,
    CreditLedgerView,
    EntitlementsView,
    PackListView,
    PlanListView,
    PurchaseView,
    StripeWebhookView,
    SubscribeView,
    VideoLedgerView,
)
from categories.views import CategoryListView
from common.health import HealthView
from content.views import MediaAssetViewSet, PostViewSet
from onboarding.views import OnboardingCompleteView, OnboardingView

router = DefaultRouter()
router.register("posts", PostViewSet, basename="post")
router.register("media", MediaAssetViewSet, basename="media-asset")

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # --- auth ---
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/login/", ThrottledTokenObtainPairView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", TokenBlacklistView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("auth/verify-email/", VerifyEmailView.as_view(), name="auth-verify-email"),
    path("auth/resend-verify/", ResendVerificationView.as_view(), name="auth-resend-verify"),
    path("auth/password/reset/", PasswordResetRequestView.as_view(), name="auth-password-reset"),
    path(
        "auth/password/reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    # --- onboarding ---
    path("onboarding/", OnboardingView.as_view(), name="onboarding"),
    path("onboarding/complete/", OnboardingCompleteView.as_view(), name="onboarding-complete"),
    # --- billing ---
    path("billing/plans/", PlanListView.as_view(), name="billing-plans"),
    path("billing/entitlements/", EntitlementsView.as_view(), name="billing-entitlements"),
    path("billing/ledger/", CreditLedgerView.as_view(), name="billing-ledger"),
    path("billing/video-ledger/", VideoLedgerView.as_view(), name="billing-video-ledger"),
    path("billing/packs/", PackListView.as_view(), name="billing-packs"),
    path("billing/subscribe/", SubscribeView.as_view(), name="billing-subscribe"),
    path("billing/purchase/", PurchaseView.as_view(), name="billing-purchase"),
    path("billing/portal/", BillingPortalView.as_view(), name="billing-portal"),
    path("billing/webhook/stripe/", StripeWebhookView.as_view(), name="billing-webhook-stripe"),
    # --- reference data ---
    path("categories/", CategoryListView.as_view(), name="categories"),
    # --- content ---
    path("", include(router.urls)),
]
