"""The variant dock's endpoints (X-09).

Service behaviour is pinned in `test_variants.py`; what these add is the
contract a client actually sees — the status codes, the upgrade payload the
dock renders its price tag from, and the tenancy boundary, which is a 404 and
never a 403 (Part 7 rule 3).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from ai.models import GenerationKind, GenerationMode
from ai.services.pipeline import create_generation, run_generation
from billing.services.ledger import grant_credits

pytestmark = pytest.mark.django_db


@pytest.fixture
def funded(workspace: Any, plans: Any, generation_costs: None) -> Any:
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan", "updated_at"])
    grant_credits(workspace, 500, note="test funding")
    return workspace


@pytest.fixture
def generation(funded: Any, user: Any) -> Any:
    row = create_generation(
        workspace=funded,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning",
        paid_slots=1,
    )
    return run_generation(row, n=1)


def url(generation: Any, name: str) -> str:
    return reverse(f"generation-{name}", args=[generation.pk])


def test_the_generation_reports_the_pool_it_renders(auth_client: Any, generation: Any) -> None:
    """The Studio draws one placeholder per variant while it waits, so the
    pool size has to be the server's — the same rule as every other number on
    the dock."""
    response = auth_client.get(reverse("generation-detail", args=[generation.pk]))

    assert response.status_code == 200
    body = response.json()
    assert body["variant_pool"] == generation.variant_pool
    assert body["variant_pool"] > body["paid_slots"]


def test_selecting_within_the_allowance_returns_the_generation(
    auth_client: Any, generation: Any
) -> None:
    variant = generation.variants.first()

    response = auth_client.post(
        url(generation, "select"), {"variant_ids": [variant.pk]}, format="json"
    )

    assert response.status_code == 200
    selected = [v for v in response.json()["variants"] if v["was_selected"]]
    assert [v["id"] for v in selected] == [variant.pk]


def test_going_past_the_allowance_is_402_with_the_price_to_clear_it(
    auth_client: Any, generation: Any
) -> None:
    """A `Purchasable` 402, not a plan one: this customer may already be on
    the top plan, and an upgrade prompt would be both wrong and useless."""
    ids = [v.pk for v in generation.variants.all()[:3]]

    response = auth_client.post(url(generation, "select"), {"variant_ids": ids}, format="json")

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "variant_allowance_exceeded"
    assert error["detail"]["unlock_required"] == 2
    assert error["detail"]["unlock_total"] == error["detail"]["unlock_price"] * 2


def test_unlocking_then_selecting_succeeds(auth_client: Any, generation: Any) -> None:
    first, second = list(generation.variants.all()[:2])

    unlocked = auth_client.post(
        url(generation, "unlock"), {"variant_ids": [second.pk]}, format="json"
    )
    assert unlocked.status_code == 200

    response = auth_client.post(
        url(generation, "select"), {"variant_ids": [first.pk, second.pk]}, format="json"
    )
    assert response.status_code == 200
    assert sum(1 for v in response.json()["variants"] if v["was_selected"]) == 2


def test_commit_makes_drafts_and_publishes_nothing(auth_client: Any, generation: Any) -> None:
    variant = generation.variants.first()
    auth_client.post(url(generation, "select"), {"variant_ids": [variant.pk]}, format="json")

    response = auth_client.post(url(generation, "commit"), {}, format="json")

    assert response.status_code == 201
    posts = response.json()
    assert len(posts) == 1
    assert posts[0]["status"] == "DRAFT"
    assert posts[0]["scheduled_at"] is None


def test_committing_an_empty_selection_is_409(auth_client: Any, generation: Any) -> None:
    response = auth_client.post(url(generation, "commit"), {}, format="json")

    assert response.status_code == 409


def test_another_workspaces_generation_is_404_never_403(
    auth_client: Any, generation: Any, other_user: Any, plans: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    provision_workspace(other_user, name="Someone Else")
    from rest_framework.test import APIClient

    theirs = APIClient()
    theirs.force_authenticate(other_user)

    for name, method in (("select", "post"), ("unlock", "post"), ("commit", "post")):
        response = getattr(theirs, method)(url(generation, name), {}, format="json")
        assert response.status_code == 404, name
