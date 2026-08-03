"""Reference data (design.md §7)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from categories.models import Category
from categories.serializers import CategorySerializer


@extend_schema(
    summary="List categories",
    parameters=[
        OpenApiParameter("roots_only", bool, description="Return only top-level verticals."),
        OpenApiParameter("parent", int, description="Children of this category id."),
    ],
)
class CategoryListView(ListAPIView[Category]):
    serializer_class = CategorySerializer
    permission_classes: list[Any] = [IsAuthenticated]
    # Reference data, not workspace data: the whole tree is served in one call
    # so the onboarding wizard can render without paging.
    pagination_class = None

    def get_queryset(self) -> QuerySet[Category]:
        queryset = Category.objects.filter(is_active=True)

        if self.request.query_params.get("roots_only") in {"1", "true", "True"}:
            return queryset.filter(parent__isnull=True)

        parent = self.request.query_params.get("parent")
        if parent:
            return queryset.filter(parent__id=int(parent))

        return queryset
