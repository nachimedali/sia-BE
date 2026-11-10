"""Brand place endpoints (S17-28). Views parse and serialise;
`brand.services.places` decides. Every lookup is scoped to the request's
workspace, so another brand's place is a 404 (rule 3); with `ad_templates_s17`
off the routes do not exist."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFlag
from billing.services.flags import AD_TEMPLATES_S17
from brand.models import BrandPlace, PlaceSource
from brand.services import places
from common.workspaces import authenticated_user, request_workspace
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.EDIT)]


class BrandPlaceSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    #: What a template location names it: `own:<id>` for an upload, the
    #: catalogue key for a kept plate.
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    #: `upload` (a photo of the brand's place) or `made` (a kept catalogue place).
    origin = serializers.ChoiceField(choices=PlaceSource.choices)
    #: `ready`, `making` while its plate is drawn, or `failed`.
    status = serializers.CharField()
    image = serializers.CharField(allow_null=True)
    #: Credits this plate cost when it was made; 0 for an upload.
    credits = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class BrandPlaceListSerializer(serializers.Serializer[Any]):
    places = BrandPlaceSerializer(many=True)
    #: Credits keeping one more catalogue place costs.
    keep_credits = serializers.IntegerField()
    max_places = serializers.IntegerField()


class BrandPlaceCreateSerializer(serializers.Serializer[Any]):
    media = serializers.IntegerField()
    label = serializers.CharField(  # type: ignore[assignment]
        max_length=80, required=False, allow_blank=True, default=""
    )


class BrandPlaceKeepSerializer(serializers.Serializer[Any]):
    place = serializers.CharField(max_length=40)


def _out(place: BrandPlace) -> dict[str, Any]:
    made = place.source == PlaceSource.MADE
    generation = place.generation
    return {
        "id": place.pk,
        "key": place.place_key if made else f"{places.OWN}{place.pk}",
        "label": place.label,
        "origin": place.source,
        "status": places.status_of(place),
        "image": place.media.file.url if place.media is not None and place.media.file else None,
        "credits": int(generation.credits_charged or 0) if made and generation else 0,
        "created_at": place.created_at,
    }


class BrandPlaceListView(APIView):
    def get_permissions(self) -> list[Any]:
        return [permission() for permission in (_WRITE if self.request.method == "POST" else _READ)]

    @extend_schema(responses=BrandPlaceListSerializer, summary="The brand's places")
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        return Response(
            BrandPlaceListSerializer(
                {
                    "places": [_out(place) for place in places.list_places(workspace)],
                    "keep_credits": places.keep_credits(workspace),
                    "max_places": places.MAX_PLACES,
                }
            ).data
        )

    @extend_schema(
        request=BrandPlaceCreateSerializer,
        responses={201: BrandPlaceSerializer},
        summary="Add a photo of the brand's own place",
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        data = BrandPlaceCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        place = places.add(
            workspace,
            user=authenticated_user(request),
            media_id=data.validated_data["media"],
            label=data.validated_data["label"],
        )
        return Response(BrandPlaceSerializer(_out(place)).data, status=status.HTTP_201_CREATED)


class BrandPlaceKeepView(APIView):
    permission_classes = _WRITE

    @extend_schema(
        request=BrandPlaceKeepSerializer,
        responses={200: BrandPlaceSerializer, 201: BrandPlaceSerializer},
        summary="Keep a catalogue place: its plate is made once, paid on pass",
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        data = BrandPlaceKeepSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        place, made = places.keep(
            workspace, user=authenticated_user(request), place_key=data.validated_data["place"]
        )
        return Response(
            BrandPlaceSerializer(_out(place)).data,
            status=status.HTTP_201_CREATED if made else status.HTTP_200_OK,
        )


class BrandPlaceDetailView(APIView):
    permission_classes = _WRITE

    @extend_schema(responses={204: None}, summary="Remove a place (its photo stays)")
    def delete(self, request: Request, pk: int) -> Response:
        places.delete(request_workspace(request), pk)
        return Response(status=status.HTTP_204_NO_CONTENT)
