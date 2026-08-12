"""`GET /trends/` and `POST /trends/refresh/` (design.md §7, §10.5, D18)."""

from __future__ import annotations

from typing import Any

import pytest

from content.models import Platform
from trends.models import TrendCluster
from trends.serializers import TEASER_LIMIT

pytestmark = pytest.mark.django_db

TRENDS_URL = "/api/v1/trends/"
REFRESH_URL = "/api/v1/trends/refresh/"


def test_trends_returns_ranked_clusters_with_exemplars(
    auth_client: Any, trend_workspace: Any, source: Any
) -> None:
    response = auth_client.get(TRENDS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body
    assert [c["composite_score"] for c in body] == sorted(
        (c["composite_score"] for c in body), reverse=True
    )
    assert body[0]["exemplars"]
    assert body[0]["locked"] is False
    assert body[0]["item_count"] >= 2


# -----------------------------------------------------------------------------
# test_free_tier_sees_teaser_only
# -----------------------------------------------------------------------------
def test_free_tier_sees_teaser_only(
    auth_client: Any, trend_workspace: Any, source: Any, plans: dict[str, Any]
) -> None:
    """§4.1 gives Free "teaser only", and §10.5 says a 402 reaching the user as
    an error is a UI bug — so the gate shapes the response rather than refusing
    it. The exemplars are the part worth paying for and never leave the server.
    """
    # Extract while paid, so the corpus exists and only the gate differs.
    auth_client.get(TRENDS_URL)
    assert TrendCluster.objects.exists()

    trend_workspace.plan = plans["free"]
    trend_workspace.save(update_fields=["plan"])

    response = auth_client.get(TRENDS_URL)

    assert response.status_code == 200
    body = response.json()
    assert len(body) <= TEASER_LIMIT
    assert all(cluster["locked"] is True for cluster in body)
    assert all(cluster["exemplars"] == [] for cluster in body)
    # The evidence count still shows: the teaser has to be persuasive (D18).
    assert all(cluster["item_count"] >= 2 for cluster in body)


def test_free_tier_cannot_force_a_refresh(
    auth_client: Any, trend_workspace: Any, source: Any, plans: dict[str, Any]
) -> None:
    """Reading a corpus someone else extracted is free; spending vendor quota
    is not — so this one is a real 402 with an upgrade payload (A2)."""
    trend_workspace.plan = plans["free"]
    trend_workspace.save(update_fields=["plan"])

    response = auth_client.post(REFRESH_URL, {"platform": Platform.TIKTOK}, format="json")

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]


def test_refresh_re_extracts_and_returns_the_full_corpus(
    auth_client: Any, trend_workspace: Any, source: Any
) -> None:
    first = auth_client.get(TRENDS_URL).json()

    response = auth_client.post(REFRESH_URL, {"platform": Platform.TIKTOK}, format="json")

    assert response.status_code == 200
    assert response.json()[0]["exemplars"]
    assert {c["id"] for c in response.json()} != {c["id"] for c in first}


def test_platform_defaults_to_the_workspaces_own(
    auth_client: Any, trend_workspace: Any, source: Any
) -> None:
    """Onboarding already collected the intended platforms; asking again in a
    query string would be the UI re-deriving something the server knows."""
    response = auth_client.get(TRENDS_URL)

    assert {cluster["platform"] for cluster in response.json()} == {Platform.TIKTOK}


def test_an_unsupported_platform_is_refused(auth_client: Any, trend_workspace: Any) -> None:
    response = auth_client.get(TRENDS_URL, {"platform": "myspace"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_platform"


def test_a_workspace_without_a_category_is_told_to_finish_onboarding(
    auth_client: Any, workspace: Any
) -> None:
    workspace.category = None
    workspace.platforms = [Platform.TIKTOK]
    workspace.save(update_fields=["category", "platforms"])

    response = auth_client.get(TRENDS_URL)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "category_required"


def test_trends_require_authentication(client: Any) -> None:
    assert client.get(TRENDS_URL).status_code == 401


def test_extract_task_is_a_thin_wrapper(trend_workspace: Any, source: Any) -> None:
    from trends.tasks import extract_trends

    count = extract_trends(trend_workspace.category_id, Platform.TIKTOK, force=True)

    assert count == TrendCluster.objects.count() > 0
