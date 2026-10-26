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

from django.core import signing
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from billing.services.entitlements import entitlements_for
from channels.adapters import get_connect_adapter, provider_key
from channels.adapters.base import ConnectResolution
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import OCCSError, StateConflict
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
    """This workspace's own publishable accounts.

    **Reach, not accounting.** A post in one brand must never publish through
    another brand's account, however the allowance is shared — which is why
    `pooled_accounts` below is a separate function rather than this one taking
    a wider filter.
    """
    return SocialAccount.objects.filter(
        workspace=workspace, status=SocialAccountStatus.ACTIVE
    ).order_by("connected_at", "pk")


def pooled_accounts(workspace: Workspace) -> Any:
    """Every publishable account in the workspace's **organization** (P1-14).

    L-1: entitlement accounting pools at the company, so a five-account plan is
    five accounts across the whole organization and not five per brand. Counted
    per workspace, the one cap I6 calls *hard* would scale with how many
    workspaces someone created.

    Ordered oldest-first across the org, because that ordering is what decides
    who survives a downgrade — and the answer has to be "the connections the
    company has actually been publishing from", not "whoever is in this
    workspace".
    """
    return SocialAccount.objects.filter(
        workspace__organization_id=workspace.organization_id,
        status=SocialAccountStatus.ACTIVE,
    ).order_by("connected_at", "pk")


def enforce_account_cap(workspace: Workspace) -> None:
    """Refuses when connecting one more would exceed the organization's cap."""
    entitlements = entitlements_for(workspace)
    entitlements.check_quota(QUOTA_KEY, pooled_accounts(workspace).count() + 1)


