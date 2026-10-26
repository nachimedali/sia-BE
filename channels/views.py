"""Channel endpoints (design.md §7).

Views parse and serialise; `channels.services` decides. `SocialAccountViewSet`
is registered on `router` so the Phase 4 tenancy sweep walks it (A52);
`ChannelConnectView` is a plain path because `{platform}` is not a
workspace-scoped pk — there is no object for the sweep to try to leak, and
the workspace comes from the session either way.

Every one of these is `HasFeature("auto_publish")`-gated (D4): connecting an
account is the paid path's first step, and Free must be stopped at the door
rather than allowed to connect something it can never publish through.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFeature
from channels import overview as connections_overview
from channels import services
from channels.adapters import get_connect_adapter
from channels.adapters.base import REMOTE_OPTION_SOURCES
from channels.models import SocialAccount
from channels.serializers import (
    AccountCheckSerializer,
    ConnectCompleteRequestSerializer,
    ConnectCompleteResponseSerializer,
    ConnectionsOverviewSerializer,
    ConnectStartResponseSerializer,
    DisconnectImpactSerializer,
    DisconnectRequestSerializer,
    DisconnectResultSerializer,
    PlatformSharesSerializer,
    RemoteOptionListSerializer,
    SocialAccountSerializer,
)
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import request_workspace
from content.models import Platform
from workspaces.models import Permission


def _redirect_url(request: Request) -> str:
    """Where the platform sends the user back to. Client-supplied so the same
    endpoint serves the app's own callback page and, later, a mobile deep
    link — but only ever a path, joined onto our own site, so this cannot be
    turned into an open redirect."""
    path = str(request.query_params.get("redirect_path") or "/app/settings/connections")
    if not path.startswith("/"):
        raise OCCSError("redirect_path must be a path on this site.", code="invalid_redirect")
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def _can_manage(request: Request) -> bool:
    """Disconnecting is an admin's call — it changes what publishes."""
    from workspaces.permissions import caller_permissions

    return Permission.ADMIN in caller_permissions(request)


def _require_manage(request: Request) -> None:
    if not _can_manage(request):
        raise PermissionDenied("Only workspace admins can disconnect accounts.")


class SocialAccountViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[SocialAccount],
):
    serializer_class = SocialAccountSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasFeature("auto_publish")]
    pagination_class = DefaultPagination
    queryset = SocialAccount.objects.all()

    @extend_schema(
        responses={200: RemoteOptionListSerializer},
        summary="A provider-backed option list for this account",
        description=(
            "The choices behind a `remote_choice` composer field — a "
            "Pinterest board list (P4-02). The `source` must be one "
            "`rules.py` declares for this account's platform; anything else "
            "is a **400**, because an endpoint that forwarded an arbitrary "
            "source would be a proxy for every call the vendor exposes."
        ),
    )
    @action(detail=True, methods=["get"], url_path=r"options/(?P<source>[\w-]+)")
    def remote_options(
        self, request: Request, pk: str | None = None, source: str | None = None
    ) -> Response:
        account = self.get_object()
        expected_platform = REMOTE_OPTION_SOURCES.get(source or "")
        if expected_platform is None or expected_platform != account.platform:
            raise OCCSError(
                f"{account.platform} declares no option list called {source!r}.",
                code="unknown_option_source",
                detail={"source": source, "platform": account.platform},
            )

        options = get_connect_adapter().list_remote_options(
            platform=account.platform,
            provider_account_id=account.provider_account_id,
            source=source or "",
        )
        return Response({"options": options})

    @extend_schema(
        request=ConnectCompleteRequestSerializer,
        responses={200: ConnectCompleteResponseSerializer},
        summary="Complete a headless OAuth connection",
        description=(
            "Called with the OAuth callback's query params. Returns the "
            "connected account, or — on Facebook and LinkedIn — the pages or "
            "organisations to choose from, which are posted back here with a "
            "`target_id` to finish (design.md §14, V1)."
        ),
    )
    @action(detail=False, methods=["post"])
    def complete(self, request: Request) -> Response:
        payload = ConnectCompleteRequestSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        outcome = services.complete_connect(
            workspace=request_workspace(request),
            platform=data["platform"],
            params=data["params"],
            target_id=data.get("target_id", ""),
            user=request.user,
        )
        if isinstance(outcome, SocialAccount):
            return Response(
                ConnectCompleteResponseSerializer(
                    {"needs_selection": False, "platform": data["platform"], "account": outcome}
                ).data
            )
        return Response(
            ConnectCompleteResponseSerializer(
                {
                    "needs_selection": True,
                    "platform": data["platform"],
                    "targets": [{"id": t.id, "name": t.name} for t in outcome.targets],
                    "params": outcome.params,
                }
            ).data
        )

    @extend_schema(
        responses={200: ConnectionsOverviewSerializer},
        summary="Every account as the connections page draws it, and the pooled cap",
    )
    @action(detail=False, methods=["get"])
    def overview(self, request: Request) -> Response:
        workspace = request_workspace(request)
        return Response(
            ConnectionsOverviewSerializer(
                {
                    "cards": connections_overview.account_cards(workspace),
                    "cap": connections_overview.cap_summary(workspace),
                    "can_manage": _can_manage(request),
                }
            ).data
        )

    @extend_schema(
        responses={200: PlatformSharesSerializer},
        summary="What each platform shares through the selected provider",
        description=(
            "The capability rows normalisation obeys — available, reporting "
            "delay, and why a metric is not available — so what the connect "
            "screen promises is what analytics then shows."
        ),
    )
    @action(detail=False, methods=["get"])
    def shares(self, request: Request) -> Response:
        request_workspace(request)
        return Response(PlatformSharesSerializer(connections_overview.platform_shares()).data)

    @extend_schema(
        responses={200: DisconnectImpactSerializer},
        summary="The scheduled posts a disconnect would affect",
    )
    @action(detail=True, methods=["get"])
    def impact(self, request: Request, pk: str | None = None) -> Response:
        return Response(
            DisconnectImpactSerializer(services.disconnect_impact(self.get_object())).data
        )

    @extend_schema(
        request=DisconnectRequestSerializer,
        responses={200: DisconnectResultSerializer},
        summary="Disconnect an account, and choose what happens to its scheduled posts",
        description=(
            "Admins only. Posts whose only destination is this account become "
            "reminders at the same time (`scheduled=remind`) or come off the "
            "calendar with their approval kept (`unschedule`). Posts that also "
            "go elsewhere keep publishing there. The account's history stays."
        ),
    )
    @action(detail=True, methods=["post"])
    def disconnect(self, request: Request, pk: str | None = None) -> Response:
        # Looked up first: another tenant's account is a 404 before it is a 403.
        account = self.get_object()
        _require_manage(request)
        payload = DisconnectRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        result = services.disconnect(
            account, actor=request.user, scheduled=payload.validated_data["scheduled"]
        )
        return Response(DisconnectResultSerializer(result).data)

    @extend_schema(
        request=None,
        responses={200: AccountCheckSerializer},
        summary="Ask the provider whether it still holds this account",
        description=(
            'The "Check status" button. An active account the provider no '
            "longer holds moves to reconnect and its admins are told once; an "
            "account already out of service is not asked about. A provider that "
            "cannot answer is a 422 and changes nothing."
        ),
    )
    @action(detail=True, methods=["post"])
    def check(self, request: Request, pk: str | None = None) -> Response:
        account = self.get_object()
        present = services.check_account(account)
        card = next(
            card
            for card in connections_overview.account_cards(account.workspace)
            if card["account"].pk == account.pk
        )
        return Response(
            AccountCheckSerializer(
                {"present": present, "checked_at": timezone.now(), "card": card}
            ).data
        )

    @extend_schema(
        request=None,
        responses={200: ConnectStartResponseSerializer},
        summary="Start a fresh OAuth run for an account needing reauth",
    )
    @action(detail=True, methods=["post"])
    def reauth(self, request: Request, pk: str | None = None) -> Response:
        auth_url = services.start_reauth(
            account=self.get_object(), redirect_url=_redirect_url(request), user=request.user
        )
        return Response({"auth_url": auth_url})


class ChannelConnectView(APIView):
    permission_classes: list[Any] = [IsAuthenticated, HasFeature("auto_publish")]

    @extend_schema(
        responses={200: ConnectStartResponseSerializer},
        summary="Get the provider connect URL for a platform",
        description=(
            "Starts the headless OAuth flow. The user authorises on the "
            "platform and returns to `redirect_path` on this site — never to "
            "a provider-branded screen (design.md D2, §14 V1)."
        ),
    )
    def get(self, request: Request, platform: str) -> Response:
        if platform not in Platform.values:
            raise OCCSError(
                "That platform is not supported.",
                code="unsupported_platform",
                detail={"platform": platform},
            )
        auth_url = services.start_connect(
            workspace=request_workspace(request),
            platform=platform,
            redirect_url=_redirect_url(request),
            user=request.user,
        )
        return Response({"auth_url": auth_url})
