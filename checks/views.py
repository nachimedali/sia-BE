"""`/posts/{id}/checks/` (steps-plan S4). The post is resolved through the
workspace- and visibility-scoped queryset, so another tenant's post — or one
this audience may not load — is a 404 from the query, never a hidden row."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFlag
from billing.services.flags import CHECKS_S4
from checks import services
from checks.models import CheckRun, CheckRunState
from checks.serializers import CheckRunSerializer
from common.exceptions import NotFoundError
from common.visibility import request_audience, visible_values
from common.workspaces import authenticated_user, request_workspace
from content.models import Post
from workspaces.models import Permission
from workspaces.permissions import HasPermission, caller_permissions


def _post(request: Request, pk: int) -> Post:
    post = (
        Post.objects.filter(
            workspace=request_workspace(request),
            visibility__in=visible_values(request_audience(request)),
            pk=pk,
        )
        .select_related("workspace", "category")
        .prefetch_related(*services.PREFETCH)
        .first()
    )
    if post is None:
        raise NotFoundError("No such post.", detail={"post": pk})
    return post


def _payload(run: CheckRun, current: str) -> dict[str, Any]:
    return {
        "id": run.pk,
        "state": run.state,
        "verdict": run.verdict,
        "counts": run.counts or None,
        "results": run.results,
        "revision": run.revision,
        "stale": run.state == CheckRunState.DONE and run.fingerprint != current,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


class PostChecksView(APIView):
    def get_permissions(self) -> list[Any]:
        return [IsAuthenticated(), HasFlag(CHECKS_S4)(), HasPermission(Permission.VIEW)()]

    @extend_schema(
        responses={200: CheckRunSerializer, 204: None},
        summary="This post's latest pre-publish checks",
        description="204 when it has never been checked. `stale` is true once the post "
        "changed after the run.",
    )
    def get(self, request: Request, pk: int) -> Response:
        post = _post(request, pk)
        run = CheckRun.objects.filter(post=post).first()
        if run is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(_payload(run, services.fingerprint(post)))

    @extend_schema(
        request=None,
        responses={202: CheckRunSerializer},
        summary="Run the pre-publish checks on the post as it is now",
        description="Queued on `ai_q`; poll `GET` until `state` is `DONE`. Needs `edit` or "
        "`approve` — the people who act on the answer.",
    )
    def post(self, request: Request, pk: int) -> Response:
        post = _post(request, pk)
        if not caller_permissions(request) & {Permission.EDIT, Permission.APPROVE}:
            raise PermissionDenied("Running checks needs edit or approve permission.")
        current = services.fingerprint(post)
        run = CheckRun.objects.create(
            workspace=post.workspace,
            post=post,
            created_by=authenticated_user(request),
            fingerprint=current,
        )
        from checks.tasks import run_post_checks

        run_post_checks.delay(post.pk, run.created_by_id, run.pk)
        run.refresh_from_db()
        return Response(_payload(run, current), status=status.HTTP_202_ACCEPTED)
