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
from ai.views import GenerateView, GenerationViewSet, VoiceProfileViewSet
from analytics.views import (
    AnalyticsBestTimesView,
    AnalyticsCommentsView,
    AnalyticsOverviewView,
    AnalyticsPostsView,
    AnalyticsSentimentView,
    RepurposeAcceptView,
    RepurposeDismissView,
    RepurposeQueueView,
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
from channels.views import ChannelConnectView, SocialAccountViewSet
from common.health import HealthView
from content.views import MediaAssetViewSet, PostViewSet
from onboarding.views import OnboardingCompleteView, OnboardingView
from products.views import (
    AutopilotApproveView,
    AutopilotQueueView,
    AutopilotRejectView,
    ProductViewSet,
)
from reminders.views import (
    ReminderConfirmView,
    ReminderPacketView,
    ReminderSkipView,
    ReminderSnoozeView,
    ReminderViewSet,
)
from trends.views import TrendListView, TrendRefreshView
from workspaces.views import AuditLogView, MembershipViewSet, WorkspaceSettingsView

router = DefaultRouter()
router.register("posts", PostViewSet, basename="post")
router.register("media", MediaAssetViewSet, basename="media-asset")
router.register("products", ProductViewSet, basename="product")
router.register("ai/generations", GenerationViewSet, basename="generation")
router.register("ai/voice-profiles", VoiceProfileViewSet, basename="voice-profile")
router.register("reminders", ReminderViewSet, basename="reminder")
router.register("channels", SocialAccountViewSet, basename="social-account")
router.register("workspaces/members", MembershipViewSet, basename="membership")

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
    # --- channels ---
    # A plain path, not a router action: `{platform}` is not a workspace-scoped
    # pk, so there is no object here for the tenancy sweep (A52) to walk — the
    # workspace comes from the session. The ViewSet that *does* own objects is
    # registered above.
    path(
        "channels/<str:platform>/connect/",
        ChannelConnectView.as_view(),
        name="channel-connect",
    ),
    # --- analytics ---
    # Plain paths: none of these returns a workspace-scoped object by pk, so
    # there is nothing for the tenancy sweep (A52) to walk. The two that do
    # take a pk resolve it through a workspace-filtered queryset, giving the
    # same 404-not-403 answer the shared mixin gives (A9).
    path("analytics/overview/", AnalyticsOverviewView.as_view(), name="analytics-overview"),
    path("analytics/posts/", AnalyticsPostsView.as_view(), name="analytics-posts"),
    path("analytics/best-times/", AnalyticsBestTimesView.as_view(), name="analytics-best-times"),
    path("analytics/sentiment/", AnalyticsSentimentView.as_view(), name="analytics-sentiment"),
    path("analytics/comments/", AnalyticsCommentsView.as_view(), name="analytics-comments"),
    path("analytics/repurpose/", RepurposeQueueView.as_view(), name="analytics-repurpose"),
    path(
        "analytics/repurpose/<int:pk>/accept/",
        RepurposeAcceptView.as_view(),
        name="analytics-repurpose-accept",
    ),
    path(
        "analytics/repurpose/<int:pk>/dismiss/",
        RepurposeDismissView.as_view(),
        name="analytics-repurpose-dismiss",
    ),
    # --- autopilot ---
    # Plain paths, not a router registration: the queue returns no object by
    # pk, and approve/reject resolve theirs through a workspace-filtered
    # queryset — the same guarantee the tenancy sweep (A52) checks, applied
    # where the sweep does not reach. The config itself is an action on
    # `ProductViewSet`, which the sweep does walk.
    path("autopilot/queue/", AutopilotQueueView.as_view(), name="autopilot-queue"),
    path("autopilot/<int:pk>/approve/", AutopilotApproveView.as_view(), name="autopilot-approve"),
    path("autopilot/<int:pk>/reject/", AutopilotRejectView.as_view(), name="autopilot-reject"),
    # --- trends ---
    # Plain paths, not a router registration: a `TrendCluster` belongs to a
    # `Category`, not to a workspace — every workspace in a vertical reads the
    # same corpus (D11) — so there is no workspace-scoped object for the
    # tenancy sweep (A52) to walk.
    path("trends/", TrendListView.as_view(), name="trends"),
    path("trends/refresh/", TrendRefreshView.as_view(), name="trends-refresh"),
    # --- workspaces: collaboration & roles (design.md §8.8) ---
    # Plain paths: both answer for the caller's own workspace rather than an
    # id in the URL, so there is nothing here for the tenancy sweep (A52) to
    # walk. `MembershipViewSet`, which does return objects by pk, is
    # registered on `router` above instead.
    path("workspaces/settings/", WorkspaceSettingsView.as_view(), name="workspace-settings"),
    path("workspaces/audit-log/", AuditLogView.as_view(), name="workspace-audit-log"),
    # --- reference data ---
    path("categories/", CategoryListView.as_view(), name="categories"),
    # --- ai ---
    path("ai/generate/", GenerateView.as_view(), name="ai-generate"),
    # --- reminders: public, token-scoped, no login (design.md §8.5) ---
    # Not on `router` — a token is not a workspace-scoped pk, so the
    # cross-workspace tenancy sweep (A52) has nothing to walk here;
    # `reminders.services.resolve_token` is the access control instead.
    path("reminders/<str:token>/packet/", ReminderPacketView.as_view(), name="reminder-packet"),
    path("reminders/<str:token>/confirm/", ReminderConfirmView.as_view(), name="reminder-confirm"),
    path("reminders/<str:token>/snooze/", ReminderSnoozeView.as_view(), name="reminder-snooze"),
    path("reminders/<str:token>/skip/", ReminderSkipView.as_view(), name="reminder-skip"),
    # --- content ---
    path("", include(router.urls)),
]
