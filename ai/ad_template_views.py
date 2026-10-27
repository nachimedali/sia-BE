"""Ad template endpoints (S17). Views parse and serialise;
`ai.services.ad_templates` decides. A product or generation of another tenant
is a 404 (rule 3); with `ad_templates_s17` off the routes do not exist."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import Generation
from ai.serializers import GenerationSerializer
from ai.services import ad_templates
from ai.tasks import run_generation_task
from billing.permissions import HasFlag
from billing.services.flags import AD_TEMPLATES_S17
from common.workspaces import authenticated_user, request_workspace
from products.models import Product
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.EDIT)]


class AdTemplateLengthSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    seconds = serializers.IntegerField()
    units = serializers.IntegerField()


class AdTemplateLockSerializer(serializers.Serializer[Any]):
    feature = serializers.CharField()
    suggested_plan = serializers.CharField()


class AdTemplateScriptSerializer(serializers.Serializer[Any]):
    hook = serializers.CharField()
    body = serializers.CharField()
    cta = serializers.CharField()


class AdTemplateSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    description = serializers.CharField()
    category = serializers.CharField()
    fits = serializers.ListField(child=serializers.CharField())
    person = serializers.ChoiceField(choices=list(ad_templates.PERSON))
    tier = serializers.ChoiceField(choices=list(ad_templates.TIER_FEATURE))
    lengths = AdTemplateLengthSerializer(many=True)
    beats = serializers.ListField(child=serializers.CharField())
    script = AdTemplateScriptSerializer()
    hooks = serializers.ListField(child=serializers.CharField())
    locations = serializers.ListField(child=serializers.CharField())
    needs = serializers.CharField(allow_blank=True)
    #: False while the format needs speech (T4): shown, never rendered.
    available = serializers.BooleanField()
    #: False when the template is made from the product's own photos (T3):
    #: there is no first frame to draw.
    frame = serializers.BooleanField()
    #: Why it cannot be made for this product (`template_fit`,
    #: `needs_screenshot`), or null.
    fit = serializers.CharField(allow_null=True)
    #: The plan feature that unlocks it, or null when the plan has it.
    lock = AdTemplateLockSerializer(allow_null=True)
    #: On an optional-person template, what adding a person needs, or null.
    person_lock = AdTemplateLockSerializer(allow_null=True)


class AdTemplateOptionSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]


class AdTemplateGallerySerializer(serializers.Serializer[Any]):
    templates = AdTemplateSerializer(many=True)
    categories = AdTemplateOptionSerializer(many=True)
    locations = AdTemplateOptionSerializer(many=True)
    #: Model re-rolls allowed per ad on this plan.
    rerolls = serializers.IntegerField()
    #: Product photos a render sends on this plan (`max_references`).
    references_cap = serializers.IntegerField(allow_null=True)
    references_total = serializers.IntegerField()


class AdKeyframeRequestSerializer(serializers.Serializer[Any]):
    template = serializers.CharField(max_length=40)
    product = serializers.IntegerField()
    avatar = serializers.IntegerField(required=False, allow_null=True, default=None)
    location = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    language = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")
    #: A keyframe of this ad to draw again — a re-roll, counted against
    #: `ad_rerolls`.
    reroll_of = serializers.IntegerField(required=False, allow_null=True, default=None)


def _product(request: Request, pk: Any) -> Product:
    product = Product.objects.filter(workspace=request_workspace(request), pk=pk).first()
    if product is None:
        raise NotFound
    return product


class AdTemplateGalleryView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(
        parameters=[OpenApiParameter("product", int, required=False)],
        responses={200: AdTemplateGallerySerializer},
        summary="The ad template gallery",
        description=(
            "Every active template with its fit for the product and its plan lock. Locked "
            "templates are listed, never hidden. 404 with `ad_templates_s17` off, or for a "
            "product of another workspace."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        raw = request.query_params.get("product")
        product = _product(request, raw) if raw not in (None, "") else None
        return Response(AdTemplateGallerySerializer(ad_templates.gallery(workspace, product)).data)


class AdTemplateKeyframeView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=AdKeyframeRequestSerializer,
        responses={201: GenerationSerializer},
        summary="Draw a template's first frame",
        description=(
            "Queues an image generation (poll `GET /ai/generations/{id}/`), paid in credits on "
            "pass. 400 when the template does not fit the product, needs a person or has none; "
            "402 `feature_not_available` for a template above the plan, `reroll_limit_reached` "
            "past `ad_rerolls`, `avatar_over_limit` for an avatar locked by a downgrade; 409 "
            "`template_unavailable` for a format not ready yet, `avatar_consent_required`; 404 "
            "for a product, avatar or generation of another workspace."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = AdKeyframeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        product = _product(request, data["product"])
        row, avatar = ad_templates.resolve_with_avatar(
            workspace,
            data["template"],
            product=product,
            avatar_id=data["avatar"],
            language=data["language"],
        )
        reroll_of = None
        if data["reroll_of"] is not None:
            reroll_of = Generation.objects.filter(workspace=workspace, pk=data["reroll_of"]).first()
            if reroll_of is None:
                raise NotFound
        generation = ad_templates.create_keyframe(
            workspace,
            user=authenticated_user(request),
            row=row,
            product=product,
            avatar=avatar,
            location=data["location"],
            reroll_of=reroll_of,
        )
        run_generation_task.delay(generation_id=generation.id, n=1)
        generation.refresh_from_db()
        return Response(GenerationSerializer(generation).data, status=status.HTTP_201_CREATED)


class AdScriptRequestSerializer(serializers.Serializer[Any]):
    template = serializers.CharField(max_length=40)
    product = serializers.IntegerField()
    language = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")
    #: The clip length, so the words fit it.
    seconds = serializers.IntegerField(required=False, min_value=0, max_value=60, default=0)


class AdScriptSerializer(serializers.Serializer[Any]):
    hook = serializers.CharField(allow_blank=True)
    body = serializers.CharField(allow_blank=True)
    cta = serializers.CharField(allow_blank=True)
    #: `model` when written for this product, `template` when the template's
    #: own words came back (nothing charged).
    by = serializers.ChoiceField(choices=["model", "template"])
    credits = serializers.IntegerField()


class AdTemplateScriptView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=AdScriptRequestSerializer,
        responses={200: AdScriptSerializer},
        summary="Write a template's script for a product",
        description=(
            "The text model writes the hook, body and call to action in the content language. "
            "Paid in credits only when its words pass their checks; otherwise the template's "
            "own words come back free (`by: template`). 402 `insufficient_credits` or "
            "`feature_not_available`; 400 when the template does not fit the product; 409 "
            "`template_unavailable`; 404 for another workspace's product or with "
            "`ad_templates_s17` off."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = AdScriptRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        script = ad_templates.write_script(
            request_workspace(request),
            key=data["template"],
            product=_product(request, data["product"]),
            language=data["language"],
            seconds=data["seconds"],
        )
        return Response(AdScriptSerializer(script).data)