def park_accounts_over_cap(workspace: Workspace) -> None:
    """Marks the newest accounts beyond the cap `OVER_LIMIT` (I10: mark, never
    delete). Called on downgrade; the oldest connections survive, because those
    are the ones the organization has been publishing from.

    Pooled like the cap it enforces — parking per workspace against an
    org-wide limit would leave the company over its cap with every workspace
    individually under it.
    """
    limit = entitlements_for(workspace).quota(QUOTA_KEY)
    over_cap = [account.pk for account in pooled_accounts(workspace)[limit:]]
    if not over_cap:
        return
    SocialAccount.objects.filter(pk__in=over_cap).update(
        status=SocialAccountStatus.OVER_LIMIT, updated_at=timezone.now()
    )
    logger.info(
        "parked social accounts over plan cap",
        extra={
            "organization_id": workspace.organization_id,
            "count": len(over_cap),
            "limit": limit,
        },
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
    # An id minted by one vendor is never sent to another (plan §1): an account
    # connected through the previous provider must be reconnected.
    provider = provider_key("publish")
    if account.provider_key and account.provider_key != provider:
        raise AccountNotPublishableError(
            "This account was connected through another publishing provider. Reconnect it "
            "to publish.",
            detail={
                "social_account": account.pk,
                "account_provider": account.provider_key,
                "provider": provider,
            },
        )


# -----------------------------------------------------------------------------
# Connect flow (headless — design.md §14, V1)
# -----------------------------------------------------------------------------
#: How long a sign-in may take, from the link to the return. Long enough for a
#: slow 2FA prompt, short enough that a leaked link is useless by tomorrow.
STATE_MAX_AGE_SECONDS = 30 * 60
STATE_SALT = "channels.connect.state"


class ConnectStateError(OCCSError):
    default_code = "connect_state_invalid"
    default_detail = "This sign-in is not for this workspace, or it took too long. Start it again."


def sign_state(
    *, workspace: Workspace, user: Any, platform: str, reconnect: int | None = None
) -> str:
    """A signed, expiring token naming who started this connect, where, for
    what. The provider echoes it back; `complete_connect` refuses a return
    that does not carry one matching the person and workspace finishing it —
    otherwise a link started in one workspace could finish in another.

    `reconnect` names the account a reauth is repairing, so the return is not
    refused at the cap for an account that already holds its slot."""
    data: dict[str, Any] = {"w": workspace.pk, "u": getattr(user, "pk", None), "p": platform}
    if reconnect is not None:
        data["r"] = reconnect
    return signing.dumps(data, salt=STATE_SALT)


def _load_state(token: str) -> Any:
    return signing.loads(token or "", salt=STATE_SALT, max_age=STATE_MAX_AGE_SECONDS)


def read_state(token: str) -> dict[str, Any]:
    """The state's contents if its signature and age hold, else `{}`. A hint
    for routing the return (which platform) — never the check itself, which
    is `verify_state` against workspace and person."""
    try:
        data = _load_state(token)
    except signing.BadSignature:
        return {}
    return data if isinstance(data, dict) else {}


def verify_state(token: str, *, workspace: Workspace, user: Any, platform: str) -> dict[str, Any]:
    """The state's contents, once it is shown to belong to this workspace,
    person and platform; `ConnectStateError` otherwise."""
    try:
        data = _load_state(token)
    except signing.SignatureExpired as error:
        raise ConnectStateError(
            "This sign-in took longer than 30 minutes. Start it again.",
            code="connect_state_expired",
        ) from error
    except signing.BadSignature as error:
        raise ConnectStateError() from error
    if not isinstance(data, dict) or (data.get("w"), data.get("u"), data.get("p")) != (
        workspace.pk,
        getattr(user, "pk", None),
        platform,
    ):
        raise ConnectStateError()
    return data


def _holds_slot(workspace: Workspace, account_id: Any) -> bool:
    """Whether a reconnect names an account the workspace still holds — a
    repair, which needs no new slot under the cap."""
    return (
        isinstance(account_id, int)
        and SocialAccount.objects.filter(pk=account_id, workspace=workspace)
        .exclude(status=SocialAccountStatus.REVOKED)
        .exists()
    )


def ensure_profile(workspace: Workspace) -> str:
    """The provider-side tenant for this workspace (a SocialAPI brand, a
    Zernio profile), minted on first use and stored at once — neither vendor
    makes creation idempotent, so the id must outlive any retry."""
    profile_id = get_connect_adapter().ensure_profile(
        name=workspace.slug, existing_profile_id=workspace.provider_profile_id
    )
    if profile_id != workspace.provider_profile_id:
        workspace.provider_profile_id = profile_id
        workspace.save(update_fields=["provider_profile_id", "updated_at"])
    return profile_id


def start_connect(
    *, workspace: Workspace, platform: str, redirect_url: str, user: Any = None
) -> str:
    """The URL to send the user to. The cap is checked here as well as at
    save time so a workspace at its limit is told before it authorises
    anything on the platform's side and has to be told afterwards."""
    enforce_account_cap(workspace)
    profile_id = ensure_profile(workspace)
    return get_connect_adapter().connect_url(
        platform=platform,
        profile_id=profile_id,
        redirect_url=redirect_url,
        state=sign_state(workspace=workspace, user=user, platform=platform),
    )


def resolve_connect(
    *, workspace: Workspace, platform: str, params: dict[str, Any]
) -> ConnectResolution:
    profile_id = ensure_profile(workspace)
    return get_connect_adapter().resolve_callback(
        platform=platform, profile_id=profile_id, params=params
    )


@transaction.atomic
def attach_account(*, workspace: Workspace, platform: str, connected: Any) -> SocialAccount:
    """Persists what the adapter connected. `update_or_create` on the model's
    own uniqueness triple, so re-running a connect for an account that is
    already here (a reauth, or an impatient second click) refreshes it instead
    of colliding — and clears any reconnect state it carried.

    The cap is re-checked inside the transaction even though `start_connect`
    already checked it: a whole OAuth round trip happened in between, and two
    connects finishing at once must not both be waved through by a count each
    read before either wrote. A reconnect of an account already here is not a
    new slot, so it is not re-counted.
    """
    existing = SocialAccount.objects.filter(
        workspace=workspace, platform=platform, provider_account_id=connected.provider_account_id
    ).first()
    if existing is None or existing.status == SocialAccountStatus.REVOKED:
        enforce_account_cap(workspace)
    # A fresh sign-in asks for every permission again, so what the last one
    # lacked (G13's insights) is unknown until the next capture shows it.
    capabilities = {
        key: value
        for key, value in (existing.capabilities if existing else {}).items()
        if key != "insights"
    }
    account, _created = SocialAccount.objects.update_or_create(
        workspace=workspace,
        platform=platform,
        provider_account_id=connected.provider_account_id,
        defaults={
            "handle": connected.handle,
            "display_name": connected.display_name,
            "followers_cached": connected.followers,
            "status": SocialAccountStatus.ACTIVE,
            "provider_key": provider_key("connect"),
            "reconnect_reason": "",
            "capabilities": capabilities,
        },
    )
    return account


def select_and_attach(
    *, workspace: Workspace, platform: str, params: dict[str, Any], target_id: str
) -> SocialAccount:
    profile_id = ensure_profile(workspace)
    connected = get_connect_adapter().select_target(
        platform=platform, profile_id=profile_id, params=params, target_id=target_id
    )
    return attach_account(workspace=workspace, platform=platform, connected=connected)


def complete_connect(
    *,
    workspace: Workspace,
    platform: str,
    params: dict[str, Any],
    target_id: str = "",
    user: Any = None,
) -> SocialAccount | ConnectResolution:
    """The whole of "finish a headless connect", in one call.

    Which leg this is — resolve the callback, or attach the target the user
    picked — is a property of the connect protocol, so it is decided here
    rather than by whichever caller happens to be holding the request. Returns
    the attached account, or the resolution still awaiting a choice.

    The `state` we signed is verified first whenever the provider echoes one
    (and required when the adapter says it always does), so a return can only
    finish in the workspace, for the person and the platform that started it.
    Then the cap (I6), ahead of any provider call — unless the state names an
    account being repaired, which already holds its slot.
    """
    token = str(params.get("state") or "")
    state: dict[str, Any] = {}
    if token or get_connect_adapter().requires_state:
        state = verify_state(token, workspace=workspace, user=user, platform=platform)
    if not _holds_slot(workspace, state.get("r")):
        enforce_account_cap(workspace)
    if target_id:
        return select_and_attach(
            workspace=workspace, platform=platform, params=params, target_id=target_id
        )

    resolution = resolve_connect(workspace=workspace, platform=platform, params=params)
    if resolution.needs_selection:
        return resolution
    return attach_account(workspace=workspace, platform=platform, connected=resolution.account)


def start_reauth(*, account: SocialAccount, redirect_url: str, user: Any = None) -> str:
    """Re-running OAuth for an account whose platform grant lapsed (or whose
    Page went away). The cap is deliberately not re-checked: this connects
    nothing new, and refusing to repair an account a workspace already has
    because it is at its limit would be exactly backwards."""
    profile_id = ensure_profile(account.workspace)
    return get_connect_adapter().connect_url(
        platform=account.platform,
        profile_id=profile_id,
        redirect_url=redirect_url,
        state=sign_state(
            workspace=account.workspace, user=user, platform=account.platform, reconnect=account.pk
        ),
    )


# -----------------------------------------------------------------------------
# Disconnect — and what happens to the posts that were going out through it
# -----------------------------------------------------------------------------
class ScheduledFate:
    #: Posts with nowhere else to go keep their time, as a reminder to post by hand.
    REMIND = "remind"
    #: Posts with nowhere else to go come off the calendar, approval kept.
    UNSCHEDULE = "unschedule"
    CHOICES = (REMIND, UNSCHEDULE)


def disconnect_impact(account: SocialAccount) -> dict[str, list[Any]]:
    """The auto-published posts this account is carrying, split by whether it
    is their only destination. Read-only — the panel shows it before anyone
    confirms."""
    from content.models import PostStatus, PostTarget, PostTargetState

    elsewhere = PostTarget.objects.filter(
        post_id=OuterRef("post_id"),
        social_account__isnull=False,
        state=PostTargetState.PENDING,
    ).exclude(social_account=account)
    targets = (
        PostTarget.objects.filter(
            social_account=account,
            state=PostTargetState.PENDING,
            post__status=PostStatus.SCHEDULED,
        )
        .annotate(elsewhere=Exists(elsewhere))
        .select_related("post")
        .order_by("post__scheduled_at", "post_id")
    )
    only: list[Any] = []
    shared: list[Any] = []
    for target in targets:
        (shared if target.elsewhere else only).append(target.post)
    return {"only_here": only, "also_elsewhere": shared}


@transaction.atomic
def disconnect(
    account: SocialAccount, *, actor: Any = None, scheduled: str = ScheduledFate.REMIND
) -> dict[str, Any]:
    """Tell the provider, then settle every scheduled post, then mark the
    account. All-or-nothing locally: a provider refusal changes nothing.

    * Posts whose **only** destination was this account follow `scheduled`:
      a reminder at the same time (through the schedule service, the one
      writer of delivery), or off the calendar with their approval kept. A
      post whose time has already passed is taken off rather than reminded.
    * Posts that **also** go elsewhere lose this destination and keep
      publishing to the others.
    * The account row stays (I10): its published posts and analytics remain.
    """
    from content.models import DeliveryMode, PostTarget, PostTargetState
    from scheduling.services import schedule_post, unschedule_post

    if scheduled not in ScheduledFate.CHOICES:
        raise OCCSError("Choose what happens to scheduled posts.", code="invalid_scheduled_fate")

    impact = disconnect_impact(account)
    get_connect_adapter().disconnect(provider_account_id=account.provider_account_id)

    reminded = unscheduled = 0
    now = timezone.now()
    for post in impact["only_here"]:
        when = post.scheduled_at
        post = unschedule_post(post, actor=actor)
        if scheduled == ScheduledFate.REMIND and when and when > now:
            try:
                # Its own savepoint: a reminder the plan refuses (a horizon, a
                # trial allowance) leaves the post safely off the calendar
                # rather than failing the whole disconnect.
                with transaction.atomic():
                    schedule_post(
                        post=post,
                        delivery_mode=DeliveryMode.REMINDER,
                        scheduled_at=when,
                        actor=actor,
                    )
                reminded += 1
                continue
            except OCCSError:
                logger.info("reminder refused on disconnect", extra={"post": post.pk})
        unscheduled += 1
    PostTarget.objects.filter(
        social_account=account,
        state=PostTargetState.PENDING,
        post__in=[post.pk for post in impact["also_elsewhere"]],
    ).delete()

    account.status = SocialAccountStatus.REVOKED
    account.save(update_fields=["status", "updated_at"])
    counts = {
        "reminded": reminded,
        "unscheduled": unscheduled,
        "kept_elsewhere": len(impact["also_elsewhere"]),
    }
    logger.info("social account disconnected", extra={"social_account": account.pk, **counts})
    return {"account": account, **counts}


def set_account_status(account: SocialAccount, status: str, reason: str) -> bool:
    """Moves the account to `status` and tells its admins — only on a change,
    so a retried webhook or a second "Check status" never notifies twice."""
    from notifications.services import account_event

    if account.status == status:
        return False
    account.status = status
    account.reconnect_reason = reason[:280]
    account.save(update_fields=["status", "reconnect_reason", "updated_at"])
    account_event(account, reason=reason)
    return True


GONE_REASON = "The provider no longer has this account. Reconnect it to keep publishing."
OTHER_PROVIDER_REASON = "This account was connected through another publishing provider."


def check_account(account: SocialAccount) -> bool | None:
    """The "Check status" button: asks the provider whether it still holds an
    active account. Returns whether it does, or None when nothing was asked —
    an account already out of service is fixed by reconnecting, not by asking.
    A provider that cannot answer raises `PlatformError` and changes nothing."""
    if account.status != SocialAccountStatus.ACTIVE:
        return None
    if account.provider_key and account.provider_key != provider_key("connect"):
        set_account_status(account, SocialAccountStatus.REAUTH_REQUIRED, OTHER_PROVIDER_REASON)
        return None
    present = get_connect_adapter().account_present(
        profile_id=account.workspace.provider_profile_id,
        provider_account_id=account.provider_account_id,
    )
    if not present:
        set_account_status(account, SocialAccountStatus.REAUTH_REQUIRED, GONE_REASON)
    return present
