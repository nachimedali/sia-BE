"""Product endpoints (design.md §7). No destroy — the endpoint list is
`GET/POST /products/` and `GET/PATCH /products/{id}/` only; deleting a
product that later phases' posts/generations point at is not a Phase 5
decision to make.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace
from products.models import Product
from products.serializers import (
    ProductCompletenessSerializer,
    ProductReferenceImagesUploadSerializer,
    ProductSerializer,
)
from products.services.completeness import completeness_payload
from products.services.products import attach_reference_images, create_product, update_product


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
