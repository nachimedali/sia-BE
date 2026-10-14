"""What the merged `/app/tools` page reads and writes (X-38).

The page shows the wallet, each tool's price and how often this workspace ran
it this month, and — to platform staff only — an "Edit price" control.
`ToolConfig` is one global row per tool, so a customer admin editing it would
change every other customer's price: the write is staff-only, not org-admin.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from billing.services import ledger
from tools.models import Tool, ToolConfig, ToolUsage
from tools.services import run

pytestmark = pytest.mark.django_db


@pytest.fixture
def tools(db: None) -> None:
    from django.core.management import call_command

    call_command("seed_tools", verbosity=0)


@pytest.fixture
def funded(workspace: Any, tools: None) -> Any:
    ledger.grant_credits(workspace, 50, note="test")
    return workspace


# -----------------------------------------------------------------------------
# Readiness carries the wallet, the price and this month's use
# -----------------------------------------------------------------------------
def test_readiness_reports_the_balance_and_each_price(auth_client: Any, funded: Any) -> None:
    ToolConfig.objects.filter(slug=Tool.BIO).update(credits_cost=4)

    body = auth_client.get(reverse("tools-readiness")).json()

    assert body["balance"] == 50
    assert states_of(body)[Tool.BIO]["credits_cost"] == 4


def test_readiness_counts_this_months_runs_for_this_workspace_only(
    auth_client: Any, funded: Any, user: Any, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    run(workspace=funded, user=user, slug=Tool.HEADLINE, payload={"topic": "a"})
    run(workspace=funded, user=user, slug=Tool.HEADLINE, payload={"topic": "b"})
    last_month = ToolUsage.objects.create(workspace=funded, user=user, tool=Tool.HEADLINE)
    ToolUsage.objects.filter(pk=last_month.pk).update(
        created_at=timezone.now().replace(day=1) - dt.timedelta(days=2)
    )
    theirs = provision_workspace(other_user, name="Not Yours")
    ToolUsage.objects.create(workspace=theirs, user=other_user, tool=Tool.HEADLINE)

    body = auth_client.get(reverse("tools-readiness")).json()

    assert states_of(body)[Tool.HEADLINE]["used_this_month"] == 2
    assert states_of(body)[Tool.HOOK]["used_this_month"] == 0


def test_only_staff_are_told_they_can_edit_prices(auth_client: Any, funded: Any, user: Any) -> None:
    assert auth_client.get(reverse("tools-readiness")).json()["can_edit_prices"] is False

    user.is_staff = True
    user.save(update_fields=["is_staff"])

    assert auth_client.get(reverse("tools-readiness")).json()["can_edit_prices"] is True


def states_of(body: dict[str, Any]) -> dict[str, Any]:
    return {row["slug"]: row for row in body["tools"]}


# -----------------------------------------------------------------------------
# PATCH /tools/{slug}/price/
# -----------------------------------------------------------------------------
def test_staff_change_a_tools_price(auth_client: Any, user: Any, tools: None) -> None:
    user.is_staff = True
    user.save(update_fields=["is_staff"])

    response = auth_client.patch(
        reverse("tool-price", args=[Tool.HOOK]), {"credits_cost": 3}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["credits_cost"] == 3
    assert ToolConfig.objects.get(slug=Tool.HOOK).credits_cost == 3


def test_the_next_run_is_charged_the_new_price(auth_client: Any, funded: Any, user: Any) -> None:
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    auth_client.patch(reverse("tool-price", args=[Tool.HOOK]), {"credits_cost": 3}, format="json")

    usage = run(workspace=funded, user=user, slug=Tool.HOOK, payload={"topic": "x"})

    assert usage.credits_charged == 3


def test_a_customer_cannot_change_a_price_even_as_owner(
    auth_client: Any, workspace: Any, tools: None
) -> None:
    response = auth_client.patch(
        reverse("tool-price", args=[Tool.HOOK]), {"credits_cost": 0}, format="json"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert ToolConfig.objects.get(slug=Tool.HOOK).credits_cost == 1


def test_an_unknown_tool_price_is_a_404(auth_client: Any, user: Any, tools: None) -> None:
    user.is_staff = True
    user.save(update_fields=["is_staff"])

    response = auth_client.patch(
        reverse("tool-price", args=["nope"]), {"credits_cost": 2}, format="json"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("value", [-1, "two", None])
def test_a_price_that_is_not_a_whole_number_of_credits_is_a_400(
    auth_client: Any, user: Any, tools: None, value: Any
) -> None:
    user.is_staff = True
    user.save(update_fields=["is_staff"])

    response = auth_client.patch(
        reverse("tool-price", args=[Tool.HOOK]), {"credits_cost": value}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_an_anonymous_caller_cannot_change_a_price(tools: None) -> None:
    from rest_framework.test import APIClient

    response = APIClient().patch(
        reverse("tool-price", args=[Tool.HOOK]), {"credits_cost": 2}, format="json"
    )

    assert response.status_code == 401
