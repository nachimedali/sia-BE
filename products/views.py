"""Product and autopilot endpoints (design.md §7). No destroy — the endpoint
list is `GET/POST /products/` and `GET/PATCH /products/{id}/` only; deleting a
product that later phases' posts/generations point at is not a Phase 5
decision to make.

The three autopilot views are plain paths rather than a second router
registration: `/autopilot/queue/` returns no object by pk, and a draft is
reached through a workspace-filtered queryset, which gives the same
404-not-403 answer the shared mixin gives where the mixin does not reach (the
same shape `analytics/views.py` uses). Autopilot is a paid feature (§4.1), so
all four carry `HasFeature("autopilot")`.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from billing.permissions import HasFeature
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.models import Platform, PostStatus
from products.models import AutopilotConfig, AutopilotDraft, AutopilotDraftStatus, Product
from products.serializers import (
    AutopilotConfigSerializer,
    AutopilotDraftSerializer,
    DraftRejectRequestSerializer,
    ProductCompletenessSerializer,
    ProductReferenceImagesUploadSerializer,
    ProductSerializer,
)

# Aliased: `ProductViewSet.autopilot` is a route name fixed by design.md §7's
# `/products/{id}/autopilot/`, and it would otherwise shadow the module.
from products.services import autopilot as autopilot_service
from products.services.completeness import completeness_payload
from products.services.products import (
    attach_reference_images,
    create_product,
    detach_reference_image,
    update_product,
)

AUTOPILOT_FEATURE = "autopilot"

# The products page's "high completeness" filter. A display threshold, not a
# gate: nothing is allowed or refused on it.
HIGH_COMPLETENESS = 80

PRODUCT_STATUS_FILTERS: dict[str, Q] = {
    "ready": Q(is_generation_ready=True),
    "needs_reference": Q(is_generation_ready=False),
    "high_completeness": Q(completeness_score__gte=HIGH_COMPLETENESS),
    "autopilot_on": Q(autopilot__enabled=True),
}


class ProductViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet[Product],
):
    serializer_class = ProductSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = (
        Product.objects.select_related("category", "autopilot")
        .prefetch_related("reference_images")
        .annotate(
            post_count=Count("posts", distinct=True),
            published_count=Count(
                "posts", filter=Q(posts__status=PostStatus.PUBLISHED), distinct=True
            ),
        )
    )

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, description="Matches name or description."),
            OpenApiParameter(
                "status",
                str,
                enum=list(PRODUCT_STATUS_FILTERS),
                description="An unknown value is a 400 rather than a silently full page.",
            ),
            OpenApiParameter("platform", str, enum=Platform.values),
        ]
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> Any:
        """Filters run here, not in the browser: the list paginates, so
        filtering the loaded page would miss every match past it."""
        queryset = super().get_queryset()
        if self.action != "list":
            return queryset
        params = self.request.query_params

        q = params.get("q", "").strip()
        if q:
            queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q))

        status = params.get("status")
        if status:
            if status not in PRODUCT_STATUS_FILTERS:
                raise OCCSError(
                    f"Unknown product status: {status}.",
                    code="invalid_status",
                    detail={"status": [status]},
                )
            queryset = queryset.filter(PRODUCT_STATUS_FILTERS[status])

        platform = params.get("platform")
        if platform:
            if platform not in Platform.values:
                raise OCCSError(
                    f"Unknown platform: {platform}.",
                    code="invalid_platform",
                    detail={"platform": [platform]},
                )
            queryset = queryset.filter(platforms__contains=[platform])

        return queryset

    def perform_create(self, serializer: BaseSerializer[Product]) -> None:
        assert isinstance(serializer, ProductSerializer)  # always this view's own serializer_class
        data = serializer.validated_data
        serializer.instance = create_product(workspace=request_workspace(self.request), **data)

    def perform_update(self, serializer: BaseSerializer[Product]) -> None:
        assert isinstance(serializer, ProductSerializer)  # always this view's own serializer_class
        assert serializer.instance is not None  # set by UpdateModelMixin.get_object() beforehand
        serializer.instance = update_product(serializer.instance, **serializer.validated_data)

    @extend_schema(
        request=ProductReferenceImagesUploadSerializer,
        responses={200: ProductSerializer},
        summary="Attach reference images",
        description="Ingests each uploaded file as a MediaAsset and attaches it as a "
        "reference image in one call (design.md §7, I7).",
    )
    @action(detail=True, methods=["post"], url_path="reference-images")
    def reference_images(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        uploads = request.FILES.getlist("files")
        if not uploads:
            raise OCCSError("No files were uploaded.", code="missing_file")
        attach_reference_images(product=product, uploads=uploads)
        product.refresh_from_db()
        return Response(self.get_serializer(product).data)

    @extend_schema(
        request=None,
        responses={200: ProductSerializer},
        summary="Detach a reference image",
        description="Unlinks the asset from this product and recomputes readiness. The "
        "MediaAsset itself is immutable and is kept.",
    )
    @action(
        detail=True,
        methods=["delete"],
        url_path=r"reference-images/(?P<asset_id>\d+)",
    )
    def detach_reference(
        self, request: Request, pk: str | None = None, asset_id: str | None = None
    ) -> Response:
        product = self.get_object()
        # Through the product's own relation: an asset that is not attached
        # here, or belongs to another tenant, is the same 404.
        asset = get_object_or_404(product.reference_images.all(), pk=asset_id)
        detach_reference_image(product=product, media_asset=asset)
        return Response(self.get_serializer(self.get_queryset().get(pk=product.pk)).data)

    @extend_schema(responses={200: ProductCompletenessSerializer})
    @action(detail=True, methods=["get"])
    def completeness(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        return Response(completeness_payload(product))

    @extend_schema(
        request=AutopilotConfigSerializer,
        responses={200: AutopilotConfigSerializer},
        summary="Read or retune this product's autopilot",
        description="Creates the config at its defaults on first read, so the product "
        "page always has something to render (design.md §6.4, §8.7).",
    )
    @action(
        detail=True,
        methods=["get", "patch"],
        permission_classes=[IsAuthenticated, HasFeature(AUTOPILOT_FEATURE)],
    )
    def autopilot(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        config, _created = AutopilotConfig.objects.get_or_create(product=product)

        if request.method == "PATCH":
            serializer = AutopilotConfigSerializer(config, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            config = autopilot_service.update_config(config, **serializer.validated_data)

        return Response(AutopilotConfigSerializer(config).data)


class _AutopilotView(APIView):
    """Autopilot is paid (§4.1) — a 402 with an upgrade payload, not a 403."""

    permission_classes: list[Any] = [IsAuthenticated, HasFeature(AUTOPILOT_FEATURE)]

    def draft(self, request: Request, pk: int) -> AutopilotDraft:
        # Filtered by workspace before the pk is applied, so another
        # workspace's draft is a 404 here rather than a 403 (design.md A9).
        return get_object_or_404(
            AutopilotDraft.objects.filter(product__workspace=request_workspace(request)), pk=pk
        )


class AutopilotQueueView(_AutopilotView):
    @extend_schema(
        responses={200: AutopilotDraftSerializer(many=True)},
        summary="Drafts waiting for review, soonest slot first",
    )
    def get(self, request: Request) -> Response:
        drafts = (
            AutopilotDraft.objects.filter(
                product__workspace=request_workspace(request),
                status=AutopilotDraftStatus.PENDING,
            )
            .select_related("product")
            .prefetch_related("generation__variants__media_asset")
        )
        return Response(AutopilotDraftSerializer(drafts, many=True).data)


class AutopilotApproveView(_AutopilotView):
    @extend_schema(
        request=None,
        responses={200: AutopilotDraftSerializer},
        summary="Approve a draft: creates the post and schedules its slot",
    )
    def post(self, request: Request, pk: int) -> Response:
        draft = autopilot_service.approve_draft(
            self.draft(request, pk), actor=authenticated_user(request)
        )
        return Response(AutopilotDraftSerializer(draft).data)


class AutopilotRejectView(_AutopilotView):
    @extend_schema(
        request=DraftRejectRequestSerializer,
        responses={200: AutopilotDraftSerializer},
        summary="Reject a draft, retiring its slot",
        description=(
            "`reason_code` comes from the fixed vocabulary (P5-12) and lands "
            "in the decision log. Structured codes are what make rejections "
            'aggregable — *"63% of your rejections were off_brand_voice"* '
            "routes to a profile revision, and free text routes nowhere."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        payload = DraftRejectRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        draft = autopilot_service.reject_draft(
            self.draft(request, pk),
            actor=authenticated_user(request),
            reason_code=payload.validated_data["reason_code"],
        )
        return Response(AutopilotDraftSerializer(draft).data)
