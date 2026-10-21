"""The 2026-10-21 packages (pricing.plan.md, Phase 2) and what they rest on.

- `Plan.is_default` replaced the row coded `free` as the floor: signup lands on
  it, a lapsed subscription returns to it, and there is exactly one.
- Upgrade prompts name the cheapest public plan that unlocks what was refused,
  read from the catalogue rather than from plan codes in the code.
- `billing/0022` brings an existing catalogue to the packages without undoing
  an operator's edit.
- A Stripe price pasted on a plan's price row maps a subscription back to the
  plan, as checkout reads it from there first.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils import timezone

from billing.models import Pack, Plan, PlanPrice
from billing.services import subscriptions, webhooks
from billing.services.entitlements import entitlements_for
from billing.services.plans import default_plan, suggest_upgrade
from common.exceptions import OCCSError

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# The packages as seeded
# -----------------------------------------------------------------------------
def test_the_public_list_is_the_four_packages_in_price_order(plans) -> None:
    public = list(Plan.objects.filter(is_public=True).order_by("sort_order"))

    assert [plan.code for plan in public] == ["spark", "essential", "growth", "scale"]
    assert [plan.price_monthly_cents for plan in public] == [0, 4900, 9900, 24900]
    # Annual is ten months: two free.
    assert [plan.price_annual_cents for plan in public] == [0, 49000, 99000, 249000]


def test_each_package_carries_the_ladder_the_audit_sets(plans) -> None:
    spark, essential, growth, scale = (plans[c] for c in ("spark", "essential", "growth", "scale"))

    assert spark.feature("auto_publish") is False and spark.max_autopublish_posts == 0
    assert [p.max_social_accounts for p in (spark, essential, growth, scale)] == [1, 3, 5, 10]
    assert [p.monthly_ai_credits for p in (spark, essential, growth, scale)] == [
        60,
        500,
        1100,
        2200,
    ]
    assert [p.feature("analytics_history_days") for p in (spark, essential, growth, scale)] == [
        7,
        90,
        365,
        730,
    ]
    assert growth.feature("autopilot") and not essential.feature("autopilot")
    assert scale.feature("reply_to_comments") and not growth.feature("reply_to_comments")
    # L-4a: daily, daily, six-hourly, webhook.
    assert [p.comment_capture_interval_minutes for p in (spark, essential, growth, scale)] == [
        1440,
        1440,
        360,
        0,
    ]
    # L-2 on every package: nothing sells skipping the human.
    assert not any(p.feature("autopilot_auto_approve") for p in plans.values())


# -----------------------------------------------------------------------------
# The default plan
# -----------------------------------------------------------------------------
def test_a_new_account_lands_on_the_default_plan(workspace) -> None:
    assert workspace.organization.plan.code == "spark"
    assert entitlements_for(workspace).plan.is_default


def test_moving_the_default_moves_signup_with_it(plans, other_user) -> None:
    """A package rename is a tick box in admin, not a deploy."""
    from workspaces.services.provisioning import provision_workspace

    free = plans["free"]
    free.is_default = True
    free.save()

    assert list(Plan.objects.filter(is_default=True)) == [free]
    assert provision_workspace(other_user).organization.plan == free


def test_a_paid_plan_cannot_be_the_default(plans) -> None:
    growth = plans["growth"]
    growth.is_default = True

    with pytest.raises(ValidationError) as caught:
        growth.save()
    assert "is_default" in caught.value.message_dict


def test_no_default_plan_is_a_deployment_error_not_a_silent_guess(plans) -> None:
    Plan.objects.update(is_default=False)

    with pytest.raises(RuntimeError, match="default"):
        default_plan()


def test_a_lapsed_subscription_returns_to_the_default_plan(workspace, plans) -> None:
    workspace.organization.plan = plans["growth"]
    workspace.organization.save(update_fields=["plan"])

    subscriptions.downgrade_to_free(workspace, reason="test")

    workspace.organization.refresh_from_db()
    assert workspace.organization.plan.code == "spark"


def test_the_default_plan_cannot_be_checked_out(workspace, plans) -> None:
    with pytest.raises(OCCSError) as caught:
        subscriptions.start_checkout(
            workspace, plan_code="spark", cycle="monthly", success_url="s", cancel_url="c"
        )
    assert caught.value.code == "invalid_plan"


def test_a_retired_plan_cannot_be_checked_out(workspace, plans) -> None:
    """Pro and Advanced keep their subscribers; nobody new can buy them."""
    for code in ("pro", "advanced", "trial"):
        with pytest.raises(OCCSError) as caught:
            subscriptions.start_checkout(
                workspace, plan_code=code, cycle="monthly", success_url="s", cancel_url="c"
            )
        assert caught.value.code == "invalid_plan", code


# -----------------------------------------------------------------------------
# Upgrade suggestions come from the catalogue
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "feature", "expected"),
    [
        ("spark", None, "essential"),
        ("spark", "auto_publish", "essential"),
        ("spark", "autopilot", "growth"),
        ("essential", "approval_workflow", "growth"),
        ("growth", "api_access", "scale"),
        ("growth", "reply_to_comments", "scale"),
        # A retired plan's subscriber is pointed at a public package.
        ("pro", "approval_workflow", "growth"),
        # Nothing above the top: the top itself, never an empty prompt.
        ("scale", "api_access", "scale"),
    ],
)
def test_the_suggestion_is_the_cheapest_public_plan_that_unlocks_it(
    plans, current, feature, expected
) -> None:
    assert suggest_upgrade(plans[current], feature) == expected


def test_an_unpublished_package_is_never_suggested(plans) -> None:
    Plan.objects.filter(code="essential").update(is_public=False)

    assert suggest_upgrade(plans["spark"], "auto_publish") == "growth"


def test_an_empty_catalogue_still_names_a_plan(plans) -> None:
    Plan.objects.update(is_public=False)

    assert suggest_upgrade(plans["spark"]) == "pro"


# -----------------------------------------------------------------------------
# billing/0022 on a database that ran 0018 before the packages existed
# -----------------------------------------------------------------------------
def _older_catalogue(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    import_module("billing.migrations.0018_seed_catalog").forwards(apps, None)
    # 0018 now reads lists that already retire the legacy rows; an older
    # database still has them public, with Pro recommended and Free the floor.
    Plan.objects.filter(code__in=("trial", "pro", "advanced")).update(is_public=True)
    Plan.objects.filter(code="pro").update(is_recommended=True)
    Plan.objects.filter(code="free").update(is_default=True)
    Pack.objects.filter(code="credits-500").update(is_public=True)
    Plan.objects.filter(code__in=("spark", "essential", "growth", "scale")).delete()
    Pack.objects.filter(code="credits-100").delete()


def _run_0022() -> None:
    import_module("billing.migrations.0022_packages_2026_10").forwards(apps, None)


def test_the_migration_brings_an_older_catalogue_to_the_packages(settings) -> None:
    _older_catalogue(settings)

    _run_0022()

    assert list(Plan.objects.filter(is_public=True).values_list("code", flat=True)) == [
        "spark",
        "essential",
        "growth",
        "scale",
    ]
    assert Plan.objects.get(is_default=True).code == "spark"
    assert list(Plan.objects.filter(is_recommended=True).values_list("code", flat=True)) == [
        "growth"
    ]
    # Each package gets its default price row, with no Stripe id to guess.
    for code in ("essential", "growth", "scale"):
        price = PlanPrice.objects.get(plan__code=code, is_default=True)
        assert price.monthly_cents == Plan.objects.get(code=code).price_monthly_cents
        assert price.stripe_price_id_monthly == ""
    assert Pack.objects.get(code="credits-100").is_public
    assert not Pack.objects.get(code="credits-500").is_public
    assert Pack.objects.get(code="videos-4").is_public


def test_the_migration_leaves_an_operators_choices_alone(settings) -> None:
    _older_catalogue(settings)
    # The operator had already moved the floor and the badge, and re-published Pro.
    Plan.objects.filter(code="free").update(is_default=False)
    Plan.objects.filter(code="trial").update(is_default=True)
    Plan.objects.filter(code="pro").update(is_recommended=False)
    Plan.objects.filter(code="advanced").update(is_recommended=True)

    _run_0022()
    Plan.objects.filter(code="essential").update(tagline="Our words")
    _run_0022()

    assert Plan.objects.get(is_default=True).code == "trial"
    assert Plan.objects.get(is_recommended=True).code == "advanced"
    assert Plan.objects.get(code="essential").tagline == "Our words"
    assert Plan.objects.filter(code="essential").count() == 1


def test_the_migration_is_off_under_test_settings(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    before = Plan.objects.count()

    _run_0022()

    assert Plan.objects.count() == before


def test_0021_keeps_free_as_the_floor_where_no_seed_runs(settings) -> None:
    """The schema step's data half runs everywhere, so code reading
    `is_default` finds a floor even where seeding is off."""
    settings.SEED_CATALOG_ON_MIGRATE = True
    import_module("billing.migrations.0018_seed_catalog").forwards(apps, None)
    Plan.objects.update(is_default=False)
    settings.SEED_CATALOG_ON_MIGRATE = False

    import_module("billing.migrations.0021_plan_is_default").forwards(apps, None)

    assert Plan.objects.get(is_default=True).code == "free"


# -----------------------------------------------------------------------------
# Stripe: a price on the plan's price row maps back to the plan
# -----------------------------------------------------------------------------
def _event(workspace, price_id: str) -> dict:
    now = int(timezone.now().timestamp())
    return {
        "id": "evt_row_price",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_row_price",
                "status": "active",
                "customer": "cus_1",
                "cancel_at_period_end": False,
                "current_period_start": now,
                "current_period_end": now + 30 * 24 * 3600,
                "metadata": {"workspace_id": str(workspace.pk)},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


def test_a_price_pasted_on_the_price_row_maps_the_subscription_to_its_plan(
    workspace, plans
) -> None:
    """Checkout reads the row's id first (`pricing.plan_price`); the webhook
    must find the plan by the same id, or the payment lands nowhere."""
    plans["growth"].prices.update(stripe_price_id_monthly="price_growth_row")
    assert plans["growth"].stripe_price_id_monthly == ""

    webhooks.process_event(_event(workspace, "price_growth_row"))

    workspace.organization.refresh_from_db()
    assert workspace.organization.plan.code == "growth"


def test_checkout_and_webhook_agree_on_the_row_price(workspace, plans) -> None:
    from billing.gateways.fake import _fake_gateway

    plans["essential"].prices.update(stripe_price_id_annual="price_essential_year")

    subscriptions.start_checkout(
        workspace, plan_code="essential", cycle="annual", success_url="s", cancel_url="c"
    )
    sent = _fake_gateway.checkout_calls[-1]["price_id"]
    webhooks.process_event(_event(workspace, sent))

    workspace.organization.refresh_from_db()
    assert sent == "price_essential_year"
    assert workspace.organization.plan.code == "essential"
