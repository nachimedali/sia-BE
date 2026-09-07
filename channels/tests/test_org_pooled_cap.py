"""The connected-account cap is pooled at the organization (P1-14).

L-1: entitlement accounting pools at the company. A 5-account plan is five
accounts across the whole organization, not five per brand — otherwise a group
with eight workspaces quietly buys forty accounts on a plan that sells five,
and the cap that I6 calls *hard* is the one number in the system that scales
with how many workspaces someone creates.

The uniqueness half of P1-14 was already correct: `SocialAccount` has been
unique on `(workspace, platform, provider_account_id)` since Phase 9, so one
workspace could always connect two Instagram accounts. What was missing is that
the counter counted per workspace.
"""

from __future__ import annotations

from typing import Any

import pytest

from billing.services.entitlements import entitlements_for
from channels import services as channel_services
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import PaymentRequired
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def sibling(paid_workspace: Any, user: Any) -> Any:
    """A second workspace in the **same** organization — the shape L-1
    describes and the one the old per-workspace count could not see.

    Built through `provision_extra_workspace` rather than by hand, so the test
    can never assert against a workspace the application would not have made.
    """
    from workspaces.services.provisioning import provision_extra_workspace

    second = provision_extra_workspace(user=user, name="Second Brand")
    assert second.organization_id == paid_workspace.organization_id
    return second


def _connect(workspace: Any, index: int, platform: str = "instagram") -> SocialAccount:
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        handle=f"@acct-{index}",
        provider_account_id=f"acct-{workspace.pk}-{index}",
    )


def test_the_cap_counts_accounts_in_every_workspace_of_the_org(
    paid_workspace: Any, sibling: Any
) -> None:
    """The whole of P1-14. Pro allows five; four in one brand plus one in
    another is five, and the sixth is refused wherever it is attempted."""
    limit = entitlements_for(paid_workspace).quota("max_social_accounts")

    for index in range(limit - 1):
        _connect(paid_workspace, index)
    _connect(sibling, 99)

    with pytest.raises(PaymentRequired):
        channel_services.enforce_account_cap(sibling)


def test_a_second_workspace_does_not_double_the_allowance(
    paid_workspace: Any, sibling: Any
) -> None:
    limit = entitlements_for(paid_workspace).quota("max_social_accounts")
    for index in range(limit):
        _connect(paid_workspace, index)

    with pytest.raises(PaymentRequired):
        channel_services.enforce_account_cap(sibling)


def test_another_organizations_accounts_do_not_count(paid_workspace: Any, other_user: Any) -> None:
    """Pooling stops at the company. A stranger's connections must not consume
    this org's allowance — that would be a cross-tenant leak wearing a
    counter."""
    theirs = provision_workspace(other_user, name="Another Company")
    assert theirs.organization != paid_workspace.organization

    limit = entitlements_for(paid_workspace).quota("max_social_accounts")
    for index in range(limit + 3):
        _connect(theirs, index)

    channel_services.enforce_account_cap(paid_workspace)  # no raise


def test_the_cap_counts_rows_not_distinct_platforms(paid_workspace: Any) -> None:
    """Two Instagram accounts are two accounts. Counting platforms would make
    the cap unreachable for anyone with a second page."""
    limit = entitlements_for(paid_workspace).quota("max_social_accounts")
    for index in range(limit):
        _connect(paid_workspace, index, platform="instagram")

    with pytest.raises(PaymentRequired):
        channel_services.enforce_account_cap(paid_workspace)


def test_parking_over_the_cap_keeps_the_oldest_across_the_whole_org(
    paid_workspace: Any, sibling: Any, plans: Any
) -> None:
    """I10: mark, never delete, and the oldest connections survive — the ones
    the organization has actually been publishing from, wherever they live."""
    limit = entitlements_for(paid_workspace).quota("max_social_accounts")
    oldest = [_connect(paid_workspace, index) for index in range(limit)]
    newest = _connect(sibling, 99)

    channel_services.park_accounts_over_cap(sibling)

    newest.refresh_from_db()
    assert newest.status == SocialAccountStatus.OVER_LIMIT
    for account in oldest:
        account.refresh_from_db()
        assert account.status == SocialAccountStatus.ACTIVE


def test_parking_does_not_reach_into_another_organization(
    paid_workspace: Any, other_user: Any
) -> None:
    theirs = provision_workspace(other_user, name="Another Company")
    limit = entitlements_for(paid_workspace).quota("max_social_accounts")
    strangers = [_connect(theirs, index) for index in range(limit + 3)]

    for index in range(limit + 3):
        _connect(paid_workspace, index)
    channel_services.park_accounts_over_cap(paid_workspace)

    for account in strangers:
        account.refresh_from_db()
        assert account.status == SocialAccountStatus.ACTIVE


def test_publishing_still_reads_the_workspaces_own_accounts(
    paid_workspace: Any, sibling: Any
) -> None:
    """Pooling is about *counting*, not about *reach*. A post in one brand must
    never publish through another brand's account, however the allowance is
    shared."""
    mine = _connect(paid_workspace, 1)
    _connect(sibling, 2)

    publishable = list(channel_services.active_accounts(paid_workspace))
    assert [account.pk for account in publishable] == [mine.pk]
