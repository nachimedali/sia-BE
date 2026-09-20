"""Autopilot's guided setup (X-08).

`/app/autopilot` rendered an empty list to a workspace that had not finished
setting autopilot up, which is the worst available answer: the feature is not
broken and nothing on the screen says which of the six things it needs is
missing. These tests pin the readiness contract that replaces it.

**The endpoint is not entitlement-gated, and that is the point.** Gating it
behind `HasFeature("autopilot")` would answer a Free workspace with a 402 and
no guidance — so the plan is the first *row*, not the door.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from billing.services import ledger
from common.setup import DONE, MISSING
from products.models import AutopilotConfig

pytestmark = pytest.mark.django_db


def rows(response: Any) -> dict[str, Any]:
    return {row["key"]: row for row in response.json()["requirements"]}


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------
def test_a_fully_configured_workspace_is_ready(
    auth_client: Any, autopilot_config: Any, autopilot_workspace: Any
) -> None:
    response = auth_client.get(reverse("autopilot-readiness"))

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert {key: row["status"] for key, row in rows(response).items()} == {
        "plan": DONE,
        "product": DONE,
        "taste_profile": DONE,
        "autopilot": DONE,
        "credits": DONE,
        "channel": DONE,
    }


def test_ready_workspace_reports_its_next_slots(auth_client: Any, autopilot_config: Any) -> None:
    """The one question a user has once setup is done — "so when?" — answered
    from the same grid the engine drafts from, not from prose."""
    response = auth_client.get(reverse("autopilot-readiness"))

    assert len(response.json()["next_slots"]) > 0


# -----------------------------------------------------------------------------
# Each requirement, failing on its own
# -----------------------------------------------------------------------------
def test_a_free_workspace_gets_guidance_rather_than_a_402(auth_client: Any, workspace: Any) -> None:
    response = auth_client.get(reverse("autopilot-readiness"))

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert rows(response)["plan"]["status"] == MISSING


def test_an_inactive_taste_profile_is_offered_for_activation(
    auth_client: Any, autopilot_product: Any
) -> None:
    """A saved-but-inactive version is one click from done, so the row carries
    the version to activate rather than sending the user off to re-enter it."""
    from taste.services.profiles import create_profile

    profile = create_profile(workspace=autopilot_product.workspace)

    row = rows(auth_client.get(reverse("autopilot-readiness")))["taste_profile"]

    assert row["status"] == MISSING
    assert row["target_id"] == profile.pk


def test_autopilot_row_names_the_product_to_switch_on(
    auth_client: Any, autopilot_product: Any
) -> None:
    row = rows(auth_client.get(reverse("autopilot-readiness")))["autopilot"]

    assert row["status"] == MISSING
    assert row["target_id"] == autopilot_product.pk


def test_the_cadence_is_reported_on_the_row_that_owns_it(
    auth_client: Any, autopilot_config: Any
) -> None:
    """Cadence is not a seventh card: it is what the autopilot row *says*, so
    the summary has to carry the real numbers rather than a generic 'on'."""
    autopilot_config.cadence_days = 5
    autopilot_config.lookahead_days = 20
    autopilot_config.save(update_fields=["cadence_days", "lookahead_days"])

    row = rows(auth_client.get(reverse("autopilot-readiness")))["autopilot"]

    assert row["facts"]["cadence_days"] == 5
    assert row["facts"]["lookahead_days"] == 20


def test_a_product_with_no_reference_image_is_not_a_ready_product(
    auth_client: Any, autopilot_workspace: Any
) -> None:
    from products.services.products import create_product

    create_product(workspace=autopilot_workspace, name="Unphotographed")

    row = rows(auth_client.get(reverse("autopilot-readiness")))["product"]

    assert row["status"] == MISSING
    assert row["facts"]["ready"] == 0
    assert row["facts"]["total"] == 1


def test_an_empty_credit_balance_blocks(auth_client: Any, autopilot_config: Any) -> None:
    from billing.services.entitlements import entitlements_for

    workspace = autopilot_config.product.workspace
    ledger.debit_credits(
        workspace,
        ledger.credit_balance(workspace),
        note="spend it all",
        quota=entitlements_for(workspace).quota("monthly_ai_credits"),
    )

    response = auth_client.get(reverse("autopilot-readiness"))

    assert rows(response)["credits"]["status"] == MISSING
    assert response.json()["ready"] is False


def test_a_missing_channel_warns_without_blocking(auth_client: Any, autopilot_config: Any) -> None:
    """Drafting works with nowhere to publish; *approving* does not. Telling
    the user at approval time is a dead end, so it is a row — but a row that
    does not claim autopilot is broken."""
    from channels.models import SocialAccount

    SocialAccount.objects.filter(workspace=autopilot_config.product.workspace).delete()

    response = auth_client.get(reverse("autopilot-readiness"))

    row = rows(response)["channel"]
    assert row["status"] == MISSING
    assert row["blocking"] is False
    assert response.json()["ready"] is True


# -----------------------------------------------------------------------------
# Tenancy
# -----------------------------------------------------------------------------
def test_another_workspaces_setup_does_not_count_as_yours(
    auth_client: Any, autopilot_config: Any, other_user: Any
) -> None:
    """The readiness of one brand must not read as the readiness of another —
    the same 404-not-403 discipline applied to an endpoint that takes no id."""
    from workspaces.services.provisioning import provision_workspace

    second = provision_workspace(other_user, name="Second Brand")
    auth_client.force_authenticate(other_user)

    response = auth_client.get(
        reverse("autopilot-readiness"), headers={"X-Workspace-Id": str(second.pk)}
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    body = rows(response)
    assert body["product"]["status"] == MISSING
    assert body["taste_profile"]["status"] == MISSING
    assert AutopilotConfig.objects.filter(product__workspace=second).count() == 0
