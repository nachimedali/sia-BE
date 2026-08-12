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
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFeature
from channels import services
from channels.models import SocialAccount
from channels.serializers import (
    ConnectCompleteRequestSerializer,
    ConnectCompleteResponseSerializer,
    ConnectStartResponseSerializer,
    SocialAccountSerializer,
)
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace
from content.models import Platform


def _redirect_url(request: Request) -> str:
    """Where the platform sends the user back to. Client-supplied so the same
    endpoint serves the app's own callback page and, later, a mobile deep
    link — but only ever a path, joined onto our own site, so this cannot be
    turned into an open redirect."""
    path = str(request.query_params.get("redirect_path") or "/app/settings/connections")
    if not path.startswith("/"):
        raise OCCSError("redirect_path must be a path on this site.", code="invalid_redirect")
    return f"{settings.SITE_URL.rstrip('/')}{path}"


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
            workspace=active_workspace(request),
            platform=data["platform"],
            params=data["params"],
            target_id=data.get("target_id", ""),
        )
        if isinstance(outcome, SocialAccount):
            return Response(
                ConnectCompleteResponseSerializer(
                    {"needs_selection": False, "account": outcome}
                ).data
            )
        return Response(
            ConnectCompleteResponseSerializer(
                {
                    "needs_selection": True,
                    "targets": [{"id": t.id, "name": t.name} for t in outcome.targets],
                    "params": outcome.params,
                }
            ).data
        )

    @extend_schema(
        request=None,
        responses={200: SocialAccountSerializer},
        summary="Disconnect a connected account",
    )
    @action(detail=True, methods=["post"])
    def disconnect(self, request: Request, pk: str | None = None) -> Response:
        account = services.disconnect(self.get_object())
        return Response(SocialAccountSerializer(account).data)

    @extend_schema(
        request=None,
        responses={200: ConnectStartResponseSerializer},
        summary="Start a fresh OAuth run for an account needing reauth",
    )
    @action(detail=True, methods=["post"])
    def reauth(self, request: Request, pk: str | None = None) -> Response:
        auth_url = services.start_reauth(
            account=self.get_object(), redirect_url=_redirect_url(request)
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
            workspace=active_workspace(request),
            platform=platform,
            redirect_url=_redirect_url(request),
        )
        return Response({"auth_url": auth_url})
