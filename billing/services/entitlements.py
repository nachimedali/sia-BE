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

from billing.models import QUOTA_FIELDS, UNLIMITED, Plan, Subscription
from billing.services import ledger
from common.exceptions import (
    FeatureNotAvailable,
    InsufficientCredits,
    InsufficientVideoUnits,
    QuotaExceeded,
)
from common.redis import get_redis
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
FREE_PLAN_CODE = "free"

# What to suggest when a gate closes. Free → Pro covers everything except the
# Advanced-only features, which name themselves.
ADVANCED_ONLY = frozenset({"approval_workflow", "api_access", "autopilot_auto_approve"})


def _cache_key(workspace: Workspace, plan: Plan) -> str:
    # `updated_at` in the key is the invalidation: an admin edit mints a new key
    # rather than requiring every workspace on the plan to be hunted down.
    stamp = int(plan.updated_at.timestamp() * 1_000_000)
    return f"entitlements:{workspace.pk}:{plan.pk}:{stamp}"


class Entitlements:
    """Everything the caller is allowed to do, resolved once per request."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._snapshot: dict[str, Any] | None = None

    # --- resolution ------------------------------------------------------
    def _resolve_plan(self) -> Plan:
        plan = self.workspace.plan
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
        }
        try:
            client.set(key, json.dumps(snapshot), ex=CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("entitlement cache write failed", exc_info=True)

        self._snapshot = snapshot
        return snapshot

    # --- reads -----------------------------------------------------------
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

        return {
            **self.snapshot,
            "credits_remaining": self.credits_remaining(),
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
