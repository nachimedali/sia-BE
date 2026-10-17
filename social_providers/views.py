"""The provider's inbound endpoint: SocialAPI's webhooks (SA-40)."""

from __future__ import annotations

from typing import Any

from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from social_providers.serializers import CockpitSerializer
from social_providers.socialapi import webhooks


class SocialAPIWebhookView(APIView):
    """`POST /webhooks/socialapi/` — unauthenticated by necessity,
    authenticated by signature (`socialapi.webhooks`).

    `204` once a delivery is recorded (or recognised as one already held),
    before any processing: the vendor allows ten seconds and retries anything
    else. A refusal answers in the usual error envelope.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @csrf_exempt
    @extend_schema(request=None, responses={204: None}, summary="SocialAPI webhook")
    def post(self, request: Request) -> Response:
        try:
            received = webhooks.receive(request.body, request.headers)
        except webhooks.WebhookRefusedError as refused:
            return Response(
                {"error": {"code": refused.code, "message": refused.message}},
                status=refused.status,
            )
        if received.fresh and received.delivery is not None:
            from social_providers.tasks import enqueue

            try:
                enqueue(received.delivery)
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "webhook recorded but not queued", exc_info=True
                )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CockpitView(APIView):
    """`GET /ops/cockpit/` — the provider cockpit (SA-80), staff only.

    Anyone else gets a 404, not a 403: the page should not admit it exists.
    Not tenant-scoped — it is the operator's view across every workspace.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: CockpitSerializer}, summary="Provider cockpit (staff only)")
    def get(self, request: Request) -> Response:
        if not getattr(request.user, "is_staff", False):
            raise NotFound()
        from social_providers.cockpit import cockpit

        return Response(CockpitSerializer(cockpit()).data)
