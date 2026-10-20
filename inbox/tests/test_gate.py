"""🚩 S2 gate: a captured comment is visible, assignable and answered end to
end on Advanced, and TikTok reads unavailable rather than empty."""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from analytics.providers.base import CommentSnapshot
from analytics.services import ingest
from analytics.tests.conftest import make_target
from channels.models import SocialAccount
from inbox import tasks

pytestmark = pytest.mark.django_db


def test_a_captured_comment_is_visible_coded_assigned_and_answered(
    auth_client: Any,
    workspace: Any,
    user: Any,
    plans: dict[str, Any],
    metrics_provider: Any,
) -> None:
    # Pro captures by polling; Advanced is webhook-driven (L-4a). The comment
    # arrives on the polling path and the workspace upgrades before replying.
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram", handle="@a", provider_account_id="acct-gate"
    )
    target = make_target(workspace, user, account, age_days=1)
    metrics_provider.comments_for[target.provider_post_id] = [
        CommentSnapshot(
            external_id="ig-1",
            body="Where can I buy this?",
            posted_at=timezone.now(),
            author="Maria",
        )
    ]

    # captured by the ordinary capture path, then coded by the beat pass
    assert ingest.capture_comments(target) == 1
    assert tasks.code_new_comments() == 1

    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])

    [row] = auth_client.get("/api/v1/inbox/comments/").json()["results"]
    assert (row["body"], row["intent"], row["status"]) == (
        "Where can I buy this?",
        "purchase_intent",
        "open",
    )

    mine = auth_client.patch(
        f"/api/v1/inbox/comments/{row['id']}/",
        {"assignee": user.pk, "status": "open"},
        format="json",
    )
    assert mine.json()["assignee"]["id"] == user.pk

    auth_client.post(
        f"/api/v1/inbox/comments/{row['id']}/notes/", {"body": "link the shop"}, format="json"
    )
    sent = auth_client.post(
        f"/api/v1/inbox/comments/{row['id']}/reply/", {"body": "In our shop!"}, format="json"
    )
    assert sent.status_code == 201
    done = auth_client.patch(
        f"/api/v1/inbox/comments/{row['id']}/", {"status": "done"}, format="json"
    ).json()

    assert metrics_provider.replies == [("ig-1", "In our shop!")]
    assert (done["status"], done["replies"], len(done["notes"])) == ("done", 1, 1)
    assert (
        auth_client.get("/api/v1/inbox/comments/", {"status": "done", "replied": "yes"}).json()[
            "count"
        ]
        == 1
    )


def test_tiktok_reads_unavailable_not_empty(
    auth_client: Any, workspace: Any, user: Any, plans: dict[str, Any], metrics_provider: Any
) -> None:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace, platform="tiktok", handle="@a", provider_account_id="acct-tt-gate"
    )
    target = make_target(workspace, user, account, age_days=1)

    ingest.capture_comments(target)

    body = auth_client.get("/api/v1/inbox/summary/").json()
    assert body["total"] == 0
    assert [(p["platform"], p["state"]) for p in body["platforms"]] == [("tiktok", "unavailable")]
