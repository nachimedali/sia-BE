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
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from ai.models import (
    CreativeKind,
    CreativeOption,
    GenerationKind,
    GenerationStatus,
    GenerationVariant,
)
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
    AutopilotReadinessSerializer,
    CompletenessCheckSerializer,
    DraftRejectRequestSerializer,
    GeneratedImageSerializer,
    ProductCleanSerializer,
    ProductCompletenessSerializer,
    ProductIdentitySerializer,
    ProductReferenceImagesUploadSerializer,
    ProductSerializer,
    ProductSummarySerializer,
)

# Aliased: `ProductViewSet.autopilot` is a route name fixed by design.md §7's
# `/products/{id}/autopilot/`, and it would otherwise shadow the module.
from products.services import autopilot as autopilot_service
from products.services import identity
from products.services import readiness as readiness_service
from products.services.completeness import check_definitions, completeness_payload
from products.services.products import (
    attach_reference_images,
    create_product,
    detach_reference_image,
    update_product,
)
from taste.models import CandidateState

AUTOPILOT_FEATURE = "autopilot"

# The products page's "high completeness" filter. A display threshold, not a
# gate: nothing is allowed or refused on it.
HIGH_COMPLETENESS = 80

PRODUCT_STATUS_FILTERS: dict[str, Q] = {
    "ready": Q(is_generation_ready=True),
    "needs_reference": Q(is_generation_ready=False),
    "high_completeness": Q(completeness_score__gte=HIGH_COMPLETENESS),
    "autopilot_on": Q(autopilot__enabled=True),
    "generated": Q(generations__status=GenerationStatus.SUCCEEDED),
    "requires_check": Q(posts__status=PostStatus.PENDING_REVIEW)
    | Q(candidates__state=CandidateState.PENDING_REVIEW),
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
        # Explicit: the counts group the query, and a grouped query drops
        # `Meta.ordering` — the list came back in whatever order Postgres chose.
        .order_by("-created_at", "-id")
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
            OpenApiParameter(
                "platform",
                str,
                enum=Platform.values,
                many=True,
                explode=True,
                description="Repeat for several; a product on any of them matches.",
            ),
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
            # The two joined filters fan out to one row per matching post,
            # candidate or generation; `id__in` keeps a product once.
            queryset = queryset.filter(
                id__in=Product.objects.filter(PRODUCT_STATUS_FILTERS[status]).values("id")
            )

        platforms = [p for p in params.getlist("platform") if p]
        if platforms:
            unknown = [p for p in platforms if p not in Platform.values]
            if unknown:
                raise OCCSError(
                    f"Unknown platform: {unknown[0]}.",
                    code="invalid_platform",
                    detail={"platform": unknown},
                )
            any_of = Q()
            for platform in platforms:
                any_of |= Q(platforms__contains=[platform])
            queryset = queryset.filter(any_of)

        return queryset

    @extend_schema(responses={200: ProductSummarySerializer}, summary="Products per status")
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        """The counts on the products page's status tiles.

        Over the whole catalogue and ignoring any filter: each tile is itself
        a filter, so its number must not change when it is pressed.
        """
        products = Product.objects.filter(workspace=request_workspace(request))
        # One count per tile rather than one aggregate: three filters join
        # posts, candidates and generations, and joined together they multiply.
        counts = {
            key: products.filter(rule).distinct().count()
            for key, rule in PRODUCT_STATUS_FILTERS.items()
            if key != "high_completeness"
        }
        return Response({"all": products.count(), **counts})

    @extend_schema(responses={200: ProductSerializer}, summary="A product by its address ref")
    @action(detail=False, methods=["get"], url_path=r"by-ref/(?P<ref>[0-9a-f]{8})")
    def by_ref(self, request: Request, ref: str) -> Response:
        """`/app/products/<slug>-<ref>` resolves here: the ref alone decides,
        so a renamed product keeps its address working."""
        product = get_object_or_404(self.get_queryset(), ref=ref)
        return Response(self.get_serializer(product).data)

    @extend_schema(
        responses={200: GeneratedImageSerializer(many=True)},
        summary="Images generated for a product",
    )
    @action(detail=True, methods=["get"], url_path="generated-images")
    def generated_images(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        variants = (
            GenerationVariant.objects.filter(
                generation__product=product,
                generation__status=GenerationStatus.SUCCEEDED,
                kind=GenerationKind.IMAGE,
                media_asset__isnull=False,
            )
            .select_related("media_asset")
            .order_by("-generation__created_at", "rank", "id")
        )
        assert self.paginator is not None  # `pagination_class` is set on the view
        page = self.paginator.paginate_queryset(variants, request, view=self)
        return self.get_paginated_response(GeneratedImageSerializer(page, many=True).data)

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
        shot_tags = request.data.getlist("tags") if hasattr(request.data, "getlist") else []
        valid = {
            *CreativeOption.objects.filter(kind=CreativeKind.SHOT_TAG, is_active=True).values_list(
                "key", flat=True
            )
        }
        if any(tag and tag not in valid for tag in shot_tags):
            raise OCCSError("Unknown shot type.", code="invalid_shot_tag")
        attach_reference_images(product=product, uploads=uploads, shot_tags=shot_tags)
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

    @extend_schema(
        request=None,
        responses={200: ProductSerializer},
        summary="Draft the product's identity card from its front photo",
        description="A vision reading of what the product looks like (S17-29), paid in "
        "credits when it returns a card, saved unconfirmed: a draft is never sent to a model.",
    )
    @action(detail=True, methods=["post"], url_path="identity/draft")
    def identity_draft(self, request: Request, pk: str | None = None) -> Response:
        product = identity.draft(self.get_object())
        return Response(self.get_serializer(self.get_queryset().get(pk=product.pk)).data)

    @extend_schema(
        request=ProductIdentitySerializer,
        responses={200: ProductSerializer},
        summary="Correct and confirm the product's identity card",
    )
    @action(detail=True, methods=["patch"], url_path="identity")
    def identity_card(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        data = ProductIdentitySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        card = {key: value for key, value in data.validated_data.items() if key != "confirmed"}
        identity.save(product, card, confirmed=data.validated_data["confirmed"])
        return Response(self.get_serializer(self.get_queryset().get(pk=product.pk)).data)

    @extend_schema(
        request=None,
        responses={202: ProductCleanSerializer},
        summary="Make a clean photo of the product from one of its photos",
        description="A new picture of the product alone, made from that photo only (S17-29); "
        "read `clean/{generation}/` to add it as a front photo once it is ready.",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"reference-images/(?P<asset_id>\d+)/clean",
    )
    def clean_photo(
        self, request: Request, pk: str | None = None, asset_id: str | None = None
    ) -> Response:
        generation = identity.clean(
            self.get_object(), int(asset_id or 0), user=authenticated_user(request)
        )
        return Response(
            ProductCleanSerializer({"generation": generation.pk, "status": "pending"}).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        responses={200: ProductCleanSerializer},
        summary="Where a clean photo stands; a ready one is added once as a front photo",
    )
    @action(detail=True, methods=["get"], url_path=r"clean/(?P<generation_id>\d+)")
    def clean_status(
        self, request: Request, pk: str | None = None, generation_id: str | None = None
    ) -> Response:
        state = identity.clean_result(self.get_object(), int(generation_id or 0))
        return Response(
            ProductCleanSerializer({"generation": int(generation_id or 0), "status": state}).data
        )

    @extend_schema(
        responses={200: CompletenessCheckSerializer(many=True)},
        summary="The completeness checks, as data",
        description="Weights and required flags the new-product form's panel reads, so the "
        "browser carries no second copy of them.",
    )
    @action(detail=False, methods=["get"], url_path="completeness-checks")
    def completeness_checks(self, request: Request) -> Response:
        return Response(check_definitions())

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


class AutopilotReadinessView(APIView):
    """What is still missing before autopilot can draft (X-08).

    **Deliberately not `HasFeature("autopilot")`.** Every other autopilot view
    carries the gate, because there is nothing to say to a workspace that has
    not bought the feature. This one is the exception that makes the others
    usable: a 402 here would answer "which six things do I need?" with "pay",
    and the plan is the first row of the answer rather than the door to it.
    """

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: AutopilotReadinessSerializer},
        summary="Autopilot setup state: what is done, what is missing",
    )
    def get(self, request: Request) -> Response:
        payload = readiness_service.autopilot_readiness(request_workspace(request))
        return Response(AutopilotReadinessSerializer(payload).data)


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
