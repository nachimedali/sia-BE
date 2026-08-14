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

from drf_spectacular.utils import extend_schema
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
from common.workspaces import active_workspace
from products.models import AutopilotConfig, AutopilotDraft, AutopilotDraftStatus, Product
from products.serializers import (
    AutopilotConfigSerializer,
    AutopilotDraftSerializer,
    ProductCompletenessSerializer,
    ProductReferenceImagesUploadSerializer,
    ProductSerializer,
)

# Aliased: `ProductViewSet.autopilot` is a route name fixed by design.md §7's
# `/products/{id}/autopilot/`, and it would otherwise shadow the module.
from products.services import autopilot as autopilot_service
from products.services.completeness import completeness_payload
from products.services.products import attach_reference_images, create_product, update_product

AUTOPILOT_FEATURE = "autopilot"


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
    queryset = Product.objects.select_related("category").prefetch_related("reference_images")

    def perform_create(self, serializer: BaseSerializer[Product]) -> None:
        assert isinstance(serializer, ProductSerializer)  # always this view's own serializer_class
        data = serializer.validated_data
        serializer.instance = create_product(workspace=active_workspace(self.request), **data)

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
            AutopilotDraft.objects.filter(product__workspace=active_workspace(request)), pk=pk
        )


class AutopilotQueueView(_AutopilotView):
    @extend_schema(
        responses={200: AutopilotDraftSerializer(many=True)},
        summary="Drafts waiting for review, soonest slot first",
    )
    def get(self, request: Request) -> Response:
        drafts = (
            AutopilotDraft.objects.filter(
                product__workspace=active_workspace(request),
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
        draft = autopilot_service.approve_draft(self.draft(request, pk))
        return Response(AutopilotDraftSerializer(draft).data)


class AutopilotRejectView(_AutopilotView):
    @extend_schema(
        request=None,
        responses={200: AutopilotDraftSerializer},
        summary="Reject a draft, retiring its slot",
    )
    def post(self, request: Request, pk: int) -> Response:
        draft = autopilot_service.reject_draft(self.draft(request, pk))
        return Response(AutopilotDraftSerializer(draft).data)
