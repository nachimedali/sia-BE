"""Organizations, workspaces, invites, add-ons and API keys (P0-46..P0-50).

The centre of gravity is **P0-G2**: a user creates a second workspace, invites
someone with no account, and is billed for two. That path crosses provisioning,
the gateway, the invitation token and account creation, and the test named for
it walks all of it end to end rather than asserting each piece in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from billing.gateways.fake import _fake_gateway
from billing.models import (
    AddonStatus,
    OrganizationAddon,
    Subscription,
    SubscriptionStatus,
)
from billing.services import subscriptions
from workspaces.models import ApiKey, Invitation, Membership, Role, Workspace, WorkspaceStatus
from workspaces.services import invitations as invitation_service

pytestmark = pytest.mark.django_db


def _subscribe(workspace: Any, organization: Any, *, plan: Any = None) -> Subscription:
    """An active subscription with both lines: the base, and the overage line
    the extra-workspace quantity moves on (P0-17)."""
    return Subscription.objects.create(
        workspace=workspace,
        organization=organization,
        plan=plan or workspace.organization.plan,
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_test_1",
        stripe_subscription_item_id="si_test_1",
        stripe_overage_item_id="si_overage_1",
    )


def _include(organization: Any, plan: Any, workspaces: int) -> None:
    """Puts the organization on `plan` with a known included allowance.

    Set explicitly rather than relying on the seeded number, so a commercial
    edit to Pro cannot silently change what these tests are asserting.
    """
    plan.max_workspaces = workspaces
    plan.save(update_fields=["max_workspaces"])
    organization.plan = plan
    organization.save(update_fields=["plan"])


# -----------------------------------------------------------------------------
# P0-G2 — the ship gate
# -----------------------------------------------------------------------------
def test_second_workspace_created_invited_and_billed_for_two(
    auth_client: Any, user: Any, workspace: Any, organization: Any, plans: Any, outbox: list[Any]
) -> None:
    """The gate, walked end to end.

    Every step is a place the whole thing could quietly half-work: a workspace
    with no organization, an invite to an address with no account, a quantity
    that never moved.

    "Billed for two" is **tiered**, not per-seat (P0-17: "tiered above
    `Plan.max_workspaces`"). On a plan including one workspace, the second is
    one unit of overage — the base line stays at the subscription itself and
    never moves, because charging it per workspace would bill for the ones the
    plan already includes.
    """
    _include(organization, plans["pro"], workspaces=1)
    _subscribe(workspace, organization, plan=plans["pro"])

    created = auth_client.post(reverse("workspaces"), {"name": "Second Brand"}, format="json")
    assert created.status_code == 201

    second = Workspace.objects.get(name="Second Brand")
    assert second.organization == organization
    # The quantity landed, so the workspace is writable rather than parked.
    assert second.status == WorkspaceStatus.ACTIVE
    assert _fake_gateway.quantity_updates[-1] == ("si_overage_1", 1)

    invited = auth_client.post(
        reverse("workspace-invite", args=[second.pk]),
        {"email": "newcomer@example.com", "role": Role.EDITOR},
        format="json",
    )
    assert invited.status_code == 201
    assert outbox[-1].to == "newcomer@example.com"

    # The invitee has no account at all — the case the gate names.
    assert not get_user_model().objects.filter(email="newcomer@example.com").exists()

    raw = _raw_token_for("newcomer@example.com")
    accepted = auth_client.post(
        reverse("invite-accept", args=[raw]), {"password": "a-good-password"}, format="json"
    )
    assert accepted.status_code == 201

    newcomer = get_user_model().objects.get(email="newcomer@example.com")
    assert Membership.objects.filter(user=newcomer, workspace=second).exists()
    assert organization.memberships.filter(user=newcomer).exists()


def _raw_token_for(email: str) -> str:
    """The raw token is never stored, so a test has to mint one it knows.

    Re-issuing supersedes the emailed invitation, which is exactly the
    behaviour under test elsewhere — here it is simply how the test gets a
    value it can send.
    """
    invitation = Invitation.objects.get(email=email)
    _fresh, raw = Invitation.issue(
        workspace=invitation.workspace,
        email=invitation.email,
        role=invitation.role,
        invited_by=invitation.invited_by,
    )
    return raw


# -----------------------------------------------------------------------------
# P0-17 — the plan covers its allowance; only the excess is billed
# -----------------------------------------------------------------------------
def test_a_workspace_inside_the_allowance_is_not_charged(
    auth_client: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    """The bug this fixes: billing the total count charged 3 x $37 for what
    Pro's own pricing page calls $37."""
    _include(organization, plans["pro"], workspaces=3)
    _subscribe(workspace, organization, plan=plans["pro"])

    created = auth_client.post(reverse("workspaces"), {"name": "Second Brand"}, format="json")

    assert created.status_code == 201
    assert Workspace.objects.get(name="Second Brand").status == WorkspaceStatus.ACTIVE
    # Nothing to charge, so nothing was asked of the gateway at all.
    assert _fake_gateway.quantity_updates == []


def test_only_the_excess_is_billed(
    user: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    _include(organization, plans["pro"], workspaces=3)
    for name in ("Second", "Third", "Fourth", "Fifth"):
        Workspace.objects.create(
            organization=organization,
            name=name,
            slug=Workspace.unique_slug(name),
        )

    # Five workspaces, three included.
    assert subscriptions.live_workspace_count(organization) == 5
    assert subscriptions.included_workspaces(organization) == 3
    assert subscriptions.billable_overage(organization) == 2


def test_an_unlimited_plan_never_bills_an_overage(
    user: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    _include(organization, plans["advanced"], workspaces=-1)
    for name in ("Second", "Third"):
        Workspace.objects.create(
            organization=organization,
            name=name,
            slug=Workspace.unique_slug(name),
        )

    assert subscriptions.billable_overage(organization) == 0


def test_a_parked_workspace_stops_being_billed(
    user: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    """`OVER_LIMIT` is read-only after a downgrade, and read-only must not keep
    charging."""
    _include(organization, plans["pro"], workspaces=1)
    Workspace.objects.create(
        organization=organization,
        name="Second",
        slug=Workspace.unique_slug("Second"),
        status=WorkspaceStatus.OVER_LIMIT,
    )

    assert subscriptions.billable_overage(organization) == 0


def test_an_overage_with_no_line_to_charge_it_on_parks_rather_than_refuses(
    auth_client: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    """A configuration error, not a customer error (P0-18)."""
    _include(organization, plans["pro"], workspaces=1)
    subscription = _subscribe(workspace, organization, plan=plans["pro"])
    Subscription.objects.filter(pk=subscription.pk).update(stripe_overage_item_id="")

    created = auth_client.post(reverse("workspaces"), {"name": "Second Brand"}, format="json")

    assert created.status_code == 201
    assert Workspace.objects.get(name="Second Brand").status == WorkspaceStatus.PENDING_BILLING


# -----------------------------------------------------------------------------
# P0-18 — a failed quantity update parks, never refuses
# -----------------------------------------------------------------------------
def test_a_refused_quantity_update_leaves_the_workspace_pending_not_absent(
    auth_client: Any, workspace: Any, organization: Any, plans: Any
) -> None:
    """The webhook is the source of truth for what was granted. A workspace the
    customer asked for and cannot see is worse than one they can see and cannot
    yet write to."""
    _include(organization, plans["pro"], workspaces=1)
    _subscribe(workspace, organization, plan=plans["pro"])
    _fake_gateway.fail_quantity_update = True

    response = auth_client.post(reverse("workspaces"), {"name": "Third Brand"}, format="json")

    assert response.status_code == 201
    assert Workspace.objects.get(name="Third Brand").status == WorkspaceStatus.PENDING_BILLING


def test_an_organization_with_no_subscription_activates_immediately(
    auth_client: Any, workspace: Any, organization: Any
) -> None:
    """A trial org has real, usable workspaces and no quantity to move."""
    response = auth_client.post(reverse("workspaces"), {"name": "Trial Brand"}, format="json")

    assert response.status_code == 201
    assert Workspace.objects.get(name="Trial Brand").status == WorkspaceStatus.ACTIVE


# -----------------------------------------------------------------------------
# P0-23 — downgrade parks the newest, oldest survive
# -----------------------------------------------------------------------------
def test_downgrade_parks_the_newest_workspaces(
    user: Any, workspace: Any, organization: Any, plans: dict[str, Any]
) -> None:
    """Same rule as the social-account cap: the oldest carry the history, the
    connected accounts and the published posts, so keeping the newest would
    park the ones that matter."""
    for name in ("Second", "Third"):
        Workspace.objects.create(
            organization=organization,
            name=name,
            slug=Workspace.unique_slug(name),
        )
    organization.plan = plans["pro"]
    organization.plan.max_workspaces = 1
    organization.plan.save(update_fields=["max_workspaces"])
    organization.save(update_fields=["plan"])

    parked = subscriptions.park_workspaces_over_cap(organization)

    assert parked == 2
    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.ACTIVE
    assert Workspace.objects.get(name="Third").status == WorkspaceStatus.OVER_LIMIT


def test_nothing_is_deleted_by_a_downgrade(
    user: Any, workspace: Any, organization: Any, plans: dict[str, Any]
) -> None:
    """Read-only, never gone. A downgrade that removed a brand's content would
    be indistinguishable from data loss."""
    Workspace.objects.create(
        organization=organization,
        name="Second",
        slug=Workspace.unique_slug("Second"),
    )
    organization.plan = plans["pro"]
    organization.plan.max_workspaces = 1
    organization.plan.save(update_fields=["max_workspaces"])
    organization.save(update_fields=["plan"])

    subscriptions.park_workspaces_over_cap(organization)

    assert Workspace.objects.filter(organization=organization).count() == 2


def test_re_upgrading_releases_what_a_downgrade_parked(
    user: Any, workspace: Any, organization: Any, plans: dict[str, Any]
) -> None:
    Workspace.objects.create(
        organization=organization,
        name="Second",
        slug=Workspace.unique_slug("Second"),
        status=WorkspaceStatus.OVER_LIMIT,
    )
    organization.plan = plans["advanced"]
    organization.plan.max_workspaces = 10
    organization.plan.save(update_fields=["max_workspaces"])
    organization.save(update_fields=["plan"])

    subscriptions.park_workspaces_over_cap(organization)

    assert Workspace.objects.get(name="Second").status == WorkspaceStatus.ACTIVE


# -----------------------------------------------------------------------------
# Invitations — P0-16, P0-47
# -----------------------------------------------------------------------------
def test_only_the_token_hash_is_stored(workspace: Any) -> None:
    """The raw value exists once, in the email. A database leak must not be
    replayable into a workspace someone does not belong to."""
    invitation, raw = Invitation.issue(workspace=workspace, email="a@example.com", role=Role.VIEWER)

    assert raw not in str(invitation.token_hash)
    assert Invitation.objects.filter(token_hash=raw).count() == 0


def test_re_inviting_supersedes_the_earlier_link(workspace: Any) -> None:
    """Without this, re-inviting leaves the earlier link live, widening the
    window on an invitation that may have gone to a mistyped address."""
    _first, first_raw = Invitation.issue(
        workspace=workspace, email="a@example.com", role=Role.VIEWER
    )
    Invitation.issue(workspace=workspace, email="a@example.com", role=Role.VIEWER)

    assert Invitation.resolve(first_raw) is None


def test_an_expired_invitation_does_not_resolve(workspace: Any) -> None:
    import datetime as dt

    from django.utils import timezone

    invitation, raw = Invitation.issue(workspace=workspace, email="a@example.com", role=Role.VIEWER)
    invitation.expires_at = timezone.now() - dt.timedelta(seconds=1)
    invitation.save(update_fields=["expires_at"])

    assert Invitation.resolve(raw) is None


def test_accepting_twice_is_refused(workspace: Any) -> None:
    _invitation, raw = Invitation.issue(
        workspace=workspace, email="a@example.com", role=Role.VIEWER
    )
    invitation_service.accept(raw_token=raw, password="a-good-password")

    with pytest.raises(invitation_service.InvitationInvalidError):
        invitation_service.accept(raw_token=raw, password="a-good-password")


def test_an_existing_account_needs_no_password(workspace: Any, other_user: Any) -> None:
    _invitation, raw = Invitation.issue(
        workspace=workspace, email=other_user.email, role=Role.EDITOR
    )

    membership = invitation_service.accept(raw_token=raw)

    assert membership.user == other_user
    assert membership.permissions


def test_a_new_account_is_verified_by_accepting(workspace: Any) -> None:
    """Accepting an emailed invitation proves control of the address as
    convincingly as a verification link does."""
    _invitation, raw = Invitation.issue(
        workspace=workspace, email="fresh@example.com", role=Role.VIEWER
    )

    invitation_service.accept(raw_token=raw, password="a-good-password")

    assert get_user_model().objects.get(email="fresh@example.com").is_email_verified


def test_owner_cannot_be_invited(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(
        reverse("workspace-invite", args=[workspace.pk]),
        {"email": "a@example.com", "role": Role.OWNER},
        format="json",
    )

    assert response.status_code == 400


def test_inviting_into_a_workspace_you_do_not_belong_to_is_404(
    auth_client: Any, other_user: Any
) -> None:
    """Part 7 rule 3. A 403 would confirm the workspace exists."""
    from workspaces.services.provisioning import provision_workspace

    theirs = provision_workspace(other_user, name="Not Yours")

    response = auth_client.post(
        reverse("workspace-invite", args=[theirs.pk]),
        {"email": "a@example.com", "role": Role.VIEWER},
        format="json",
    )

    assert response.status_code == 404


def test_an_unknown_invite_token_is_404_not_400(client: Any) -> None:
    """An invalid token and a token that never existed are the same answer;
    distinguishing them tells an attacker which guesses were close."""
    response = client.post(
        reverse("invite-accept", args=["not-a-real-token"]),
        data="{}",
        content_type="application/json",
    )

    assert response.status_code == 404


# -----------------------------------------------------------------------------
# Reads — P0-46
# -----------------------------------------------------------------------------
def test_organizations_lists_only_the_callers_own(
    auth_client: Any, organization: Any, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    provision_workspace(other_user, name="Someone Else")

    response = auth_client.get(reverse("organizations"))

    assert [row["id"] for row in response.json()] == [organization.pk]


def test_the_organization_row_carries_its_workspace_count(
    auth_client: Any, user: Any, organization: Any, workspace: Any
) -> None:
    Workspace.objects.create(
        organization=organization,
        name="Second",
        slug=Workspace.unique_slug("Second"),
    )

    assert auth_client.get(reverse("organizations")).json()[0]["workspace_count"] == 2


# -----------------------------------------------------------------------------
# Add-ons — P0-49
# -----------------------------------------------------------------------------
def test_enabling_an_addon_through_the_api(auth_client: Any, organization: Any) -> None:
    response = auth_client.post(
        reverse("billing-addons"), {"addon_key": "extra_seats", "enabled": True}, format="json"
    )

    assert response.status_code == 200
    assert OrganizationAddon.objects.get(addon_key="extra_seats").status == AddonStatus.ACTIVE


def test_disabling_an_addon_through_the_api(auth_client: Any, organization: Any) -> None:
    auth_client.post(
        reverse("billing-addons"), {"addon_key": "extra_seats", "enabled": True}, format="json"
    )

    auth_client.post(
        reverse("billing-addons"), {"addon_key": "extra_seats", "enabled": False}, format="json"
    )

    assert OrganizationAddon.objects.get(addon_key="extra_seats").status == AddonStatus.CANCELLED


# -----------------------------------------------------------------------------
# API keys — P0-50
# -----------------------------------------------------------------------------
def test_a_key_is_returned_once_and_never_again(auth_client: Any, organization: Any) -> None:
    created = auth_client.post(
        reverse("api-keys"), {"name": "CI", "scopes": ["view", "publish"]}, format="json"
    )

    assert created.status_code == 201
    raw = created.json()["key"]
    assert raw.startswith("cv_")

    listed = auth_client.get(reverse("api-keys")).json()
    assert "key" not in listed[0]
    assert listed[0]["prefix"] == raw[:8]


def test_only_the_key_hash_is_stored(organization: Any) -> None:
    key, raw = ApiKey.issue(organization=organization, name="CI", scopes=["view"])

    assert key.key_hash == ApiKey.hash_key(raw)
    assert raw not in key.key_hash


def test_an_unknown_scope_is_refused(auth_client: Any, organization: Any) -> None:
    response = auth_client.post(
        reverse("api-keys"), {"name": "CI", "scopes": ["do-anything"]}, format="json"
    )

    assert response.status_code == 400


def test_scopes_carry_no_implicit_hierarchy(organization: Any) -> None:
    """A machine credential does exactly what was typed. Inferring that
    `admin` implies `publish` would grant something nobody wrote down."""
    key, _raw = ApiKey.issue(organization=organization, name="CI", scopes=["admin"])

    assert key.allows("admin") is True
    assert key.allows("publish") is False


def test_a_revoked_key_allows_nothing(organization: Any) -> None:
    from django.utils import timezone

    key, _raw = ApiKey.issue(organization=organization, name="CI", scopes=["view"])
    key.revoked_at = timezone.now()

    assert key.allows("view") is False
