"""SA-60/61/62 end to end: comments captured through SocialAPI, answered and moderated.

The gate lines (plan §10): 402 wording for each budget — ours and the
vendor's; capability flags respected; another tenant's comment is a 404; a
refused reply gives the allowance back.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from analytics.models import AudienceComment, AudienceCommentAction, Availability
from analytics.providers.base import MetricsProviderRegistry
from analytics.services import ingest
from analytics.tests.conftest import make_target
from billing.models import ReplyLedger, ReplyReason
from billing.services.ledger import replies_used_this_month
from social_providers.socialapi.audience import SocialAPIAudienceProvider
from social_providers.socialapi.metrics import SocialAPIMetricsProvider
from social_providers.tests.stub import ReadVendor, error

pytestmark = pytest.mark.django_db

POST = "17895695668004550"
ACCOUNT = "acc_1"
FLAGS = {
    "can_reply": True,
    "can_hide": True,
    "can_like": False,
    "can_delete": True,
    "can_private_reply": True,
}


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> ReadVendor:
    """SocialAPI serves measurement and the audience."""
    from analytics import providers

    stub = ReadVendor()
    stub.routes["GET /usage"] = {"interactions_used": 3, "interactions_limit": 50}

    def registry() -> MetricsProviderRegistry:
        return MetricsProviderRegistry(
            [SocialAPIMetricsProvider(stub.client())],
            audience=SocialAPIAudienceProvider(stub.client()),
        )

    monkeypatch.setattr(ingest, "get_metrics_registry", registry)
    monkeypatch.setattr(providers, "get_metrics_registry", registry)
    return stub


@pytest.fixture
def advanced(paid_workspace: Any, plans: dict[str, Any]) -> Any:
    paid_workspace.organization.plan = plans["advanced"]
    paid_workspace.organization.save(update_fields=["plan"])
    return paid_workspace


@pytest.fixture
def target(advanced: Any, user: Any, social_account: Any) -> Any:
    social_account.provider_account_id = ACCOUNT
    social_account.provider_key = "socialapi"
    social_account.save(update_fields=["provider_account_id", "provider_key"])
    made = make_target(advanced, user, social_account, provider_post_id="post-uuid")
    made.platform_post_id = POST
    made.provider_key = "socialapi"
    made.save(update_fields=["platform_post_id", "provider_key"])
    return made


def _comment(target: Any, *, external_id: str = "sapi_cmt_1", **flags: bool) -> AudienceComment:
    return AudienceComment.objects.create(
        post_target=target,
        external_id=external_id,
        author="Jane",
        body="Love it",
        posted_at=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
        capabilities={**FLAGS, **flags},
        reactions=None,
    )


# -----------------------------------------------------------------------------
# Capture (SA-60/61)
# -----------------------------------------------------------------------------
def test_a_poll_stores_comments_with_their_flags_and_no_breakdown(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    social_account.provider_account_id = ACCOUNT
    social_account.save(update_fields=["provider_account_id"])
    made = make_target(paid_workspace, user, social_account, provider_post_id="post-uuid")
    made.platform_post_id = POST
    made.save(update_fields=["platform_post_id"])
    vendor.routes[f"GET /inbox/comments/{POST}"] = {
        "data": [
            {
                "id": "sapi_cmt_1",
                "text": "Love it",
                "author_name": "Jane",
                "created_at": "2026-10-13T10:00:00Z",
                "capabilities": FLAGS,
            }
        ],
        "pagination": {"has_more": False},
    }

    assert ingest.capture_comments(made) == 1
    assert ingest.capture_comments(made) == 0, "the cadence is spent"

    stored = AudienceComment.objects.get(post_target=made)
    assert stored.capabilities == FLAGS
    assert stored.reactions is None
    assert stored.availability == Availability.MEASURED
    assert vendor.calls[0][2]["account_id"] == ACCOUNT


def test_tiktok_comments_read_unavailable_not_empty(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    social_account.platform = "tiktok"
    social_account.save(update_fields=["platform"])
    made = make_target(paid_workspace, user, social_account)

    ingest.capture_comments(made)

    assert AudienceComment.objects.get(post_target=made).availability == Availability.UNAVAILABLE
    assert vendor.calls == []


def test_the_feed_says_reactions_are_not_shared(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    _comment(target)

    [row] = auth_client.get("/api/v1/analytics/comments/").json()

    assert row["reactions_shared"] is False
    assert row["capabilities"]["can_hide"] is True
    assert row["state"] == {"hidden": False, "liked": False, "deleted": False, "replies": 0}


# -----------------------------------------------------------------------------
# Replying: two budgets, the flags, refunds (SA-62)
# -----------------------------------------------------------------------------
def reply(client: Any, comment: AudienceComment, body: str = "Thank you!") -> Any:
    return client.post(
        f"/api/v1/analytics/comments/{comment.pk}/reply/", {"body": body}, format="json"
    )


def test_a_reply_goes_out_spends_one_of_each_allowance_and_is_recorded(
    auth_client: Any, vendor: ReadVendor, target: Any, user: Any
) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}"] = {"comment_id": "sapi_cmt_99"}
    comment = _comment(target)

    response = reply(auth_client, comment)

    assert response.status_code == 201
    assert response.json() == {"external_id": "sapi_cmt_99"}
    assert vendor.bodies[-1] == {
        "account_id": ACCOUNT,
        "comment_id": "sapi_cmt_1",
        "text": "Thank you!",
    }
    assert replies_used_this_month(target.post.workspace.organization) == 1
    action = AudienceCommentAction.objects.get(comment=comment)
    assert (action.action, action.actor, action.external_id) == ("reply", user, "sapi_cmt_99")


def test_a_comment_the_platform_will_not_let_us_answer_is_refused_before_spending(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    comment = _comment(target, can_reply=False)

    response = reply(auth_client, comment)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "comment_action_unsupported"
    assert not ReplyLedger.objects.exists()
    assert not [c for c in vendor.calls if c[0] == "POST"]


def test_the_vendors_allowance_used_up_refuses_in_its_own_words(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    vendor.routes["GET /usage"] = {"interactions_used": 50, "interactions_limit": 50}
    comment = _comment(target)

    response = reply(auth_client, comment)

    assert response.status_code == 402
    body = response.json()["error"]
    assert body["code"] == "provider_interactions_exhausted"
    assert "provider" in body["message"]
    assert "upgrade" in body
    assert not ReplyLedger.objects.exists()


def test_our_allowance_used_up_refuses_as_the_plans(
    auth_client: Any, vendor: ReadVendor, target: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from billing.services import ledger

    monkeypatch.setattr(ledger, "replies_used_this_month", lambda *_a, **_k: 10**9)

    response = reply(auth_client, _comment(target))

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "insufficient_credits"


def test_a_reply_the_vendor_refuses_gives_the_allowance_back(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}"] = error(
        400, "validation.text_too_long", "Too long for Instagram."
    )
    comment = _comment(target)

    response = reply(auth_client, comment)

    assert response.status_code == 422
    organization = target.post.workspace.organization
    assert replies_used_this_month(organization) == 0
    reasons = list(ReplyLedger.objects.order_by("pk").values_list("reason", "delta"))
    assert reasons == [(ReplyReason.OUTBOUND, -1), (ReplyReason.REFUNDED, 1)]
    assert not AudienceCommentAction.objects.exists()


def test_the_vendors_monthly_limit_hit_mid_reply_is_a_402_and_a_refund(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}"] = httpx.Response(
        429,
        json={"error": {"code": "validation.interaction_limit_exhausted", "message": "Limit"}},
    )

    response = reply(auth_client, _comment(target))

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "provider_interactions_exhausted"
    assert replies_used_this_month(target.post.workspace.organization) == 0


def test_below_the_top_plan_replying_is_a_402_with_the_way_up(
    auth_client: Any, vendor: ReadVendor, target: Any, plans: dict[str, Any]
) -> None:
    organization = target.post.workspace.organization
    organization.plan = plans["pro"]
    organization.save(update_fields=["plan"])

    response = reply(auth_client, _comment(target))

    assert response.status_code == 402
    assert "upgrade" in response.json()["error"]


# -----------------------------------------------------------------------------
# Moderating (SA-62)
# -----------------------------------------------------------------------------
def act(client: Any, comment: AudienceComment, action: str) -> Any:
    return client.post(
        f"/api/v1/analytics/comments/{comment.pk}/actions/", {"action": action}, format="json"
    )


def test_hide_and_show_again_are_recorded_and_reflected(
    auth_client: Any, vendor: ReadVendor, target: Any, user: Any
) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}/sapi_cmt_1/hide"] = {"success": True}
    vendor.routes[f"DELETE /inbox/comments/{POST}/sapi_cmt_1/hide"] = {"success": True}
    comment = _comment(target)

    hidden = act(auth_client, comment, "hide")
    shown = act(auth_client, comment, "unhide")

    assert hidden.status_code == 200 and hidden.json()["hidden"] is True
    assert shown.json()["hidden"] is False
    assert list(
        AudienceCommentAction.objects.order_by("created_at").values_list("action", "actor")
    ) == [("hide", user.pk), ("unhide", user.pk)]


def test_an_action_the_comments_flags_rule_out_never_reaches_the_platform(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    comment = _comment(target)  # Instagram: can_like is false

    response = act(auth_client, comment, "like")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "comment_action_unsupported"
    assert vendor.calls == []


def test_a_deleted_comment_takes_no_further_action(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    vendor.routes[f"DELETE /inbox/comments/{POST}/sapi_cmt_1"] = {"success": True}
    comment = _comment(target)

    assert act(auth_client, comment, "delete").json()["deleted"] is True
    again = act(auth_client, comment, "hide")

    assert again.status_code == 422
    assert again.json()["error"]["code"] == "comment_deleted"


def test_a_comment_without_flags_offers_no_moderation(
    auth_client: Any, vendor: ReadVendor, target: Any
) -> None:
    comment = _comment(target)
    AudienceComment.objects.filter(pk=comment.pk).update(capabilities=None)

    assert act(auth_client, comment, "hide").status_code == 422


def test_an_unknown_action_is_a_400(auth_client: Any, vendor: ReadVendor, target: Any) -> None:
    assert act(auth_client, _comment(target), "pin").status_code == 400


def test_moderating_needs_the_top_plan(
    auth_client: Any, vendor: ReadVendor, target: Any, plans: dict[str, Any]
) -> None:
    organization = target.post.workspace.organization
    organization.plan = plans["pro"]
    organization.save(update_fields=["plan"])

    response = act(auth_client, _comment(target), "hide")

    assert response.status_code == 402
    assert vendor.calls == []


def test_another_tenants_comment_is_not_found(
    auth_client: Any, vendor: ReadVendor, target: Any, other_workspace: Any
) -> None:
    workspace, author, account = other_workspace
    theirs = make_target(workspace, author, account)
    comment = _comment(theirs)

    response = act(auth_client, comment, "hide")
    assert response.status_code == 404, response.json()
    assert reply(auth_client, comment).status_code == 404
    assert vendor.calls == []
