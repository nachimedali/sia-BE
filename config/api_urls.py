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
from ai.views import (
    GenerateView,
    GenerationViewSet,
    HashtagSuggestionView,
    VoiceProfileViewSet,
)
from analytics.views import (
    AnalyticsBestTimesView,
    AnalyticsCommentsView,
    AnalyticsOverviewView,
    AnalyticsPostsView,
    AnalyticsSentimentView,
    AudienceCommentReplyView,
    RepurposeAcceptView,
    RepurposeDismissView,
    RepurposeQueueView,
    ZernioCommentWebhookView,
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
from collaboration.views import (
    ReviewApproveView,
    ReviewCommentView,
    ReviewLinkRevokeView,
    ReviewLinkView,
    ReviewPacketView,
    ThreadViewSet,
)
from common.health import HealthView
from content.views import (
    MediaAssetViewSet,
    PlatformRuleListView,
    PostTemplateViewSet,
    PostViewSet,
    RecurrenceRuleViewSet,
)
from notifications.views import NotificationListView, NotificationPreferenceView
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
from tools.views import ToolListView, ToolRunView
from trends.views import TrendListView, TrendRefreshView
from workspaces.views import (
    ApiKeyView,
    ApprovalChainStageView,
    AuditLogView,
    InvitationAcceptView,
    MembershipViewSet,
    OrganizationAddonView,
    OrganizationListView,
    WorkspaceInviteView,
    WorkspaceListCreateView,
    WorkspaceSettingsView,
)

router = DefaultRouter()
router.register("posts", PostViewSet, basename="post")
router.register("media", MediaAssetViewSet, basename="media-asset")
router.register("post-templates", PostTemplateViewSet, basename="post-template")
router.register("recurrence-rules", RecurrenceRuleViewSet, basename="recurrence-rule")
router.register("threads", ThreadViewSet, basename="thread")
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
    # --- organization & workspace (BUILD-PLAN Phase 0, P0-46..P0-50) ---
    path("organizations/", OrganizationListView.as_view(), name="organizations"),
    path("workspaces/", WorkspaceListCreateView.as_view(), name="workspaces"),
    path("workspaces/<int:pk>/invite/", WorkspaceInviteView.as_view(), name="workspace-invite"),
    # Unauthenticated by necessity: the invitee may have no account yet, and
    # the token is the credential. Hash-only, single-use, 14 days.
    path("invites/<str:token>/accept/", InvitationAcceptView.as_view(), name="invite-accept"),
    path("billing/addons/", OrganizationAddonView.as_view(), name="billing-addons"),
    # C-11 / P0-04: `/app/tools` had a frontend and no backend, so it 404'd.
    # Built rather than removed — six small forms over machinery that already
    # exists. `POST` is the one endpoint that calls a provider in-request; see
    # `tools/views.py` for why that exception is scoped rather than general.
    path("tools/", ToolListView.as_view(), name="tools"),
    path("tools/<str:slug>/", ToolRunView.as_view(), name="tool-run"),
    # Scopes ship now; keys are issued to customers in Phase 10 (P0-50).
    path("api-keys/", ApiKeyView.as_view(), name="api-keys"),
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
    # L-4a. A plain path like its siblings, and the `{pk}` is resolved through
    # a workspace-filtered queryset inside the view — another tenant's comment
    # id is a 404, not a 403 (Part 7 rule 3).
    path(
        "analytics/comments/<int:pk>/reply/",
        AudienceCommentReplyView.as_view(),
        name="analytics-comment-reply",
    ),
    # Unauthenticated by necessity, authenticated by signature — the same
    # shape as the Stripe handler, and next to it in spirit if not in the file.
    path(
        "webhooks/zernio/comment/",
        ZernioCommentWebhookView.as_view(),
        name="webhook-zernio-comment",
    ),
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
    # The default chain and its stages. A plain path for the same reason as
    # the settings toggle above: it answers for the caller's own workspace
    # rather than an id in the URL, so the tenancy sweep (A52) has nothing
    # here to walk. Depth is what Advanced buys (P2-13).
    path(
        "workspaces/approval-chain/",
        ApprovalChainStageView.as_view(),
        name="workspace-approval-chain",
    ),
    path("workspaces/audit-log/", AuditLogView.as_view(), name="workspace-audit-log"),
    # --- notifications (P2-12) ---
    # Plain paths: both answer for the caller in their current workspace rather
    # than for an id in the URL, so the tenancy sweep (A52) has nothing to walk.
    # Scoped by user *and* workspace — a colleague reading your notifications
    # would be a leak inside a tenant rather than across one.
    path("notifications/", NotificationListView.as_view(), name="notifications"),
    path(
        "notifications/preferences/",
        NotificationPreferenceView.as_view(),
        name="notification-preferences",
    ),
    # --- reference data ---
    path("categories/", CategoryListView.as_view(), name="categories"),
    # Platform facts, not tenant data — a caption limit is true whoever
    # asks. Served rather than mirrored in the frontend so `rules.py`
    # stays the one declaration (P1-05).
    path("platform-rules/", PlatformRuleListView.as_view(), name="platform-rules"),
    # --- ai ---
    path("ai/generate/", GenerateView.as_view(), name="ai-generate"),
    # A read, not a generation (P1-13): the corpus is category-shared, so
    # there is no workspace-scoped object here for the tenancy sweep to walk.
    path("ai/hashtags/", HashtagSuggestionView.as_view(), name="ai-hashtags"),
    # --- guest review: sharing a post with someone who has no account ---
    # `{pk}` is resolved through a workspace-filtered queryset inside the view,
    # so another tenant's post is a 404 and not a 403 (Part 7 rule 3) — the
    # same guarantee the sweep (A52) checks, applied where it does not reach.
    path("posts/<int:pk>/share/", ReviewLinkView.as_view(), name="post-share"),
    path(
        "posts/<int:pk>/share/<int:link_id>/revoke/",
        ReviewLinkRevokeView.as_view(),
        name="post-share-revoke",
    ),
    # Public and token-scoped, exactly like the reminder packet below: a token
    # is not a workspace-scoped pk, so the tenancy sweep has nothing to walk
    # and `collaboration.review.resolve` is the access control instead. The
    # audience narrowing on top of it is what P2-G1 checks.
    path("review/<str:token>/", ReviewPacketView.as_view(), name="review-packet"),
    path("review/<str:token>/approve/", ReviewApproveView.as_view(), name="review-approve"),
    path("review/<str:token>/comment/", ReviewCommentView.as_view(), name="review-comment"),
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
