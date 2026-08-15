"""Analytics endpoints (design.md §7, §8.9, §10.5)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import Comment, RepurposeCandidate, Sentiment
from analytics.tests.conftest import capture, make_target

pytestmark = pytest.mark.django_db

OVERVIEW = "/api/v1/analytics/overview/"
POSTS = "/api/v1/analytics/posts/"
BEST_TIMES = "/api/v1/analytics/best-times/"
SENTIMENT = "/api/v1/analytics/sentiment/"
COMMENTS = "/api/v1/analytics/comments/"
REPURPOSE = "/api/v1/analytics/repurpose/"


def test_overview_reports_headline_numbers_and_attribution(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    capture(make_target(paid_workspace, user, social_account, source="AI"), rate=0.30)
    capture(make_target(paid_workspace, user, social_account, source="MANUAL"), rate=0.10)

    response = auth_client.get(OVERVIEW)

    assert response.status_code == 200
    body = response.json()
    assert body["posts"] == 2
    assert body["impressions"] == 2000
    assert body["by_source"]["AI"]["engagement_rate"] == pytest.approx(0.30)
    assert len(body["top"]) == 2


def test_posts_are_ranked_best_first(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    capture(make_target(paid_workspace, user, social_account), rate=0.05)
    capture(make_target(paid_workspace, user, social_account), rate=0.50)

    body = auth_client.get(POSTS).json()

    assert [row["engagement_rate"] for row in body] == [0.50, 0.05]


def test_a_free_plan_sees_a_shorter_window_not_a_402(
    auth_client: Any, workspace: Any, user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    """Reading your own numbers is not a paid feature; keeping two years of
    them is (§4.1). A Free workspace gets 7 days, and gets them successfully."""
    workspace.plan = plans["free"]
    workspace.save(update_fields=["plan"])
    capture(make_target(workspace, user, social_account, age_days=3), rate=0.20)
    capture(make_target(workspace, user, social_account, age_days=40), rate=0.90)

    response = auth_client.get(POSTS)

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_best_times_returns_buckets(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    when = timezone.now() - dt.timedelta(days=5)
    for _ in range(2):
        target = make_target(paid_workspace, user, social_account)
        target.published_at = when
        target.save(update_fields=["published_at"])
        capture(target, rate=0.2)

    body = auth_client.get(BEST_TIMES).json()

    assert body[0]["samples"] == 2
    assert body[0]["weekday"] == when.weekday()


def test_sentiment_aggregates_across_the_workspace(auth_client: Any, published_target: Any) -> None:
    now = timezone.now()
    for index, (sentiment, score) in enumerate(
        [(Sentiment.POSITIVE, 1.0), (Sentiment.POSITIVE, 1.0), (Sentiment.NEGATIVE, -1.0)]
    ):
        Comment.objects.create(
            post_target=published_target,
            external_id=f"c-{index}",
            body="x",
            sentiment=sentiment,
            sentiment_score=score,
            posted_at=now,
        )

    body = auth_client.get(SENTIMENT).json()

    assert body == {
        "total": 3,
        "positive": 2,
        "neutral": 0,
        "negative": 1,
        "score": pytest.approx(0.3333),
    }


def test_comments_are_listed_newest_first(auth_client: Any, published_target: Any) -> None:
    now = timezone.now()
    Comment.objects.create(
        post_target=published_target,
        external_id="old",
        body="older",
        posted_at=now - dt.timedelta(days=1),
    )
    Comment.objects.create(
        post_target=published_target, external_id="new", body="newer", posted_at=now
    )

    body = auth_client.get(COMMENTS).json()

    assert [c["body"] for c in body] == ["newer", "older"]


def test_another_workspaces_numbers_are_never_visible(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    """These endpoints are not on `router`, so the tenancy sweep does not walk
    them — the scoping is asserted here instead."""
    from django.contrib.auth import get_user_model

    from channels.models import SocialAccount
    from workspaces.services.provisioning import provision_workspace

    other_user = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_ws = provision_workspace(other_user, name="Other")
    other_ws.plan = plans["pro"]
    other_ws.save(update_fields=["plan"])
    other_account = SocialAccount.objects.create(
        workspace=other_ws, platform="instagram", handle="@o", provider_account_id="acct-o"
    )
    capture(make_target(other_ws, other_user, other_account), rate=0.99)

    assert auth_client.get(POSTS).json() == []
    assert auth_client.get(OVERVIEW).json()["posts"] == 0


# -----------------------------------------------------------------------------
# Repurpose queue — a paid feature (§4.1)
# -----------------------------------------------------------------------------
def _candidate(workspace: Any, user: Any, account: Any) -> RepurposeCandidate:
    target = make_target(workspace, user, account, age_days=75)
    return RepurposeCandidate.objects.create(post=target.post, percentile=95.0, score=90.0)


def test_the_repurpose_queue_lists_open_candidates_best_first(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    low = _candidate(paid_workspace, user, social_account)
    low.score = 10.0
    low.save(update_fields=["score"])
    _candidate(paid_workspace, user, social_account)

    body = auth_client.get(REPURPOSE).json()

    assert [row["score"] for row in body] == [90.0, 10.0]
    assert body[0]["post_body"]


def test_the_repurpose_queue_is_gated_to_paid(
    auth_client: Any, workspace: Any, plans: dict[str, Any]
) -> None:
    workspace.plan = plans["free"]
    workspace.save(update_fields=["plan"])

    response = auth_client.get(REPURPOSE)

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]


def test_accepting_opens_a_draft(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    candidate = _candidate(paid_workspace, user, social_account)

    response = auth_client.post(f"{REPURPOSE}{candidate.pk}/accept/")

    assert response.status_code == 200
    candidate.refresh_from_db()
    assert candidate.reissued_post is not None


def test_dismissing_closes_it(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    candidate = _candidate(paid_workspace, user, social_account)

    response = auth_client.post(f"{REPURPOSE}{candidate.pk}/dismiss/")

    assert response.status_code == 200
    candidate.refresh_from_db()
    assert candidate.dismissed_at is not None
    assert auth_client.get(REPURPOSE).json() == []


def test_another_workspaces_candidate_is_a_404_not_a_403(
    auth_client: Any, paid_workspace: Any, plans: dict[str, Any]
) -> None:
    """A9: existence itself is not disclosed."""
    from django.contrib.auth import get_user_model

    from channels.models import SocialAccount
    from workspaces.services.provisioning import provision_workspace

    other_user = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_ws = provision_workspace(other_user, name="Other")
    other_ws.plan = plans["pro"]
    other_ws.save(update_fields=["plan"])
    other_account = SocialAccount.objects.create(
        workspace=other_ws, platform="instagram", handle="@o", provider_account_id="acct-o"
    )
    theirs = _candidate(other_ws, other_user, other_account)

    assert auth_client.post(f"{REPURPOSE}{theirs.pk}/dismiss/").status_code == 404


def test_analytics_requires_authentication(client: Any) -> None:
    assert client.get(OVERVIEW).status_code == 401


def test_the_beat_tasks_are_thin_wrappers(published_target: Any, platform_adapter: Any) -> None:
    from analytics.tasks import capture_due_metrics, scan_repurpose_candidates, snapshot_accounts

    assert capture_due_metrics() == 1
    assert snapshot_accounts() == 1
    assert scan_repurpose_candidates() == 0
