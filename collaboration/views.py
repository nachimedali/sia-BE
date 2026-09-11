"""Collaboration endpoints (P2-01, P2-03, P2-09).

Two shapes in one module, the same split `reminders/views.py` already makes:
the authenticated, workspace-scoped surface at the top, and the public
token-scoped review surface at the bottom.

`ThreadViewSet` is the only registered ViewSet here, so the tenancy sweep
(`test_cross_workspace_access_returns_404_on_every_viewset`) walks it like any
other. It stacks **two** scopes: workspace, then audience. Order matters only
for readability — both are `WHERE` clauses on the same query — but the pair is
what makes P2-G1 provable at queryset level rather than by reviewing views.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from collaboration import review, services
from collaboration.models import Comment, Reaction, ReviewLink, ReviewLinkPurpose, Thread
from collaboration.serializers import (
    CommentCreateSerializer,
    CommentSerializer,
    GuestCommentRequestSerializer,
    ReactionRequestSerializer,
    ReviewLinkSerializer,
    ReviewPacketSerializer,
    ShareRequestSerializer,
    ThreadAssignRequestSerializer,
    ThreadCreateSerializer,
    ThreadSerializer,
    ThreadStatusRequestSerializer,
)
from common.exceptions import NotFoundError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.throttling import IPTokenBucketThrottle
from common.visibility import (
    Audience,
    VisibilityScopedQuerySetMixin,
    request_audience,
    visible_values,
)
from common.workspaces import authenticated_user, request_workspace
from content.models import Post
from workspaces.models import Permission
from workspaces.permissions import HasPermission
from workspaces.services import approvals


class ThreadViewSet(
    WorkspaceScopedQuerySetMixin,
    VisibilityScopedQuerySetMixin,
    viewsets.ModelViewSet[Thread],
):
    """Internal discussion on a post — the Jira-ticket surface (C-03).

    **Not gated by plan.** Collaboration is how the product gets used at all,
    so it is available on every plan (L-3); what Advanced sells is approval
    *chain depth* (P2-13), not the ability to talk about a draft.
    """

    serializer_class = ThreadSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = Thread.objects.select_related("post", "assignee", "opened_by", "resolved_by")

    def get_queryset(self) -> Any:
        """`comment_count` counts only what this caller may load.

        An unfiltered `Count` would tell a client "5 comments" and then hand
        them two — which both looks broken and quietly reports how many
        internal remarks exist. A number is a smaller leak than a body, and it
        is still a leak.

        The explicit `order_by` restores `Meta.ordering`, which Django drops
        once an aggregate puts the query into a GROUP BY.
        """
        queryset = (
            super()
            .get_queryset()
            .annotate(
                comment_count=Count(
                    "comments",
                    filter=Q(
                        comments__visibility__in=visible_values(request_audience(self.request))
                    ),
                )
            )
            .order_by("-created_at")
        )
        post = self.request.query_params.get("post")
        if post:
            queryset = queryset.filter(post_id=post)
        thread_status = self.request.query_params.getlist("status")
        if thread_status:
            queryset = queryset.filter(status__in=thread_status)
        return queryset

    def get_serializer_class(self) -> Any:
        return ThreadCreateSerializer if self.action == "create" else ThreadSerializer

    def get_permissions(self) -> Any:
        """Reading needs `view`; saying anything needs `comment`.

        Declared here rather than as four `permission_classes` on four actions
        because the split is one rule — write versus read — and spelling it out
        per action is how one of them later gets forgotten.
        """
        if self.action in {"list", "retrieve"}:
            return [
                permission() for permission in (IsAuthenticated, HasPermission(Permission.VIEW))
            ]
        return [permission() for permission in (IsAuthenticated, HasPermission(Permission.COMMENT))]

    @extend_schema(
        parameters=[
            OpenApiParameter("post", int, description="Only threads on this post."),
            OpenApiParameter(
                "status",
                str,
                description="Repeatable. OPEN / LATER / DONE.",
                many=True,
            ),
        ]
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(request=ThreadCreateSerializer, responses={201: ThreadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = ThreadCreateSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        anchor = (data["anchor_start"], data["anchor_end"]) if "anchor_start" in data else None
        thread = services.open_thread(
            data["post"],
            author=authenticated_user(request),
            title=data["title"],
            body=data["body"],
            visibility=data["visibility"],
            assignee=data.get("assignee"),
            anchor=anchor,
        )
        return Response(
            ThreadSerializer(self.get_queryset().get(pk=thread.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=CommentCreateSerializer,
        responses={200: CommentSerializer(many=True), 201: CommentSerializer},
        summary="Read or add comments in this thread",
        description=(
            "Pass `anchor_start`/`anchor_end` to pin the comment to a range of "
            "the post's body. If a later edit changes that text the annotation "
            "is marked orphaned and shown detached — it is never moved onto "
            "whatever now occupies the range (P2-02)."
        ),
    )
    @action(detail=True, methods=["get", "post"])
    def comments(self, request: Request, pk: str | None = None) -> Response:
        thread = self.get_object()
        audience = request_audience(request)

        if request.method == "POST":
            payload = CommentCreateSerializer(
                data=request.data, context={"request": request, "thread": thread}
            )
            payload.is_valid(raise_exception=True)
            data = payload.validated_data
            comment = services.add_comment(
                thread,
                author=authenticated_user(request),
                body=data["body"],
                parent=data.get("parent"),
                visibility=data.get("visibility", thread.visibility),
                anchor=(
                    (data["anchor_start"], data["anchor_end"]) if "anchor_start" in data else None
                ),
            )
            return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        rows = (
            thread.comments.filter(visibility__in=visible_values(audience))
            .select_related("author", "annotation")
            .prefetch_related(
                Prefetch("reactions", queryset=Reaction.objects.select_related("user"))
            )
        )
        return Response(CommentSerializer(rows, many=True).data)

    @extend_schema(
        request=ThreadStatusRequestSerializer,
        responses={200: ThreadSerializer},
        summary="Move a thread to OPEN, LATER or DONE",
    )
    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request: Request, pk: str | None = None) -> Response:
        payload = ThreadStatusRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        thread = services.set_status(
            self.get_object(),
            status=payload.validated_data["status"],
            actor=authenticated_user(request),
        )
        return Response(ThreadSerializer(self.get_queryset().get(pk=thread.pk)).data)

    @extend_schema(
        request=ThreadAssignRequestSerializer,
        responses={200: ThreadSerializer},
        summary="Assign a thread, or clear its assignee",
    )
    @action(detail=True, methods=["post"])
    def assign(self, request: Request, pk: str | None = None) -> Response:
        payload = ThreadAssignRequestSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        thread = services.assign(
            self.get_object(),
            assignee=payload.validated_data["assignee"],
            actor=authenticated_user(request),
        )
        return Response(ThreadSerializer(self.get_queryset().get(pk=thread.pk)).data)

    @extend_schema(
        request=ReactionRequestSerializer,
        responses={200: CommentSerializer},
        summary="Add or remove an emoji reaction",
        description="POST adds, DELETE removes. Both are idempotent.",
        parameters=[OpenApiParameter("comment_pk", int, OpenApiParameter.PATH)],
    )
    @action(
        detail=True,
        methods=["post", "delete"],
        url_path=r"comments/(?P<comment_pk>[0-9]+)/reactions",
    )
    def reactions(
        self, request: Request, pk: str | None = None, comment_pk: str | None = None
    ) -> Response:
        thread = self.get_object()
        audience = request_audience(request)
        comment = get_object_or_404(
            Comment.objects.filter(visibility__in=visible_values(audience)),
            pk=comment_pk,
            thread=thread,
        )
        payload = ReactionRequestSerializer(
            data=request.data if request.method == "POST" else request.query_params
        )
        payload.is_valid(raise_exception=True)

        user = authenticated_user(request)
        emoji = payload.validated_data["emoji"]
        if request.method == "POST":
            services.react(comment, user=user, emoji=emoji)
        else:
            services.unreact(comment, user=user, emoji=emoji)

        comment.refresh_from_db()
        return Response(CommentSerializer(comment).data)


# -----------------------------------------------------------------------------
# Token-scoped review (P2-09)
#
# Keyed by the token rather than a workspace-scoped pk, reachable from
# `/a/{token}` and `/g/{token}` with no login, and **deliberately not on
# `router`** (A52): a token is not a pk, so the cross-workspace sweep has
# nothing to walk here. `collaboration.review.resolve` is the access control,
# and the audience narrowing on top of it is what P2-G1 checks.
#
# Every view below sets `request.audience = Audience.CLIENT` before touching a
# queryset. This is the only place in the codebase that does, which is what
# makes `common.visibility.request_audience`'s default of STAFF safe.
# -----------------------------------------------------------------------------
class ReviewTokenThrottle(IPTokenBucketThrottle):
    """Generous, like the reminder one. A client reloading a review page on a
    shaky connection must never see this; it exists only as defence in depth
    against naive enumeration, which a 256-bit token already makes
    infeasible."""

    scope = "collaboration:review"
    capacity = 60
    refill_per_second = 1


def _resolve_or_404(token: str) -> ReviewLink:
    link = review.resolve(token)
    if link is None:
        raise NotFound("This link is invalid, expired or has been revoked.")
    return link


class _PublicReviewView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [ReviewTokenThrottle]

    def resolve(self, request: Request, token: str) -> ReviewLink:
        """Resolves the link **and declares the audience**.

        Both in one call, because they are one decision: everything reachable
        from here is reachable by someone with no account, and a view that
        resolved the token without narrowing the audience would be a view that
        loads internal threads.
        """
        request.audience = Audience.CLIENT  # type: ignore[attr-defined]
        return _resolve_or_404(token)


class ReviewPacketView(_PublicReviewView):
    @extend_schema(
        responses={200: ReviewPacketSerializer},
        summary="Fetch a shared post's review packet",
        description=(
            "No login — the token is the credential. Returns the post as "
            "`render_post` would publish it, plus only the threads and comments "
            "marked shared. Internal discussion is filtered in the queryset, "
            "never in this serializer (P2-03)."
        ),
        auth=[],
    )
    def get(self, request: Request, token: str) -> Response:
        link = self.resolve(request, token)
        return Response(ReviewPacketSerializer(review.guest_context(link)).data)


class ReviewApproveView(_PublicReviewView):
    @extend_schema(
        request=None,
        responses={200: ReviewPacketSerializer},
        summary="Approve a post from an emailed link",
        description=(
            "`APPROVE` links only, and **single-use**: the token is spent by "
            "the approval, so the same link cannot approve twice. A "
            "`GUEST_VIEW` link is a 404 here rather than a 403 — it must not "
            "learn that an approval endpoint exists for this post."
        ),
        auth=[],
    )
    def post(self, request: Request, token: str) -> Response:
        link = self.resolve(request, token)
        if link.purpose != ReviewLinkPurpose.APPROVE:
            raise NotFound("This link is invalid, expired or has been revoked.")

        approvals.approve_as_guest(link.post, link=link)
        review.spend(link)
        link.refresh_from_db()
        link.post.refresh_from_db()
        return Response(ReviewPacketSerializer(review.guest_context(link)).data)


class ReviewCommentView(_PublicReviewView):
    @extend_schema(
        request=GuestCommentRequestSerializer,
        responses={201: ReviewPacketSerializer},
        summary="Leave a comment as a guest reviewer",
        description=(
            "Lands in the same thread the team is working in, marked shared. A "
            "guest can neither author something the client surface would hide "
            "from them nor see the internal asides around it."
        ),
        auth=[],
    )
    def post(self, request: Request, token: str) -> Response:
        link = self.resolve(request, token)
        payload = GuestCommentRequestSerializer(data=request.data, context={"link": link})
        payload.is_valid(raise_exception=True)

        review.guest_comment(
            link,
            body=payload.validated_data["body"],
            thread=payload.validated_data.get("thread"),
        )
        return Response(
            ReviewPacketSerializer(review.guest_context(link)).data,
            status=status.HTTP_201_CREATED,
        )


class _WorkspacePostView(APIView):
    """Resolves `{pk}` through a workspace-filtered queryset.

    Shared by the two share endpoints rather than one subclassing the other:
    they take different URL arguments, so an inheritance chain would give them
    incompatible `post()` signatures — a shape that type-checks only by
    pretending the difference is not there.
    """

    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.EDIT)]

    def post_or_404(self, request: Request, pk: int) -> Post:
        """404, never 403, for another tenant's id (Part 7 rule 3). The same
        guarantee the shared mixin gives, applied where the sweep does not
        reach."""
        post = Post.objects.filter(workspace=request_workspace(request), pk=pk).first()
        if post is None:
            raise NotFoundError("No such post.", detail={"post": pk})
        return post


class ReviewLinkView(_WorkspacePostView):
    """Issue, list and revoke this post's review links.

    **Revoke is not optional machinery.** A `GUEST_VIEW` link is multi-use, so
    it cannot expire by being consumed — without an explicit revoke there is no
    way to answer "stop that person seeing this" before the thirtieth day.
    """

    @extend_schema(
        responses={200: ReviewLinkSerializer(many=True)},
        summary="Who this post has been shared with",
    )
    def get(self, request: Request, pk: int) -> Response:
        links = self.post_or_404(request, pk).review_links.all()
        return Response(ReviewLinkSerializer(links, many=True).data)

    @extend_schema(
        request=ShareRequestSerializer,
        responses={201: ReviewLinkSerializer},
        summary="Send this post to a reviewer with no account",
        description=(
            "Marks the post shared and emails a link. The raw token is never "
            "returned — it exists once, in the email, so a leak of this "
            "response cannot be replayed."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        post = self.post_or_404(request, pk)
        payload = ShareRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        link, _raw = review.issue(
            post,
            purpose=data["purpose"],
            email=data["email"],
            display_name=data.get("display_name", ""),
            created_by=authenticated_user(request),
        )
        return Response(ReviewLinkSerializer(link).data, status=status.HTTP_201_CREATED)


class ReviewLinkRevokeView(_WorkspacePostView):
    @extend_schema(
        request=None,
        responses={200: ReviewLinkSerializer},
        summary="Revoke a review link",
        description="Idempotent — two people revoking at once is a race, not a conflict.",
    )
    def post(self, request: Request, pk: int, link_id: int) -> Response:
        post = self.post_or_404(request, pk)
        link = post.review_links.filter(pk=link_id).first()
        if link is None:
            raise NotFoundError("No such review link.", detail={"link": link_id})
        return Response(ReviewLinkSerializer(review.revoke(link)).data)
