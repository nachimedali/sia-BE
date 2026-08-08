"""Beat tasks and the admin guardrails (implementation.md Phase 3.4, 3.6, 3.8)."""

from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import connection
from django.http import HttpRequest
from django.utils import timezone

from billing import tasks
from billing.admin import CreditLedgerAdmin, PlanAdmin, SubscriptionAdmin
from billing.models import CreditLedger, CreditReason, Plan, Subscription, SubscriptionStatus
from billing.services import ledger

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(plans, user):
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


# -----------------------------------------------------------------------------
# Tasks — thin wrappers, so what is tested is that they are wired to the service
# -----------------------------------------------------------------------------
def test_grant_task_grants_and_then_stops(workspace) -> None:
    assert tasks.grant_monthly_allowances() == 1
    assert ledger.credit_balance(workspace) == 30
    assert tasks.grant_monthly_allowances() == 0


def test_trial_expiry_task_downgrades(workspace, plans) -> None:
    workspace.plan = plans["pro"]
    workspace.trial_ends_at = timezone.now() - dt.timedelta(days=1)
    workspace.save(update_fields=["plan", "trial_ends_at"])

    assert tasks.expire_trials() == 1

    workspace.refresh_from_db()
    assert workspace.plan.code == "free"


def test_reconcile_task_is_quiet_when_the_ledgers_agree(workspace) -> None:
    ledger.grant_credits(workspace, 30)
    ledger.debit_credits(workspace, 5, quota=30)

    assert tasks.reconcile_ledgers() == 0


def test_reconcile_task_counts_every_drifted_row(workspace) -> None:
    ledger.grant_credits(workspace, 30)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO billing_creditledger "
            "(workspace_id, delta, balance_after, reason, note, created_at) "
            "VALUES (%s, %s, %s, %s, '', clock_timestamp())",
            [workspace.pk, -1, 999, CreditReason.GENERATION],
        )

    assert tasks.reconcile_ledgers() == 1


def test_a_workspace_without_a_plan_is_skipped_rather_than_crashing(workspace) -> None:
    """Reachable when seed_plans has never run; the sweep must not stop."""
    workspace.plan = None
    workspace.save(update_fields=["plan"])

    assert tasks.grant_monthly_allowances() == 0


# -----------------------------------------------------------------------------
# Admin guardrails
# -----------------------------------------------------------------------------
@pytest.fixture
def site():
    return AdminSite()


def test_plan_code_is_read_only_once_it_exists(site, plans) -> None:
    admin = PlanAdmin(Plan, site)

    assert admin.get_readonly_fields(HttpRequest(), obj=None) == ()
    assert admin.get_readonly_fields(HttpRequest(), obj=plans["pro"]) == ("code",)


def test_a_plan_with_subscribers_cannot_be_deleted(site, workspace, plans) -> None:
    """Deprecate via `is_public=False` instead: existing subscribers keep their
    entitlements and the plan leaves the pricing page."""
    admin = PlanAdmin(Plan, site)
    unused = Plan.objects.create(code="legacy", display_name="Legacy")

    assert admin.has_delete_permission(HttpRequest(), obj=unused) is True
    # `workspace` is on Free, which therefore has a subscriber.
    assert admin.has_delete_permission(HttpRequest(), obj=plans["free"]) is False


def test_a_plan_referenced_only_by_a_subscription_cannot_be_deleted(site, workspace, plans) -> None:
    Subscription.objects.create(
        workspace=workspace,
        plan=plans["advanced"],
        status=SubscriptionStatus.CANCELED,
        stripe_subscription_id="sub_old",
    )

    assert (
        PlanAdmin(Plan, site).has_delete_permission(HttpRequest(), obj=plans["advanced"]) is False
    )


def test_plan_edits_are_logged_with_the_actor(site, plans, user, caplog) -> None:
    admin = PlanAdmin(Plan, site)
    plan = plans["pro"]
    plan.max_products = 11

    class Form:
        changed_data = ("max_products",)

    request = HttpRequest()
    request.user = user

    with caplog.at_level("WARNING"):
        admin.save_model(request, plan, Form(), change=True)

    assert "plan edited in admin" in caplog.text


def test_the_ledger_admin_is_read_only(site) -> None:
    admin = CreditLedgerAdmin(CreditLedger, site)

    assert admin.has_add_permission(HttpRequest()) is False
    assert admin.has_change_permission(HttpRequest()) is False
    assert admin.has_delete_permission(HttpRequest()) is False


def test_the_subscription_admin_is_read_only(site) -> None:
    """Stripe is the source of truth; an edit here is silently overwritten by
    the next webhook."""
    admin = SubscriptionAdmin(Subscription, site)

    assert admin.has_change_permission(HttpRequest()) is False


def test_the_adjust_action_appends_an_attributed_row(site, workspace, user, rf) -> None:
    """The one supported write, and it is still append-only."""
    admin = CreditLedgerAdmin(CreditLedger, site)
    ledger.grant_credits(workspace, 30)
    before = CreditLedger.objects.filter(workspace=workspace).count()

    request = rf.post("/admin/", {"apply": "1", "delta": "25", "note": "goodwill credit"})
    request.user = user
    request._messages = _CollectingMessages()

    admin.adjust_credits(request, CreditLedger.objects.filter(workspace=workspace))

    adjustment = CreditLedger.objects.filter(reason=CreditReason.MANUAL_ADJUST).get()
    assert adjustment.delta == 25
    assert adjustment.actor == user
    assert adjustment.note == "goodwill credit"
    assert CreditLedger.objects.filter(workspace=workspace).count() == before + 1
    assert ledger.credit_balance(workspace) == 55


def test_the_adjust_action_renders_its_form_before_applying(site, workspace, user, rf) -> None:
    admin = CreditLedgerAdmin(CreditLedger, site)
    ledger.grant_credits(workspace, 30)

    request = rf.post("/admin/", {})
    request.user = user

    response = admin.adjust_credits(request, CreditLedger.objects.filter(workspace=workspace))

    assert response.status_code == 200
    assert CreditLedger.objects.filter(reason=CreditReason.MANUAL_ADJUST).count() == 0


def test_the_adjust_action_refuses_an_empty_selection(site, user, rf) -> None:
    admin = CreditLedgerAdmin(CreditLedger, site)
    request = rf.post("/admin/", {})
    request.user = user
    request._messages = _CollectingMessages()

    assert admin.adjust_credits(request, CreditLedger.objects.none()) is None


class _CollectingMessages:
    """Stands in for the messages framework, which needs middleware the bare
    RequestFactory does not run."""

    def add(self, level, message, extra_tags=""):
        return None
