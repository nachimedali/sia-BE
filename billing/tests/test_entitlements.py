"""The entitlement resolver (design.md §8.1, I5, I6, I8)."""

from __future__ import annotations

import datetime as dt

import pytest
import time_machine
from django.utils import timezone

from billing.models import UNLIMITED, Subscription, SubscriptionStatus
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import (
    FeatureNotAvailable,
    InsufficientCredits,
    QuotaExceeded,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(plans, user):
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


def _on_plan(workspace, plans, code):
    workspace.plan = plans[code]
    workspace.save(update_fields=["plan", "updated_at"])
    return workspace


# -----------------------------------------------------------------------------
# I5 — a quota edit is visible on the next request
# -----------------------------------------------------------------------------
def test_quota_edit_visible_in_entitlements_immediately(workspace, plans) -> None:
    """I5. The cache exists to save a query, not to delay an operator.

    The key carries the plan's `updated_at`, so an admin edit orphans the old
    entry instead of requiring every workspace on the plan to be hunted down.
    """
    _on_plan(workspace, plans, "pro")
    assert entitlements_for(workspace).quota("max_products") == 10

    plan = plans["pro"]
    plan.max_products = 42
    plan.save()

    workspace.refresh_from_db()
    assert entitlements_for(workspace).quota("max_products") == 42


def test_feature_edit_visible_immediately(workspace, plans) -> None:
    _on_plan(workspace, plans, "pro")
    assert entitlements_for(workspace).feature("approval_workflow") is False

    plan = plans["pro"]
    plan.features = {**plan.features, "approval_workflow": True}
    plan.save()

    workspace.refresh_from_db()
    assert entitlements_for(workspace).feature("approval_workflow") is True


def test_snapshot_is_cached_between_resolvers(workspace, plans) -> None:
    """Two resolvers in one request-cycle must not both hit Postgres."""
    _on_plan(workspace, plans, "pro")
    entitlements_for(workspace).quota("max_products")

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        entitlements_for(workspace).quota("max_products")

    assert captured.captured_queries == []


def test_resolver_survives_redis_being_down(workspace, plans, monkeypatch) -> None:
    """Redis is a cache here, not a dependency. Losing it must degrade to a
    Postgres read, not to a 500."""
    _on_plan(workspace, plans, "pro")

    class DeadRedis:
        def get(self, key):
            raise ConnectionError("redis is down")

        def setex(self, *args):
            raise ConnectionError("redis is down")

    monkeypatch.setattr("billing.services.entitlements.get_redis", lambda: DeadRedis())

    assert entitlements_for(workspace).quota("max_products") == 10


# -----------------------------------------------------------------------------
# Gates
# -----------------------------------------------------------------------------
def test_free_plan_is_refused_the_paid_features(workspace, plans) -> None:
    entitlements = entitlements_for(_on_plan(workspace, plans, "free"))

    for feature in ("auto_publish", "playbook", "trend_engine", "video_generation", "autopilot"):
        with pytest.raises(FeatureNotAvailable) as excinfo:
            entitlements.require_feature(feature)
        assert excinfo.value.status_code == 402
        assert excinfo.value.upgrade["suggested_plan"] == "pro"


def test_advanced_only_features_suggest_advanced(workspace, plans) -> None:
    """A Pro user told to upgrade to Pro would be a dead end."""
    entitlements = entitlements_for(_on_plan(workspace, plans, "pro"))

    with pytest.raises(FeatureNotAvailable) as excinfo:
        entitlements.require_feature("approval_workflow")
    assert excinfo.value.upgrade["suggested_plan"] == "advanced"


def test_check_quota_refuses_at_the_cap_not_past_it(workspace, plans) -> None:
    entitlements = entitlements_for(_on_plan(workspace, plans, "free"))

    entitlements.check_quota("max_products", current=0)
    with pytest.raises(QuotaExceeded) as excinfo:
        entitlements.check_quota("max_products", current=1)

    assert excinfo.value.payload == {"quota": "max_products", "limit": 1, "current": 1}


def test_unlimited_quota_never_refuses(workspace, plans) -> None:
    entitlements = entitlements_for(_on_plan(workspace, plans, "advanced"))

    assert entitlements.quota("max_products") == UNLIMITED
    entitlements.check_quota("max_products", current=10_000)


def test_social_account_cap_is_never_unlimited_on_any_plan(workspace, plans) -> None:
    """I6, read through the resolver — this is the layer every caller asks, and
    the model-level `clean()` guard is only useful if it reaches here."""
    for code in ("free", "pro", "advanced"):
        cap = entitlements_for(_on_plan(workspace, plans, code)).quota("max_social_accounts")
        assert cap != UNLIMITED
        assert cap > 0


def test_unknown_quota_key_is_an_error_not_a_zero(workspace) -> None:
    """A typo'd quota resolving to 0 would silently refuse everything; one
    resolving to a default would silently allow everything."""
    with pytest.raises(KeyError):
        entitlements_for(workspace).quota("max_widgets")


def test_require_credits_reports_what_is_missing(workspace, plans) -> None:
    _on_plan(workspace, plans, "pro")
    ledger.grant_credits(workspace, 2)

    with pytest.raises(InsufficientCredits) as excinfo:
        entitlements_for(workspace).require_credits(3)

    assert excinfo.value.payload == {"required": 3, "available": 2}


# -----------------------------------------------------------------------------
# Trials
# -----------------------------------------------------------------------------
def test_a_live_trial_gets_the_full_paid_plan(workspace, plans) -> None:
    _on_plan(workspace, plans, "pro")
    workspace.trial_ends_at = timezone.now() + dt.timedelta(days=3)
    workspace.save(update_fields=["trial_ends_at"])

    entitlements = entitlements_for(workspace)

    assert entitlements.plan.code == "pro"
    assert entitlements.feature("auto_publish") is True
    assert entitlements.as_dict()["is_trialing"] is True


def test_a_lapsed_unpaid_trial_resolves_free_without_waiting_for_the_task(workspace, plans) -> None:
    """§8.1. Entitlements must not depend on a periodic task having run — the
    window between the trial lapsing and Beat firing is otherwise a free ride."""
    _on_plan(workspace, plans, "pro")
    workspace.trial_ends_at = timezone.now() - dt.timedelta(minutes=1)
    workspace.save(update_fields=["trial_ends_at"])

    entitlements = entitlements_for(workspace)

    assert entitlements.plan.code == "free"
    assert entitlements.feature("auto_publish") is False
    # The stored plan is untouched: the downgrade task owns that write.
    workspace.refresh_from_db()
    assert workspace.plan.code == "pro"


def test_a_lapsed_trial_that_converted_keeps_the_paid_plan(workspace, plans) -> None:
    _on_plan(workspace, plans, "pro")
    workspace.trial_ends_at = timezone.now() - dt.timedelta(days=1)
    workspace.save(update_fields=["trial_ends_at"])
    Subscription.objects.create(
        workspace=workspace,
        plan=plans["pro"],
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_live",
    )

    assert entitlements_for(workspace).plan.code == "pro"


@time_machine.travel("2026-08-04 12:00:00+00:00")
def test_entitlements_payload_is_what_the_ui_gates_on(workspace, plans) -> None:
    """design.md §10.5 — the UI renders locked states from this, so every field
    it needs has to be here."""
    _on_plan(workspace, plans, "pro")
    ledger.grant_credits(workspace, 150)
    ledger.grant_video_units(workspace, 4)

    payload = entitlements_for(workspace).as_dict()

    assert payload["plan_code"] == "pro"
    assert payload["credits_remaining"] == 150
    assert payload["video_units_remaining"] == 4
    assert payload["features"]["auto_publish"] is True
    assert payload["quotas"]["max_social_accounts"] == 5
    assert payload["is_trialing"] is False
