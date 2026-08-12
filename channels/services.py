"""Connected-account lifecycle (design.md §6.2, §8.5; implementation.md Phase 9).

The account cap (I6) is enforced from here, and every layer that enforces it
calls into this module rather than re-deriving it — the serializer and the admin
through `enforce_account_cap` before saving, and the publish task through
`ensure_publishable`, which re-derives the same cap against the same
`active_accounts` ordering. One rule in one place: a cap that is
re-implemented per layer is a cap that disagrees with itself.

I6 also says the cap is a *hard* one, never unlimited — `billing.models.Plan.
validation_errors` refuses the unlimited sentinel for this quota, so
`check_quota` here can never be handed one to wave through.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from billing.services.entitlements import entitlements_for
from channels.adapters.base import ConnectResolution
from channels.adapters.zernio import get_platform_adapter
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import StateConflict
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

QUOTA_KEY = "max_social_accounts"


class AccountNotPublishableError(StateConflict):
    """The account exists but is not in a state that may publish — reauth
    needed, revoked, or parked over the plan's cap. A 409, not a 402: a
    `REAUTH_REQUIRED` account is not an upgrade away from working, and only
    the `OVER_LIMIT` case has anything to sell (`enforce_account_cap` raises
    the 402 for that one before this is ever reached)."""

    default_code = "account_not_publishable"
    default_detail = "This connected account cannot publish right now."


def active_accounts(workspace: Workspace) -> Any:
    return SocialAccount.objects.filter(
        workspace=workspace, status=SocialAccountStatus.ACTIVE
    ).order_by("connected_at", "pk")


def enforce_account_cap(workspace: Workspace) -> None:
    """Refuses when connecting one more would exceed the workspace's cap."""
    entitlements = entitlements_for(workspace)
    entitlements.check_quota(QUOTA_KEY, active_accounts(workspace).count() + 1)


def park_accounts_over_cap(workspace: Workspace) -> None:
    """Marks the newest accounts beyond the cap `OVER_LIMIT` (I10: mark, never
    delete). Called on downgrade; the oldest connections survive, because
    those are the ones the workspace has been publishing from."""
    limit = entitlements_for(workspace).quota(QUOTA_KEY)
    over_cap = [account.pk for account in active_accounts(workspace)[limit:]]
    if not over_cap:
        return
    SocialAccount.objects.filter(pk__in=over_cap).update(
        status=SocialAccountStatus.OVER_LIMIT, updated_at=timezone.now()
    )
    logger.info(
        "parked social accounts over plan cap",
        extra={"workspace_id": workspace.pk, "count": len(over_cap), "limit": limit},
    )


def ensure_publishable(account: SocialAccount) -> None:
    """The third I6 layer, and the last thing standing between a downgraded
    workspace and a post going out through an account it no longer pays for.

    Re-derives the cap rather than trusting `status`: a quota edited in admin
    (I8 makes that a supported operation) moves the cap without emitting any
    downgrade event, so a workspace can be over its limit with every account
    still marked `ACTIVE`. Parking here makes the next check — and the
    connections screen — agree with the plan.
    """
    park_accounts_over_cap(account.workspace)
    account.refresh_from_db(fields=["status"])
    if not account.is_publishable:
        raise AccountNotPublishableError(
            f"This {account.platform} account is {account.get_status_display().lower()}.",
            detail={"social_account": account.pk, "status": account.status},
        )


# -----------------------------------------------------------------------------
# Connect flow (headless — design.md §14, V1)
# -----------------------------------------------------------------------------
def ensure_profile(workspace: Workspace) -> str:
    """The provider-side tenant for this workspace, minted on first use."""
    profile_id = get_platform_adapter().ensure_profile(
        name=workspace.slug, existing_profile_id=workspace.provider_profile_id
    )
    if profile_id != workspace.provider_profile_id:
        workspace.provider_profile_id = profile_id
        workspace.save(update_fields=["provider_profile_id", "updated_at"])
    return profile_id


def start_connect(*, workspace: Workspace, platform: str, redirect_url: str) -> str:
    """The URL to send the user to. The cap is checked here as well as at
    save time so a workspace at its limit is told before it authorises
    anything on the platform's side and has to be told afterwards."""
    enforce_account_cap(workspace)
    profile_id = ensure_profile(workspace)
    return get_platform_adapter().connect_url(
        platform=platform, profile_id=profile_id, redirect_url=redirect_url
    )


def resolve_connect(
    *, workspace: Workspace, platform: str, params: dict[str, Any]
) -> ConnectResolution:
    profile_id = ensure_profile(workspace)
    return get_platform_adapter().resolve_callback(
        platform=platform, profile_id=profile_id, params=params
    )


@transaction.atomic
def attach_account(
    *, workspace: Workspace, platform: str, connected: Any
) -> SocialAccount:
    """Persists what the adapter connected. `update_or_create` on the model's
    own uniqueness triple, so re-running a connect for an account that is
    already here (a reauth, or an impatient second click) refreshes it instead
    of colliding.

    The cap is re-checked inside the transaction even though `start_connect`
    already checked it: a whole OAuth round trip happened in between, and two
    connects finishing at once must not both be waved through by a count each
    read before either wrote.
    """
    enforce_account_cap(workspace)
    account, _created = SocialAccount.objects.update_or_create(
        workspace=workspace,
        platform=platform,
        provider_account_id=connected.provider_account_id,
        defaults={
            "handle": connected.handle,
            "display_name": connected.display_name,
            "followers_cached": connected.followers,
            "status": SocialAccountStatus.ACTIVE,
        },
    )
    return account


def select_and_attach(
    *, workspace: Workspace, platform: str, params: dict[str, Any], target_id: str
) -> SocialAccount:
    profile_id = ensure_profile(workspace)
    connected = get_platform_adapter().select_target(
        platform=platform, profile_id=profile_id, params=params, target_id=target_id
    )
    return attach_account(workspace=workspace, platform=platform, connected=connected)


def complete_connect(
    *, workspace: Workspace, platform: str, params: dict[str, Any], target_id: str = ""
) -> SocialAccount | ConnectResolution:
    """The whole of "finish a headless connect", in one call.

    Which leg this is — resolve the callback, or attach the target the user
    picked — is a property of the connect protocol, so it is decided here
    rather than by whichever caller happens to be holding the request. Returns
    the attached account, or the resolution still awaiting a choice.
    """
    if target_id:
        return select_and_attach(
            workspace=workspace, platform=platform, params=params, target_id=target_id
        )

    resolution = resolve_connect(workspace=workspace, platform=platform, params=params)
    if resolution.needs_selection:
        return resolution
    return attach_account(workspace=workspace, platform=platform, connected=resolution.account)


def disconnect(account: SocialAccount) -> SocialAccount:
    """Tells the provider first, then marks. The row stays (I10: a disconnect
    is not a reason to lose the posts that went through it)."""
    get_platform_adapter().disconnect(provider_account_id=account.provider_account_id)
    account.status = SocialAccountStatus.REVOKED
    account.save(update_fields=["status", "updated_at"])
    return account


def start_reauth(*, account: SocialAccount, redirect_url: str) -> str:
    """Re-running OAuth for an account whose platform grant lapsed. The cap is
    deliberately not re-checked: this connects nothing new, and refusing to
    repair an account a workspace already has because it is at its limit would
    be exactly backwards."""
    profile_id = ensure_profile(account.workspace)
    return get_platform_adapter().connect_url(
        platform=account.platform, profile_id=profile_id, redirect_url=redirect_url
    )
