"""The entitlement resolver (design.md §8.1, I5).

One resolver. Never a scattered `if plan == "advanced"` — that pattern is how a
feature ends up gated in three places and ungated in a fourth.

**Resolution order:** `Workspace.plan` → trial override → cache.

The trial override is the defensive half. A workspace on a trial carries the
paid plan on `Workspace.plan` and gets full entitlements; when the trial lapses
without payment, the Beat task downgrades it. Between lapse and that task
running, the plan row still says "pro" — so the resolver checks the clock itself
and resolves Free. Entitlements must not depend on a periodic task having run.

**Caching:** the plan-derived snapshot is cached in Redis for 300s, keyed by the
plan's `updated_at`. Editing a quota in admin changes that timestamp, so the old
key is orphaned and the next request reads the new value — which is what
`test_quota_edit_visible_in_entitlements_immediately` (I5) asserts. Balances are
never cached: they change on every debit, and a stale balance would authorise a
spend the workspace cannot cover.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from typing import Any

from django.utils import timezone

from billing.models import (
    QUOTA_FIELDS,
    UNLIMITED,
    AddonStatus,
    OrganizationAddon,
    PackKind,
    Plan,
    Subscription,
)
from billing.services import ledger
from common.exceptions import (
    AddonNotEnabled,
    FeatureNotAvailable,
    InsufficientCredits,
    InsufficientVideoUnits,
    QuotaExceeded,
    SoftBudgetExceeded,
)
from common.redis import get_redis
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
FREE_PLAN_CODE = "free"

# What to suggest when a gate closes. Free → Pro covers everything except the
# Advanced-only features, which name themselves.
ADVANCED_ONLY = frozenset({"approval_workflow", "api_access", "autopilot_auto_approve"})


def _addon_version(organization: Any) -> str:
    """A stamp that changes whenever this org's add-on set does (P0-24).

    Part of the cache key rather than a separate invalidation step, for the
    same reason `plan.updated_at` is: enabling an add-on mints a new key, so
    nothing has to hunt down and evict the old one.

    Read from the counter column, never aggregated. This runs on **every**
    resolve, and an aggregate here would put a Postgres round-trip in front of
    the cache whose whole purpose is to avoid one.
    """
    return "-" if organization is None else str(organization.addon_version)


def _org_plan(workspace: Workspace) -> Plan | None:
    organization = getattr(workspace, "organization", None)
    return organization.plan if organization is not None else None


def _cache_key(workspace: Workspace, plan: Plan) -> str:
    # `updated_at` in the key is the invalidation: an admin edit mints a new key
    # rather than requiring every workspace on the plan to be hunted down. The
    # add-on stamp is the second axis (P0-24) — org plan crossed with org add-ons.
    stamp = int(plan.updated_at.timestamp() * 1_000_000)
    addons = _addon_version(getattr(workspace, "organization", None))
    return f"entitlements:{workspace.pk}:{plan.pk}:{stamp}:{addons}"


class Entitlements:
    """Everything the caller is allowed to do, resolved once per request."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._snapshot: dict[str, Any] | None = None

    # --- resolution ------------------------------------------------------
    def _resolve_plan(self) -> Plan:
        # **Still the workspace copy** — this is the dual-write step, not the
        # cut-over (P0-54). The organization is where the plan lives after
        # P0-55, and `billing.services.plans.set_plan` already writes both, but
        # reads do not move until a full billing cycle of parity has been
        # asserted in production. Swapping the order here early is exactly the
        # shortcut the migration discipline exists to prevent.
        plan = self.workspace.plan or _org_plan(self.workspace)
        if plan is None:
            return _free_plan()

        if not self._trial_has_lapsed(plan):
            return plan

        # On a trial that has run out with nothing to bill against.
        logger.info(
            "trial lapsed; resolving Free until the downgrade task runs",
            extra={"workspace_id": self.workspace.pk, "plan": plan.code},
        )
        return _free_plan()

    def _trial_has_lapsed(self, plan: Plan) -> bool:
        """True when the trial has run out and nothing has been paid."""
        ends_at = self.workspace.trial_ends_at
        if ends_at is None or ends_at > timezone.now():
            return False
        if plan.code == FREE_PLAN_CODE:
            return False
        # A live subscription means they converted; the trial end is history.
        return Subscription.current_for(self.workspace) is None

    @property
    def plan(self) -> Plan:
        if not hasattr(self, "_plan"):
            self._plan: Plan = self._resolve_plan()
        return self._plan

    @property
    def snapshot(self) -> dict[str, Any]:
        """The plan-derived half: features and quotas, cached."""
        if self._snapshot is not None:
            return self._snapshot

        key = _cache_key(self.workspace, self.plan)
        client = get_redis()
        try:
            cached = client.get(key)
            if cached:
                self._snapshot = json.loads(cached)
                return self._snapshot
        except Exception:
            # Redis being down must not take entitlements with it: resolving
            # from Postgres is correct, just slower.
            logger.warning("entitlement cache unavailable; resolving uncached", exc_info=True)

        snapshot = {
            "plan_code": self.plan.code,
            "plan_name": self.plan.display_name,
            "features": {key: self.plan.feature(key) for key in sorted(self.plan.features)},
            "quotas": {field: getattr(self.plan, field) for field in sorted(QUOTA_FIELDS)},
            "addons": sorted(self._active_addons()),
        }
        try:
            client.set(key, json.dumps(snapshot), ex=CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("entitlement cache write failed", exc_info=True)

        self._snapshot = snapshot
        return snapshot

    def _active_addons(self) -> set[str]:
        """Add-on keys live on this organization right now.

        An add-on inside its 30-day trial counts as active — that is what a
        trial is — and `expire_trials` is what ends it, not a read-time clock
        that would make the same request answer differently at 02:44 and 02:46.
        """
        organization = getattr(self.workspace, "organization", None)
        if organization is None:
            return set()
        return set(
            OrganizationAddon.objects.filter(
                organization=organization, status=AddonStatus.ACTIVE
            ).values_list("addon_key", flat=True)
        )

    # --- reads -----------------------------------------------------------
    def addons(self) -> set[str]:
        return set(self.snapshot.get("addons", []))

    def has_addon(self, key: str) -> bool:
        return key in self.addons()

    def soft_budget(self, field: str) -> int | None:
        """The workspace's optional self-imposed ceiling, or `None` when it has
        not set one. Distinct from a plan quota: the plan is what was bought,
        this is what this brand chose to spend of it."""
        value = getattr(self.workspace, field, None)
        return None if value is None else int(value)

    def feature(self, key: str) -> Any:
        return self.snapshot["features"].get(key, False)

    def quota(self, key: str) -> int:
        if key not in QUOTA_FIELDS:
            raise KeyError(f"Unknown quota '{key}'. Known quotas: {sorted(QUOTA_FIELDS)}.")
        return int(self.snapshot["quotas"][key])

    def credits_remaining(self) -> int:
        if self.quota("monthly_ai_credits") == UNLIMITED:
            return UNLIMITED
        return ledger.credit_balance(self.workspace)

    def video_units_remaining(self) -> int:
        if self.quota("included_videos") == UNLIMITED:
            return UNLIMITED
        return ledger.video_balance(self.workspace)

    def included_video_units_remaining(self) -> int:
        """What autopilot may spend (I3) — the allowance, never a prepaid pack.

        Separate from `video_units_remaining()` on purpose: a human asking for
        a video may spend anything the workspace holds, while the engine acting
        on its own may only spend what the plan included. One number each, so
        neither caller has to remember which pool it is entitled to.
        """
        if self.quota("included_videos") == UNLIMITED:
            return UNLIMITED
        return ledger.included_video_balance(self.workspace)

    def analytics_horizon_days(self) -> int:
        """How far back this plan keeps measurement (§4.1: 7 / 90 / 730).

        A typed accessor rather than four callers spelling
        `int(feature("analytics_history_days") or 0)` themselves — the
        module's own rule is one resolver, and the `or 0` coercion is exactly
        where four copies would eventually disagree. Zero means "no analytics
        history", which every reader treats as an empty window.
        """
        return int(self.feature("analytics_history_days") or 0)

    # --- audience engagement (L-4a, P0-34) -------------------------------
    def comment_capture_interval(self) -> dt.timedelta | None:
        """How often audience comments are polled, or `None` when this plan is
        driven by the `comment.received` webhook instead.

        `None` rather than "every minute": the provider caches both read
        endpoints for ten minutes and says plainly not to poll them, so the
        top tier subscribes. Tightening an interval towards real time would
        cost the vendor's goodwill and return the same cached page.
        """
        minutes = self.quota("comment_capture_interval_minutes")
        return None if minutes <= 0 else dt.timedelta(minutes=minutes)

    def reaction_detail(self) -> str:
        """How much of a reaction breakdown this plan may see.

        Degrades, never fails: a plan capped at `TOTAL` sees a real total, not
        a fabricated breakdown and not an error.
        """
        return str(self.plan.reaction_detail)

    # --- gates -----------------------------------------------------------
    def _suggested_plan(self, feature_key: str | None = None) -> str:
        if feature_key in ADVANCED_ONLY:
            return "advanced"
        return "pro" if self.plan.code == FREE_PLAN_CODE else "advanced"

    def require_feature(self, key: str) -> None:
        if not self.feature(key):
            raise FeatureNotAvailable(
                f"Your plan does not include {key.replace('_', ' ')}.",
                detail={"feature": key, "plan": self.plan.code},
                suggested_plan=self._suggested_plan(key),
            )

    def require_addon(self, key: str) -> None:
        """402 at the add-on (P0-25).

        A distinct code from `require_feature`: "your plan does not include
        this" and "your organization has not enabled this add-on" lead to
        different screens, and collapsing them would send a customer who
        already pays enough to an upgrade page that cannot help them.
        """
        if not self.has_addon(key):
            raise AddonNotEnabled(
                f"This organization has not enabled the {key.replace('_', ' ')} add-on.",
                detail={"addon": key},
                suggested_plan=self.plan.code,
            )

    def check_soft_budget(self, field: str, current: int) -> None:
        """402 with a code the UI can phrase as *this workspace's budget*
        rather than *your plan* (P0-25).

        The distinction is the whole point. A soft budget is self-imposed, so
        the fix is an admin in this workspace raising it — not a purchase — and
        an upgrade prompt here would be both wrong and annoying.
        """
        ceiling = self.soft_budget(field)
        if ceiling is not None and current >= ceiling:
            raise SoftBudgetExceeded(
                "This workspace has reached the budget it set for itself.",
                detail={"budget": field, "limit": ceiling, "current": current},
                suggested_plan=self.plan.code,
            )

    def _purchase_options(self, kind: str) -> list[dict[str, Any]]:
        """What is on sale that would clear this block, in this org's currency.

        Never allowed to be the reason a 402 fails: a catalogue read that
        raised here would turn "you need more credits" into a 500, which is a
        strictly worse answer to the same question.
        """
        from billing.services.pricing import purchase_options

        try:
            return purchase_options(self.workspace, kind=kind)
        except Exception:
            logger.warning("could not build purchase options", exc_info=True)
            return []

    def require_credits(self, amount: int) -> None:
        """Preflight only. The authoritative check is inside the debit's
        transaction — this one can be stale by the time the spend happens, and
        exists so the UI and the task can fail early and cheaply."""
        if self.quota("monthly_ai_credits") == UNLIMITED:
            return
        available = self.credits_remaining()
        if available < amount:
            raise InsufficientCredits(
                f"This action needs {amount} credits; {available} remaining.",
                detail={"required": amount, "available": available},
                suggested_plan=self._suggested_plan(),
                purchase=self._purchase_options(PackKind.CREDITS),
            )

    def require_video_units(self, amount: int) -> None:
        if self.quota("included_videos") == UNLIMITED:
            return
        available = self.video_units_remaining()
        if available < amount:
            raise InsufficientVideoUnits(
                f"This needs {amount} video unit(s); {available} remaining.",
                detail={"required": amount, "available": available},
                suggested_plan=self._suggested_plan(),
                purchase=self._purchase_options(PackKind.VIDEO),
            )

    def require_scheduling_horizon(self, scheduled_at: dt.datetime) -> None:
        """`implementation.md` Phase 8: `POST /posts/{id}/schedule/` rejects a
        date beyond `Plan.scheduling_horizon_days` with 402, not 400 — it is
        an entitlement the workspace can upgrade past, not a malformed
        request."""
        horizon_days = self.quota("scheduling_horizon_days")
        if horizon_days == UNLIMITED:
            return
        limit = timezone.now() + dt.timedelta(days=horizon_days)
        if scheduled_at > limit:
            raise QuotaExceeded(
                f"Your plan allows scheduling up to {horizon_days} days ahead.",
                detail={"quota": "scheduling_horizon_days", "limit_days": horizon_days},
                suggested_plan=self._suggested_plan(),
            )

    def check_quota(self, key: str, current: int) -> None:
        """Refuses when `current` is already at the cap — call it before
        creating the next one."""
        limit = self.quota(key)
        if limit == UNLIMITED:
            return
        if current >= limit:
            raise QuotaExceeded(
                f"Your plan allows {limit} of these; you have {current}.",
                detail={"quota": key, "limit": limit, "current": current},
                suggested_plan=self._suggested_plan(),
            )

    # --- serialisation ---------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """What `GET /billing/entitlements/` returns, and what the UI gates on
        before acting (design.md §10.5)."""
        ends_at = self.workspace.trial_ends_at
        remaining = ends_at - timezone.now() if ends_at else None
        is_trialing = remaining is not None and remaining.total_seconds() > 0

        from billing.services.trial import trial_posts_remaining

        return {
            **self.snapshot,
            "credits_remaining": self.credits_remaining(),
            "trial_posts_remaining": trial_posts_remaining(self.workspace),
            "video_units_remaining": self.video_units_remaining(),
            "trial_ends_at": ends_at,
            "is_trialing": is_trialing,
            # Counted here rather than in the browser: the server knows the
            # truth, and a skewed client clock would show the wrong number of
            # days left on the one screen where that number matters.
            "trial_days_remaining": (
                math.ceil(remaining.total_seconds() / 86_400)
                if is_trialing and remaining is not None
                else 0
            ),
        }


def _free_plan() -> Plan:
    plan = Plan.objects.filter(code=FREE_PLAN_CODE).first()
    if plan is None:
        # Only reachable if seed_plans never ran. Refusing outright beats
        # inventing an in-memory plan whose quotas nobody can audit.
        raise RuntimeError("The 'free' plan is missing. Run `manage.py seed_plans`.")
    return plan


def entitlements_for(workspace: Workspace) -> Entitlements:
    return Entitlements(workspace)


def bump_addon_version(organization: Any) -> None:
    """Invalidates every entitlement snapshot for this organization (P0-24).

    Called by the add-on write path, not by a signal — Part 7 rule 8, and more
    practically: a signal here would fire on fixtures and migrations too, and
    the one place that must never be missed is exactly the place a signal is
    hardest to reason about.
    """
    from django.db.models import F

    type(organization).objects.filter(pk=organization.pk).update(
        addon_version=F("addon_version") + 1
    )
    organization.refresh_from_db(fields=["addon_version"])
