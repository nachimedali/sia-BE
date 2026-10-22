"""Which model answers a task, for a given package (pricing.plan.md, Phase 4).

The one reader of `AIModelRoute`. Resolution, in order:

1. the active row for `(task, plan)` — a package's override;
2. the active row for `(task, default)` — what every package inherits;
3. the settings the system ran on before the table existed
   (`LLM_DEFAULT_MODEL`, `IMAGE_PROVIDER_MODEL`, `VIDEO_DEFAULT_MODEL`).

Step 3 is what makes an empty table the previous behaviour exactly, which is
this feature's flag-off guarantee (Part 3, *Feature flags*).

A row is only eligible when its `provider` is the one this deployment points
the task's port at. A row written for another gateway is skipped — never sent
— so a model id from one vendor cannot reach another after a provider swap.

No cache: one indexed query per provider call, against a table of a few dozen
rows, beside calls that take seconds. If one is ever needed it keys on
`updated_at`, like the entitlement resolver.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db.models import F, Q

from ai.models import AIKind, AIModelRoute, kind_of
from common.exceptions import ProviderError

logger = logging.getLogger(__name__)


#: Where a resolved route came from — recorded in logs so "why did this run on
#: that model" has an answer.
SOURCE_PLAN = "plan"
SOURCE_DEFAULT = "default"
SOURCE_SETTINGS = "settings"


@dataclass(frozen=True)
class Route:
    task: str
    provider: str
    model: str
    fallback_model: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_SETTINGS


#: Per kind, the settings naming the vendor a port points at and the model it
#: used before routes existed. The text port's gateway is `LLM_BASE_URL`; the
#: label naming it is `LLM_PROVIDER`.
_SETTINGS: dict[str, tuple[str, str]] = {
    AIKind.TEXT: ("LLM_PROVIDER", "LLM_DEFAULT_MODEL"),
    AIKind.IMAGE: ("IMAGE_PROVIDER", "IMAGE_PROVIDER_MODEL"),
    AIKind.VIDEO: ("VIDEO_PROVIDER", "VIDEO_DEFAULT_MODEL"),
}


def configured_provider(kind: str) -> str:
    """The vendor this deployment points a port at."""
    return str(getattr(settings, _SETTINGS[kind][0]))


def _settings_model(kind: str) -> str:
    return str(getattr(settings, _SETTINGS[kind][1]))


def resolve(task: str, *, plan: Any = None) -> Route:
    kind = kind_of(task)
    provider = configured_provider(kind)
    scope = Q(plan__isnull=True) | (Q(plan=plan) if plan is not None else Q(pk__in=[]))
    rows = AIModelRoute.objects.filter(scope, task=task, is_active=True, provider=provider)
    # A package's own row beats the default: nulls sort last on `plan_id`.
    row = rows.order_by(F("plan_id").asc(nulls_last=True)).first()
    if row is None:
        return Route(task=task, provider=provider, model=_settings_model(kind))
    return Route(
        task=task,
        provider=row.provider,
        model=row.model,
        fallback_model=row.fallback_model,
        params=dict(row.params or {}),
        source=SOURCE_PLAN if row.plan_id else SOURCE_DEFAULT,
    )


def resolve_for(task: str, workspace: Any = None) -> Route:
    """The route for a workspace's package, or the default when there is no
    workspace in hand (bulk work on comments, rephrasing)."""
    plan = None
    if workspace is not None:
        from billing.services.entitlements import entitlements_for

        plan = entitlements_for(workspace).plan
    return resolve(task, plan=plan)


def is_rate_limited(exc: ProviderError) -> bool:
    return exc.payload.get("status") == 429


def call_with_fallback[T](route: Route, call: Callable[[str], T]) -> T:
    """`call(model)` on the primary, then once on the fallback.

    **Not on a rate limit.** The fallback runs on the same gateway and the
    same account, so a 429 on the primary is a 429 on the fallback; trying it
    spends a request and learns nothing.
    """
    try:
        return call(route.model)
    except ProviderError as exc:
        if not route.fallback_model or is_rate_limited(exc):
            raise
        logger.warning(
            "%s: %s/%s failed, trying fallback %s",
            route.task,
            route.provider,
            route.model,
            route.fallback_model,
            exc_info=True,
        )
        return call(route.fallback_model)
