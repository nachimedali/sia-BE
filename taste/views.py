"""Taste and candidate endpoints (Phase 5).

The approval queue is the product's daily habit surface — the reason someone
opens the app — so its endpoint is the one that has to be boring and correct.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from billing.permissions import HasFlag
from billing.services.flags import TASTE_V5
from common.exceptions import NotFoundError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from taste.models import ContentCandidate, Decision, RuleSet, TasteProfile
from taste.serializers import (
    CandidateDecisionRequestSerializer,
    ContentCandidateSerializer,
    RuleSetSerializer,
    TasteProfileSerializer,
)
from taste.services import candidates as candidate_service
from taste.services import profiles as profile_service
from workspaces.models import Permission
from workspaces.permissions import HasPermission


def _permissions(action_name: str, *, write: str = Permission.EDIT) -> list[Any]:
    read = action_name in {"list", "retrieve"}
    needed = Permission.VIEW if read else write
    return [
        permission() for permission in (IsAuthenticated, HasFlag(TASTE_V5), HasPermission(needed))
    ]


class TasteProfileViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[TasteProfile],
):
    """The brand's voice, versioned (C-08).

    **Create-and-read only.** A profile is never edited in place: a change is
    a new version, because an edited row would make every decision recorded
    against it unattributable — which is the whole loop.
    """

    serializer_class = TasteProfileSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = TasteProfile.objects.all()

    def get_permissions(self) -> Any:
        return _permissions(self.action)

    def perform_create(self, serializer: BaseSerializer[TasteProfile]) -> None:
        serializer.instance = profile_service.create_profile(
            workspace=request_workspace(self.request),
            created_by=authenticated_user(self.request),
            **serializer.validated_data,
        )

    @extend_schema(
        request=None,
        responses={200: TasteProfileSerializer},
        summary="Make this the profile new content is produced under",
    )
    @action(detail=True, methods=["post"])
    def activate(self, request: Request, pk: str | None = None) -> Response:
        profile = profile_service.activate(self.get_object())
        return Response(TasteProfileSerializer(profile).data)

    @extend_schema(
        responses={200: TasteProfileSerializer},
        summary="The profile new content is currently produced under",
    )
    @action(detail=False, methods=["get"])
    def active(self, request: Request) -> Response:
        profile = profile_service.active_profile(request_workspace(request))
        if profile is None:
            # 404 rather than an empty object: "this workspace has not
            # described its brand yet" is a different answer from "here is a
            # blank brand", and only the first is true.
            raise NotFoundError(
                "This workspace has no active taste profile.", code="no_active_taste_profile"
            )
        return Response(TasteProfileSerializer(profile).data)


class ContentCandidateViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[ContentCandidate],
):
    """The approval queue — **the daily habit surface** (P5-16).

    Read plus two decisions. There is deliberately no create: a candidate comes
    from a generation, and one a client could post into the queue would be a
    proposal nothing produced and no profile judged.
    """

    serializer_class = ContentCandidateSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = ContentCandidate.objects.select_related("taste_profile", "product").prefetch_related(
        "decisions"
    )

    def get_permissions(self) -> Any:
        return _permissions(self.action, write=Permission.APPROVE)

    def _rendered(self, candidate: ContentCandidate) -> Response:
        """Serialised from a **fresh** read.

        `get_object()` comes off a queryset that prefetches `decisions`, so the
        instance an action holds carries a cache captured *before* that action
        wrote its decision. Serialising it directly returned a candidate with
        an empty decision list immediately after deciding — right when the
        client most needs to see it.
        """
        fresh = self.get_queryset().get(pk=candidate.pk)
        return Response(ContentCandidateSerializer(fresh).data)

    def get_queryset(self) -> Any:
        """**`screened_out` is never loaded** (P5-G1).

        Filtered here rather than in a serializer: a row a serializer omits was
        still read, still counted in a page total, and still one forgotten
        caller away from being rendered. `?state=` narrows further, and an
        unknown value is a 400 rather than a silently empty page — the same
        rule `?status=` on posts already follows.
        """
        from common.exceptions import OCCSError
        from taste.models import CandidateState

        queryset = super().get_queryset().exclude(state=CandidateState.SCREENED_OUT)

        states = self.request.query_params.getlist("state")
        if not states:
            return queryset
        unknown = sorted(set(states) - set(CandidateState.values))
        if unknown:
            raise OCCSError(
                f"Unknown candidate state: {', '.join(unknown)}.",
                code="invalid_candidate_state",
                detail={"state": unknown},
            )
        return queryset.filter(state__in=states)

    @extend_schema(
        request=CandidateDecisionRequestSerializer,
        responses={200: ContentCandidateSerializer},
        summary="Approve this candidate, and materialise its post",
        description=(
            "Writes a `Decision` and then creates the post — in that order, "
            "and there is no other path from a candidate to a post (P5-G3). "
            "Send `edited_payload` to accept with changes; the diff is kept, "
            "because what a person chose to fix is the most informative thing "
            "they can tell us."
        ),
    )
    @action(detail=True, methods=["post"])
    def approve(self, request: Request, pk: str | None = None) -> Response:
        payload = CandidateDecisionRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        candidate = candidate_service.approve(
            self.get_object(),
            actor=authenticated_user(request),
            edited_payload=payload.validated_data.get("edited_payload"),
            note=payload.validated_data.get("note", ""),
        )
        return self._rendered(candidate)

    @extend_schema(
        request=CandidateDecisionRequestSerializer,
        responses={200: ContentCandidateSerializer},
        summary="Reject this candidate, with a reason",
        description=(
            "`reason_code` is required and comes from the fixed vocabulary "
            "(P5-12): structured codes are what make rejections aggregable, "
            "and free text aggregates to nothing."
        ),
    )
    @action(detail=True, methods=["post"])
    def reject(self, request: Request, pk: str | None = None) -> Response:
        payload = CandidateDecisionRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        reason = payload.validated_data.get("reason_code")
        if not reason:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"reason_code": "A rejection needs a reason."})

        candidate = candidate_service.reject(
            self.get_object(),
            actor=authenticated_user(request),
            reason_code=reason,
            note=payload.validated_data.get("note", ""),
        )
        return self._rendered(candidate)

    @extend_schema(
        request=None,
        responses={201: ContentCandidateSerializer},
        summary="Try again, carrying the rejection reason forward",
    )
    @action(detail=True, methods=["post"])
    def regenerate(self, request: Request, pk: str | None = None) -> Response:
        child = candidate_service.regenerate(self.get_object(), actor=authenticated_user(request))
        return Response(ContentCandidateSerializer(child).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        responses={200: None},
        summary="What a reviewer changed before accepting",
        description=(
            "**`admin` only** (P5-13). The edit diff is the highest-value "
            "signal here and the most revealing about the people using the "
            "product, so it is not on the queue payload that every render "
            "carries. It never leaves the tenant and never enters a cohort "
            "aggregate (Part 7 rule 16)."
        ),
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="edit-diff",
        permission_classes=[IsAuthenticated, HasFlag(TASTE_V5), HasPermission(Permission.ADMIN)],
    )
    def edit_diff(self, request: Request, pk: str | None = None) -> Response:
        candidate = self.get_object()
        decisions = Decision.objects.filter(candidate=candidate).exclude(edit_diff=None)
        return Response(
            {
                "diffs": [
                    {"decision": row.pk, "created_at": row.created_at, "diff": row.edit_diff}
                    for row in decisions
                ]
            }
        )


class RuleSetViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[RuleSet],
):
    """Rule sets, read-only until Phase 7 proposes any (P5-05).

    **Learn proposes; humans activate** (Part 7 rule 14). The activation
    endpoint lands with the digest that produces something to activate —
    shipping a button now, with nothing behind it, is C-11's antipattern.
    """

    serializer_class = RuleSetSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = RuleSet.objects.prefetch_related("rules")

    def get_permissions(self) -> Any:
        return _permissions(self.action)
