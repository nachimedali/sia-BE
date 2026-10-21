"""How a plan presents itself on the pricing and help pages (P4, P8).

The recommended plan is chosen in admin, never by position: the pricing page
used to badge whichever row sorted second, which was Free. The support reply
time is per plan and admin-editable like every other commercial term (rule 10).
And no plan sells auto-approval — L-2 refuses it on every tier, so a plan that
advertises it promises something the product will never do.
"""

from importlib import import_module
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command
from rest_framework.test import APIClient

from billing.models import Plan

pytestmark = pytest.mark.django_db


def _plan(code: str, **fields) -> Plan:
    return Plan.objects.create(code=code, display_name=code.title(), **fields)


def test_the_public_list_says_which_plan_is_recommended_and_how_fast_support_replies() -> None:
    _plan("pro", is_recommended=True, support_response="1 working day", sort_order=1)
    _plan("advanced", support_response="Same working day", sort_order=2)

    body = APIClient().get("/api/v1/billing/plans/").json()

    by_code = {row["code"]: row for row in body}
    assert by_code["pro"]["is_recommended"] is True
    assert by_code["advanced"]["is_recommended"] is False
    assert by_code["pro"]["support_response"] == "1 working day"


def test_recommending_a_plan_takes_the_mark_from_the_one_that_had_it() -> None:
    pro = _plan("pro", is_recommended=True)
    advanced = _plan("advanced")

    advanced.is_recommended = True
    advanced.save()

    pro.refresh_from_db()
    assert pro.is_recommended is False
    assert Plan.objects.filter(is_recommended=True).count() == 1


def test_a_hidden_plan_is_not_listed_even_when_recommended() -> None:
    _plan("legacy", is_public=False, is_recommended=True)

    assert APIClient().get("/api/v1/billing/plans/").json() == []


def test_the_seed_sells_no_auto_approval_and_recommends_one_plan(settings) -> None:
    call_command("seed_plans")

    for plan in Plan.objects.all():
        assert plan.features.get("autopilot_auto_approve", False) is False, plan.code
        assert "auto-approve" not in plan.tagline.lower(), plan.code
    assert list(Plan.objects.filter(is_recommended=True).values_list("code", flat=True)) == [
        "growth"
    ]
    # The 2026-10-21 packages replaced every legacy row on the public page.
    for code in ("free", "trial", "pro", "advanced"):
        assert not Plan.objects.get(code=code).is_public, code
    assert all(p.support_response for p in Plan.objects.filter(is_public=True))


def test_the_presentation_migration_fixes_an_existing_catalogue_without_undoing_edits(
    settings,
) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    import_module("billing.migrations.0018_seed_catalog").forwards(apps, None)
    # Rows as an older database holds them: Advanced sold auto-approval.
    advanced = Plan.objects.get(code="advanced")
    advanced.tagline = "Approvals, API access and auto-approve autopilot."
    advanced.features = {**advanced.features, "autopilot_auto_approve": True}
    advanced.save()
    # An operator's own wording for Pro survives.
    Plan.objects.filter(code="pro").update(tagline="Our own words")

    import_module("billing.migrations.0020_plan_presentation").forwards(apps, None)

    advanced.refresh_from_db()
    assert advanced.features["autopilot_auto_approve"] is False
    assert "auto-approve" not in advanced.tagline.lower()
    assert Plan.objects.get(code="pro").tagline == "Our own words"
    assert Plan.objects.get(code="pro").is_recommended is True
    assert Plan.objects.get(code="pro").support_response == "1 working day"

    import_module("billing.migrations.0020_plan_presentation").forwards(apps, None)
    assert Plan.objects.filter(is_recommended=True).count() == 1


def test_a_signed_in_caller_also_gets_their_own_plan_even_when_it_is_hidden(
    auth_client: Any, workspace: Any, plans: Any
) -> None:
    """The pricing page lists public plans; the billing page and onboarding
    must still show the plan the caller is on, public or not (P4) — else a
    workspace on a retired plan has no "current" card at all."""
    hidden = workspace.organization.plan
    hidden.is_public = False
    hidden.save()

    signed_in = [row["code"] for row in auth_client.get("/api/v1/billing/plans/").json()]
    anonymous = [row["code"] for row in APIClient().get("/api/v1/billing/plans/").json()]

    assert hidden.code in signed_in
    assert hidden.code not in anonymous
