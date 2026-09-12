"""The Phase 3 rollout flag (Part 3: every phase ships behind one).

**Flag off produces pre-phase behaviour, not an error.** Before Phase 3 these
routes did not exist, so the honest answer with the flag off is 404 — not a
402, which would sell an upgrade that changes nothing, and not a 403, which
would claim the caller lacks a permission they hold.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from billing.models import FeatureFlag
from billing.services.flags import PLANNING_V3

pytestmark = pytest.mark.django_db


@pytest.fixture
def flag_off(workspace: Any) -> None:
    FeatureFlag.objects.create(organization=workspace.organization, key=PLANNING_V3, enabled=False)


@pytest.mark.parametrize(
    "url",
    ["/api/v1/campaigns/", "/api/v1/labels/", "/api/v1/saved-views/", "/api/v1/timetables/"],
)
def test_a_planning_collection_is_404_with_the_flag_off(
    auth_client: Any, flag_off: None, url: str
) -> None:
    assert auth_client.get(url).status_code == 404


def test_bulk_operations_are_404_with_the_flag_off(auth_client: Any, flag_off: None) -> None:
    assert auth_client.get("/api/v1/bulk-operations/").status_code == 404


def test_the_same_collections_answer_with_the_flag_on(auth_client: Any, workspace: Any) -> None:
    # The control. Without it the test above would pass against a router that
    # never registered these routes at all, which is a different bug wearing
    # the same 404.
    assert auth_client.get("/api/v1/campaigns/").status_code == 200


def test_a_post_is_still_a_social_post_with_the_flag_off(
    auth_client: Any, flag_off: None, workspace: Any
) -> None:
    """Nothing already stored changes meaning when the flag moves.

    `content_kind` defaults to `SOCIAL`, so a post written before, during or
    after Phase 3 reads identically — which is what makes the phase revertible
    rather than merely switchable.
    """
    response = auth_client.post("/api/v1/posts/", {"master_body": "Hello"}, format="json")

    assert response.status_code == 201
    assert response.json()["content_kind"] == "SOCIAL"


def test_campaigns_are_unreachable_but_not_broken_with_the_flag_off(
    auth_client: Any, flag_off: None, workspace: Any
) -> None:
    from planning.models import Campaign

    campaign = Campaign.objects.create(
        workspace=workspace,
        name="Existing",
        starts_at=timezone.now(),
        ends_at=timezone.now() + dt.timedelta(days=7),
    )

    # The row survives; only the route is gone. Turning the flag back on has to
    # find the data exactly as it was, or the phase is not revertible.
    assert auth_client.get(f"/api/v1/campaigns/{campaign.id}/").status_code == 404
    assert Campaign.objects.filter(pk=campaign.pk).exists()
