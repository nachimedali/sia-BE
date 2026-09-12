"""Content endpoints (design.md §7).

Views parse and serialise; `content.services` decides (implementation.md §4.1).
`PostViewSet` and `MediaAssetViewSet` are the first two entries on the router
in `config/api_urls.py`, which is what the Phase 4 tenancy sweep
(`test_cross_workspace_access_returns_404_on_every_viewset`) walks (design.md
A52).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from billing.permissions import HasFlag
from billing.services.flags import CONTENT_MODEL_V2
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.editing.base import CropBox
from content.models import (
    ContentKind,
    MediaAsset,
    Platform,
    Post,
    PostMediaAttachment,
    PostStatus,
    PostTemplate,
    RecurrenceRule,
)
from content.serializers import (
    AltTextRequestSerializer,
    CropRequestSerializer,
    MediaAssetSerializer,
    MediaAssetUploadSerializer,
    PlatformOptionsRequestSerializer,
    PlatformRuleListSerializer,
    PostPreviewRequestSerializer,
    PostPreviewResponseSerializer,
    PostRevisionSerializer,
    PostScheduleRequestSerializer,
    PostSerializer,
    PostSubmitRequestSerializer,
    PostTemplateSerializer,
    RecurrenceRuleSerializer,
    TrimRequestSerializer,
)
from content.services import revisions as revisions_service
from content.services.adaptation import render_payloads
from content.services.editing import crop_image, trim_video
from content.services.media import ingest_media
from content.services.posts import (
    create_post,
    set_alt_text,
    set_platform_options,
    target_for_platform,
    update_post,
)
from content.services.rules import PLATFORM_RULES
from content.services.templates import apply_template
from scheduling.services import schedule_post
from workspaces.models import Permission
from workspaces.permissions import HasPermission
from workspaces.serializers import (
    ApprovalActionSerializer,
    ApprovalNoteRequestSerializer,
)
from workspaces.services import approvals

#: **No plan gate on the approval endpoints any more** (C-02, P2-04). Approval
#: is required on every plan, so a `HasFeature` in front of `submit`/`approve`
#: would make the required step unreachable on the plans that need it most.
#: What Advanced still buys is chain *depth* (P2-13), enforced where stages are
#: configured — `workspaces.views.ApprovalChainStageView`.

# `Post.ordered_attachments()` reads `media_attachments`, not the
# `media_assets` M2M manager directly (content/models.py) — the Prefetch has to
# target the same accessor, or it fetches rows nothing ever reads and every
# post in a list response re-queries its media anyway.
#
# Deliberately **unfiltered**: the per-target alt-text override rows (P1-06)
# live in this table too, and `render_post` resolves them off this same
# evaluated queryset. Filtering them out here would send `render_post` back to
# the database once per post to find them again.
_ORDERED_MEDIA_ATTACHMENTS = Prefetch(
    "media_attachments",
    queryset=PostMediaAttachment.objects.select_related("media_asset").order_by("order", "id"),
)


class PostViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[Post]):
    serializer_class = PostSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = Post.objects.select_related("category", "origin_post", "author").prefetch_related(
        _ORDERED_MEDIA_ATTACHMENTS
    )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status",
                str,
                description="Repeatable. Restricts the list to these statuses; an unknown "
                "value is a 400 rather than a silently empty page.",
                many=True,
                enum=PostStatus.values,
            )
        ]
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> Any:
        """`?status=` exists for the review queue, which is a *subset* of the
        workspace's posts and has to be the whole subset: `max_page_size` is
        100, so filtering a page client-side would quietly drop the 101st post
        awaiting review. An unrecognised value is rejected rather than ignored,
        because a typo that returns everything is worse than one that 400s.
        """
        queryset = super().get_queryset()
        queryset = self._filter_by_content_kind(queryset)
        statuses = self.request.query_params.getlist("status")
        if not statuses:
            return queryset

        unknown = sorted(set(statuses) - set(PostStatus.values))
        if unknown:
            raise OCCSError(
                f"Unknown post status: {', '.join(unknown)}.",
                code="invalid_status",
                detail={"status": unknown},
            )
        return queryset.filter(status__in=statuses)

    def _filter_by_content_kind(self, queryset: Any) -> Any:
        """`?content_kind=` — the calendar and the list both show documents and
        social posts together, so narrowing to one is a filter rather than a
        separate endpoint. That is the whole reason `DOC` is a subtype and not a
        sibling model (P3-01).
        """
        kinds = self.request.query_params.getlist("content_kind")
        if not kinds:
            return queryset
        unknown = sorted(set(kinds) - set(ContentKind.values))
        if unknown:
            raise OCCSError(
                f"Unknown content kind: {', '.join(unknown)}.",
                code="invalid_content_kind",
                detail={"content_kind": unknown},
            )
        return queryset.filter(content_kind__in=kinds)

    def perform_create(self, serializer: BaseSerializer[Post]) -> None:
        assert isinstance(serializer, PostSerializer)  # always this view's own serializer_class
        data = serializer.validated_data
        serializer.instance = create_post(
            workspace=request_workspace(self.request),
            author=authenticated_user(self.request),
            master_body=data.get("master_body", ""),
            category=data.get("category"),
            media_assets=data.get("media_asset_ids", []),
            content_kind=data.get("content_kind", ContentKind.SOCIAL),
            doc_body=data.get("doc_body"),
        )

    def perform_update(self, serializer: BaseSerializer[Post]) -> None:
        assert isinstance(serializer, PostSerializer)  # always this view's own serializer_class
        assert serializer.instance is not None  # set by UpdateModelMixin.get_object() beforehand
        serializer.instance = update_post(
            serializer.instance,
            author=authenticated_user(self.request),
            **serializer.validated_data,
        )

    @extend_schema(
        request=PostPreviewRequestSerializer,
        responses={200: PostPreviewResponseSerializer},
        summary="Preview per-platform adaptation",
        description=(
            "Adapts a master body and media into what each requested platform "
            "would receive, without persisting anything. The publish path "
            "(Phase 9) calls the same Adaptation Engine function on the saved "
            "post, so this is provably what would be sent (design.md §8.6)."
        ),
    )
    @action(detail=False, methods=["post"])
    def preview(self, request: Request) -> Response:
        payload = PostPreviewRequestSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        workspace = request_workspace(request)
        platforms = data.get("platforms") or [
            p for p in workspace.platforms if p in Platform.values
        ]
        if not platforms:
            raise OCCSError(
                "No target platforms to preview. Pick platforms in onboarding, "
                "or pass `platforms` explicitly.",
                code="no_target_platforms",
            )

        payloads = render_payloads(
            master_body=data.get("master_body", ""),
            media_assets=list(data.get("media_asset_ids", [])),
            platforms=platforms,
            alt_text={int(k): v for k, v in data.get("alt_text", {}).items()},
            options=data.get("platform_options") or {},
            workspace=workspace,
        )
        return Response({"payloads": {p: payload.as_dict() for p, payload in payloads.items()}})

    @extend_schema(
        request=AltTextRequestSerializer,
        responses={200: PostSerializer},
        summary="Describe one of this post's images",
        description=(
            "Alt text is a property of *this use* of the file, not of the file "
            "(P1-06) — `MediaAsset` is immutable and carries no descriptive "
            "text. Omit `platform` to describe the image for the whole post; "
            "pass one to describe it for that platform only, which is how "
            "Instagram and LinkedIn end up describing the same image to "
            "different audiences. Clearing a per-platform description falls "
            "back to the post-level one."
        ),
    )
    @action(detail=True, methods=["post"], url_path="alt-text")
    def alt_text(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        payload = AltTextRequestSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        platform = data.get("platform")
        set_alt_text(
            post,
            media_asset=data["media_asset"],
            alt_text=data["alt_text"],
            target=target_for_platform(post, platform) if platform else None,
            author=authenticated_user(request),
        )
        post.refresh_from_db()
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=PlatformOptionsRequestSerializer,
        responses={200: PostSerializer},
        summary="Set one platform's composer settings",
        description=(
            "First comment, location, tagging, audience targeting, custom "
            "thumbnail — whatever `content.services.rules` declares for that "
            "platform (P1-11). Validated against the declaration, so an "
            "undeclared key is a 400 naming the field rather than a setting "
            "that silently does nothing. Creates the target if the post has "
            "none yet; it never sets a schedule or an account."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="platform-options",
        permission_classes=[
            IsAuthenticated,
            HasFlag(CONTENT_MODEL_V2),
            HasPermission(Permission.EDIT),
        ],
    )
    def platform_options(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        payload = PlatformOptionsRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        set_platform_options(
            post,
            platform=payload.validated_data["platform"],
            options=payload.validated_data.get("options", {}),
            author=authenticated_user(request),
        )
        post.refresh_from_db()
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        responses={200: PostRevisionSerializer(many=True)},
        summary="This post's version history, newest first",
        description=(
            "Append-only. Each entry carries the diff against the one before "
            "it; every tenth is a full checkpoint, which is what bounds both "
            "reconstruction and what retention is allowed to drop (P1-08)."
        ),
    )
    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated, HasFlag(CONTENT_MODEL_V2)],
    )
    def revisions(self, request: Request, pk: str | None = None) -> Response:
        history = self.get_object().revisions.select_related("author").order_by("-sequence")
        return Response(PostRevisionSerializer(history, many=True).data)

    @extend_schema(
        request=None,
        responses={200: PostSerializer},
        summary="Put an earlier version back",
        description=(
            "Writes a **new** revision whose content matches the old one; it "
            "never rewinds or removes history. 404 for a sequence this post "
            "has no revision for, 409 for a post that is already publishing "
            "or published."
        ),
        parameters=[OpenApiParameter("sequence", int, OpenApiParameter.PATH)],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"revisions/(?P<sequence>[0-9]+)/restore",
        permission_classes=[
            IsAuthenticated,
            HasFlag(CONTENT_MODEL_V2),
            HasPermission(Permission.EDIT),
        ],
    )
    def restore_revision(
        self, request: Request, pk: str | None = None, sequence: str | None = None
    ) -> Response:
        post = revisions_service.restore(
            self.get_object(), sequence=int(sequence or 0), author=authenticated_user(request)
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=PostScheduleRequestSerializer,
        responses={200: PostSerializer},
        summary="Schedule a post for reminder or auto-publish delivery",
        description=(
            "Validates `scheduled_at` against the workspace's "
            "`Plan.scheduling_horizon_days` (402 past the horizon, D13/I8) "
            "and, for `delivery_mode=REMINDER`, arms the Reminder that Beat "
            "sends on time (implementation.md Phase 8).\n\n"
            "On a workspace whose approval chain does not block, **scheduling "
            "is the approval** (L-2): an `APPROVE` action is recorded naming "
            "the caller. Where the chain does block, a post that is not yet "
            "`APPROVED` is a 409."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasPermission(Permission.PUBLISH)],
    )
    def schedule(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        payload = PostScheduleRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        post = schedule_post(
            post=post,
            delivery_mode=data["delivery_mode"],
            scheduled_at=data["scheduled_at"],
            actor=authenticated_user(request),
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=PostSubmitRequestSerializer,
        responses={200: PostSerializer},
        summary="Submit a draft for review",
        description=(
            "DRAFT or CHANGES_REQUESTED → PENDING_REVIEW, parked at the first "
            "stage of the workspace's chain. Needs `edit`; an illegal "
            "transition is 409, not a silent no-op.\n\n"
            "`delivery_mode` and `scheduled_at` are the author's **proposal** "
            "(P2-10): when the last stage clears, the post is scheduled at that "
            "time through the ordinary schedule service, so nobody has to come "
            "back and press a second button. Omit them and approval simply "
            "leaves the post `APPROVED`."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasPermission(Permission.EDIT)],
    )
    def submit(self, request: Request, pk: str | None = None) -> Response:
        payload = PostSubmitRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        post = approvals.submit_for_review(
            self.get_object(),
            actor=authenticated_user(request),
            note=data.get("note", ""),
            delivery_mode=data.get("delivery_mode", ""),
            scheduled_at=data.get("scheduled_at"),
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Clear this post's current approval stage",
        description=(
            "Needs `approve`. Clearing the **last** stage reaches `APPROVED`, "
            "locks the post and, if the author proposed a time at submit, "
            "schedules it. Clearing an earlier one advances to the next stage "
            "and the post stays `PENDING_REVIEW` — the chain's shape lives in "
            "the stage rows, never in the status enum.\n\n"
            "Pass `stage` to say which stage you believe the post is at: a "
            "stale value is a **409**, not an approval of whatever stage it has "
            "since moved to."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasPermission(Permission.APPROVE)],
    )
    def approve(self, request: Request, pk: str | None = None) -> Response:
        payload = self._approval_payload(request)
        post = approvals.approve(
            self.get_object(),
            actor=authenticated_user(request),
            note=payload["note"],
            stage=payload.get("stage"),
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Send a post back with changes requested",
        description="PENDING_REVIEW → CHANGES_REQUESTED. ADMIN+ only; `note` is required "
        "— it is the only thing the author has to act on.",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="request-changes",
        permission_classes=[
            IsAuthenticated,
            HasPermission(Permission.APPROVE),
        ],
    )
    def request_changes(self, request: Request, pk: str | None = None) -> Response:
        note = self._approval_payload(request)["note"]
        if not note:
            raise OCCSError("A note is required when requesting changes.", code="note_required")
        post = approvals.request_changes(
            self.get_object(), actor=authenticated_user(request), note=note
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Reject a post under review",
        description="PENDING_REVIEW → REJECTED. ADMIN+ only.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            IsAuthenticated,
            HasPermission(Permission.APPROVE),
        ],
    )
    def reject(self, request: Request, pk: str | None = None) -> Response:
        note = self._approval_payload(request)["note"]
        post = approvals.reject(self.get_object(), actor=authenticated_user(request), note=note)
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=None,
        responses={200: PostSerializer},
        summary="Reopen an approved post for editing",
        description=(
            'An approved post is locked, so that "approved" always describes '
            "the content that was approved (P2-11). Unlocking needs `admin` and "
            "writes an audit entry; the post keeps its `APPROVED` status until "
            "the next content edit voids it."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasPermission(Permission.ADMIN)],
    )
    def unlock(self, request: Request, pk: str | None = None) -> Response:
        post = approvals.unlock(self.get_object(), actor=authenticated_user(request))
        return Response(PostSerializer(post, context={"request": request}).data)

    @staticmethod
    def _approval_payload(request: Request) -> dict[str, Any]:
        payload = ApprovalNoteRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return dict(payload.validated_data)

    @extend_schema(
        responses={200: ApprovalActionSerializer(many=True)},
        summary="This post's approval history, oldest first",
        description="The append-only `ApprovalAction` rows behind the post's current status. "
        "Scoped to one post rather than read from `GET /workspaces/audit-log/`, which is "
        "workspace-wide and paginated — a reviewer wants this post's trail, not a page of "
        "everyone's.",
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="approvals",
        # Not named `approvals`: this module imports the `approvals` *service*,
        # and a method of that name reads as a shadow of it to anyone skimming
        # the class, even though Python resolves the two in different scopes.
        permission_classes=[IsAuthenticated],
    )
    def approval_history(self, request: Request, pk: str | None = None) -> Response:
        trail = self.get_object().approval_actions.select_related("actor").order_by("created_at")
        return Response(ApprovalActionSerializer(trail, many=True).data)


class MediaAssetViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet[MediaAsset],
):
    """Upload, list, retrieve, delete. No update: a media asset's bytes and
    the metadata sniffed from them are immutable once ingested — a changed
    file is a new upload, not an edit."""

    serializer_class = MediaAssetSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = MediaAsset.objects.all()

    @extend_schema(request=MediaAssetUploadSerializer, responses={201: MediaAssetSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise OCCSError("No file was uploaded.", code="missing_file")

        asset = ingest_media(workspace=request_workspace(request), upload=upload)
        serializer = self.get_serializer(asset)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=CropRequestSerializer,
        responses={201: MediaAssetSerializer},
        summary="Crop an image into a new asset",
        description=(
            "201, not 200: a crop **creates** a new `MediaAsset` carrying "
            "`derived_from` and leaves the original exactly as it was (P1-12). "
            "The editor is a new ingestion path, not a mutation path — which "
            'is what keeps "which file did we publish" answerable later.'
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasFlag(CONTENT_MODEL_V2)],
    )
    def crop(self, request: Request, pk: str | None = None) -> Response:
        payload = CropRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        derived = crop_image(self.get_object(), box=CropBox(**payload.validated_data))
        return Response(MediaAssetSerializer(derived).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=TrimRequestSerializer,
        responses={201: MediaAssetSerializer},
        summary="Trim a video into a new asset",
        description=(
            "Same shape as `crop`. 422 when no video editor is configured for "
            "this deployment — trimming needs a codec, and a fresh checkout "
            "runs without one (Part 7 rule 6)."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasFlag(CONTENT_MODEL_V2)],
    )
    def trim(self, request: Request, pk: str | None = None) -> Response:
        payload = TrimRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        derived = trim_video(self.get_object(), **payload.validated_data)
        return Response(MediaAssetSerializer(derived).data, status=status.HTTP_201_CREATED)


class PostTemplateViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[PostTemplate]):
    """Saved starting points (P1-09). Applying **copies** into a new post; a
    template edit never reaches posts already made from it."""

    serializer_class = PostTemplateSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasFlag(CONTENT_MODEL_V2)]
    pagination_class = DefaultPagination
    queryset = PostTemplate.objects.select_related("created_by")

    def perform_create(self, serializer: BaseSerializer[PostTemplate]) -> None:
        serializer.save(
            workspace=request_workspace(self.request),
            created_by=authenticated_user(self.request),
        )

    @extend_schema(
        request=None,
        responses={201: PostSerializer},
        summary="Create a post from this template",
    )
    @action(detail=True, methods=["post"])
    def apply(self, request: Request, pk: str | None = None) -> Response:
        post = apply_template(self.get_object(), author=authenticated_user(request))
        return Response(
            PostSerializer(post, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class RecurrenceRuleViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[RecurrenceRule]):
    """ "Every Monday at 09:00" (P1-10). The scan materialises drafts; it never
    schedules — `POST /posts/{id}/schedule/` stays the sole writer of
    `delivery_mode` and `scheduled_at`."""

    serializer_class = RecurrenceRuleSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasFlag(CONTENT_MODEL_V2)]
    pagination_class = DefaultPagination
    queryset = RecurrenceRule.objects.select_related("source")
    #: The rule hangs off a template, which is what carries the workspace.
    workspace_field = "source__workspace"


class PlatformRuleListView(APIView):
    """The per-platform composer declarations, as data (P1-05, P1-11).

    Served rather than mirrored in the frontend on purpose. A TypeScript copy
    of `rules.py` is a second declaration, and two declarations of the same
    facts drift — which is exactly the failure P4-06's hard stop is written to
    catch, arriving through the back door. Adding X, Pinterest and Google
    Business Profile should change one table and no client code.

    Public reference data, like `GET /categories/`: a platform's caption limit
    is a fact about that platform, not about the caller's tenant, so there is
    nothing here for the tenancy sweep to walk.
    """

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: PlatformRuleListSerializer},
        summary="Per-platform limits and composer fields",
    )
    def get(self, request: Request) -> Response:
        return Response(
            {
                "platforms": [
                    {
                        "platform": platform,
                        "char_limit": rule.char_limit,
                        "supports_thread": rule.supports_thread,
                        # Per `(platform, format)` since P4-05 — the composer
                        # needs the cap for the format the user actually picked,
                        # and a platform-level number would be the loosest of
                        # them, which is the one value that is never right.
                        "formats": [
                            {
                                "format": name,
                                "max_media": spec.max_media,
                                "min_media": spec.min_media,
                                "allowed_media_kinds": sorted(spec.allowed_media_kinds),
                                "char_limit": spec.char_limit or rule.char_limit,
                            }
                            for name, spec in sorted(rule.formats.items())
                        ],
                        "options": [
                            {
                                "key": option.key,
                                "kind": option.kind,
                                "label": option.label,
                                "choices": list(option.choices),
                                "source": option.source,
                                "max_length": option.max_length,
                                "default": option.default,
                                "required": option.required,
                            }
                            for option in rule.options
                        ],
                    }
                    for platform, rule in PLATFORM_RULES.items()
                ]
            }
        )
