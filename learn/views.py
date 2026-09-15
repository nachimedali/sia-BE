"""Digest and rule-proposal surfaces.

**Learn proposes; a human activates** (Part 7 rule 14). There is no endpoint
here that activates a ruleset — accepting a proposal records the acceptance on
the rule, and putting a set into force stays where it already lives, in the
taste surfaces. A system that could both conclude and enact would be one whose
mistakes compound without anyone in the loop.

Cross-tenant is 404, never 403, on all three dimensions — the queryset is
workspace-scoped, and a rule proposal is reached through its digest's
workspace rather than through its own id.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from billing.permissions import HasFlag
from billing.services.flags import LEARN_V7
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from learn.models import Digest
from learn.serializers import (
    DigestSerializer,
    ProposedRuleSerializer,
    RuleVerdictSerializer,
    RunLearnSerializer,
)
from learn.services import proposals
from learn.services.run import run_learn
from planning.models import Campaign
from taste.models import Rule
from workspaces.models import Permission
from workspaces.permissions import HasPermission


class DigestViewSet(WorkspaceScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet[Digest]):
    """Read-only by design: a digest is produced by a job, never posted.

    `run` below is the one way to make one, and it is a separate action rather
    than `create` so that "produce a reading" is not mistaken for "write a
    document I composed".
    """

    serializer_class = DigestSerializer
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(LEARN_V7),
        HasPermission(Permission.ANALYZE),
    ]
    pagination_class = DefaultPagination
    queryset = Digest.objects.prefetch_related("findings", "proposed_rulesets__rules")

    @extend_schema(
        request=RunLearnSerializer,
        responses={201: DigestSerializer},
        summary="Run Learn now, optionally scoped to a campaign",
    )
    @action(detail=False, methods=["post"], url_path="run")
    def run(self, request: Request) -> Response:
        payload = RunLearnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        workspace = request_workspace(request)
        campaign = None
        campaign_id = payload.validated_data.get("campaign")
        if campaign_id:
            # Scoped lookup, so another tenant's campaign id is a 404 rather
            # than a digest computed over somebody else's posts.
            campaign = get_object_or_404(Campaign, pk=campaign_id, workspace=workspace)

        digest = run_learn(workspace, campaign=campaign, requested_by=authenticated_user(request))
        return Response(DigestSerializer(digest).data, status=201)


class ProposedRuleViewSet(WorkspaceScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet[Rule]):
    """Rule proposals awaiting a human (P7-11).

    Scoped through the ruleset's workspace rather than a column of its own: a
    second copy of the tenant key is a second thing that can disagree with the
    first, and the join is one step.

    **The mixin does the filtering, via `workspace_field`.** Hand-rolling the
    same filter in `get_queryset` would scope correctly today and be invisible
    to the router sweep, which walks for the mixin — so the next ViewSet
    written by copying this one would inherit the shape without the guarantee.
    """

    serializer_class = ProposedRuleSerializer
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(LEARN_V7),
        HasPermission(Permission.ADMIN),
    ]
    pagination_class = DefaultPagination
    workspace_field = "ruleset__workspace"
    #: Only Learn's proposals. A rule a human wrote directly is managed through
    #: the taste surfaces and has no verdict to give.
    queryset = (
        Rule.objects.filter(ruleset__derived_from__isnull=False)
        .select_related("ruleset")
        .order_by("id")
    )

    @extend_schema(
        request=RuleVerdictSerializer,
        responses={200: ProposedRuleSerializer},
        summary="Accept this proposal",
    )
    @action(detail=True, methods=["post"])
    def accept(self, request: Request, pk: str | None = None) -> Response:
        body = RuleVerdictSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rule = self.get_object()
        proposals.accept(
            rule, actor=authenticated_user(request), note=body.validated_data.get("note", "")
        )
        rule.refresh_from_db()
        return Response(ProposedRuleSerializer(rule).data)

    @extend_schema(
        request=RuleVerdictSerializer,
        responses={200: ProposedRuleSerializer},
        summary="Reject this proposal — recorded as a Decision, not deleted",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request: Request, pk: str | None = None) -> Response:
        body = RuleVerdictSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        rule = self.get_object()
        proposals.reject(
            rule,
            actor=authenticated_user(request),
            reason_code=body.validated_data.get("reason_code") or "other",
            note=body.validated_data.get("note", ""),
        )
        rule.refresh_from_db()
        return Response(ProposedRuleSerializer(rule).data)
