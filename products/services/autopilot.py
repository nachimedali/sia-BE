"""The autopilot engine (design.md §8.7, I3; implementation.md Phase 12).

Hands-off drafting: for each enabled `AutopilotConfig`, keep the calendar full
of on-brand drafts out to `lookahead_days`, on a `cadence_days` rhythm.

**Slots are a grid, not a queue.** Every run computes the same slot times from
`config.created_at` and `cadence_days`, then drafts only the ones no draft
covers yet. Nothing is scheduled ahead into a broker, so a worker that was down
for a week backfills on its next run, a retune of the cadence takes effect
immediately, and a second scan racing the first collides on
`unique_autopilot_slot_per_product` rather than double-drafting. It is the same
due-based shape the metric ladder uses (design.md A119), for the same reasons.

**A run is marked by its `AutopilotJob` row**, including a blocked or failed
one. That is what stops a config that cannot proceed from being retried on
every scan — the failure mode the Phase 9 cleanup pass flagged for
`max_autopublish_posts`.

**Quota (I3).** Autopilot may spend the plan's *included* video allowance and
never a prepaid pack, so it counts against
`Entitlements.included_video_units_remaining()` rather than the video balance.
Hitting the wall stops the run with `BLOCKED_QUOTA` and leaves the drafts
already made — "stop and ask" rather than "spend and tell". Credits are the
same story: running out mid-run blocks rather than failing, because a
half-filled calendar is a correct outcome and a lost one is not.

**Video is gated here, generated in Phase 14.** A video slot consumes allowance
headroom and can block the run, but produces nothing yet — there is no
`VideoProvider` until Phase 14, and `AutopilotDraft.kind` declares `VIDEO` the
same way `PostStatus` declared its unreachable states in Phase 4 (design.md
A47). Deferred slots are counted in `AutopilotJob.detail` rather than skipped
silently.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Mapping, Sequence

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ai.models import Generation, GenerationKind, GenerationMode, GenerationStatus
from ai.services import pipeline
from ai.services.costing import resolve_cost
from billing.models import UNLIMITED
from billing.services.entitlements import Entitlements, entitlements_for
from common.exceptions import InsufficientCredits, OCCSError
from content.models import DeliveryMode, Platform, PostSource
from content.services.posts import create_post, update_post
from products.models import (
    AutopilotConfig,
    AutopilotDraft,
    AutopilotDraftKind,
    AutopilotDraftStatus,
    AutopilotJob,
    AutopilotJobStatus,
    AutopilotLanding,
    AutopilotStrategy,
)
from products.services.guards import ensure_generation_ready
from scheduling.services import schedule_post

logger = logging.getLogger(__name__)

#: What each strategy asks the generator for. Descriptive briefs, not prompts:
#: the grounding (voice, product, restrictions, category signal, the
#: workspace's own top performer) is `ai.services.prompting`'s job, and
#: duplicating any of it here would give it two places to drift.
STRATEGY_BRIEFS: Mapping[str, str] = {
    AutopilotStrategy.TREND_LED: (
        "Take the approach currently working in this category and apply it to this product."
    ),
    AutopilotStrategy.EVERGREEN: (
        "Something that will still make sense in six months — no dates, no launch, "
        "no seasonal hook."
    ),
    AutopilotStrategy.PRODUCT_LED: (
        "Put the product itself at the centre: what it is, how it is made, who it is for."
    ),
    AutopilotStrategy.PROMOTIONAL: (
        "A direct invitation to buy, with exactly one clear call to action."
    ),
}

#: `latitude` 0-100, as three bands. Bands rather than the raw number because
#: "wander 63% from the usual" is not an instruction a model can act on, while
#: "vary the presentation but keep it recognisable" is.
LATITUDE_BANDS: tuple[tuple[int, str], ...] = (
    (34, "Stay close to how this product is normally presented."),
    (67, "Vary the presentation, but keep it recognisably this product's."),
    (101, "Try an angle this product has not used before."),
)


class AutopilotNotConfigurableError(OCCSError):
    default_code = "autopilot_not_configurable"
    default_detail = "This draft cannot be acted on."


# -----------------------------------------------------------------------------
# Planning
# -----------------------------------------------------------------------------
def _latitude_line(latitude: int) -> str:
    return next(text for ceiling, text in LATITUDE_BANDS if latitude < ceiling)


def _weighted_pick(
    weights: Mapping[str, object], *, choices: Sequence[str], fallback: str, index: int
) -> str:
    """Deterministic weighted rotation.

    Deterministic, not random: a Beat task that produced a different calendar
    on a re-run would make every cadence test a coin flip, and an operator
    asking "why did it post that" deserves an answer that reproduces. Weights
    expand into a pool and the slot index walks it, so a 2:1 bias really does
    land twice as often over any full turn of the pool.
    """
    pool = [choice for choice in choices for _ in range(_weight(weights.get(choice)))]
    if not pool:
        return fallback
    return pool[index % len(pool)]


def _weight(value: object) -> int:
    """A weight out of user-supplied JSON. Anything that is not a usable count
    is zero — a malformed `strategy_weights` must fall back to the config's
    single `strategy`, never crash a Beat task."""
    if not isinstance(value, int | float | str):
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def planned_slots(
    config: AutopilotConfig, *, now: dt.datetime, horizon_days: int
) -> list[dt.datetime]:
    """Every posting time on this config's grid between `now` and the horizon.

    Anchored on `created_at` rather than on the run time: a run that happens an
    hour late must not shift the whole calendar an hour later, and two runs
    over the same window must agree on the slot times exactly or the uniqueness
    constraint stops being able to recognise a duplicate.
    """
    step = dt.timedelta(days=config.cadence_days)
    anchor = config.created_at
    end = now + dt.timedelta(days=horizon_days)

    first = math.floor((now - anchor) / step) + 1
    moments = []
    moment = anchor + first * step
    while moment <= end:
        moments.append(moment)
        moment += step
    return moments


def _horizon_days(config: AutopilotConfig, entitlements: Entitlements) -> int:
    """`lookahead_days`, clamped to what the plan can actually schedule.

    Without the clamp a Free-horizon workspace would draft posts that its own
    `require_scheduling_horizon` check refuses to schedule — drafts that can
    only ever be rejected.
    """
    horizon = entitlements.quota("scheduling_horizon_days")
    if horizon == UNLIMITED:
        return config.lookahead_days
    return min(config.lookahead_days, horizon)


def due_configs(*, now: dt.datetime) -> list[AutopilotConfig]:
    """Enabled configs whose last run is at least `cadence_days` old.

    The cadence is read from the jobs table rather than a `last_run_at` column
    so it cannot drift from the runs that actually happened.
    """
    candidates = (
        AutopilotConfig.objects.filter(enabled=True)
        .select_related("product", "product__workspace")
        .annotate(last_run=Max("jobs__run_at"))
    )
    due: list[AutopilotConfig] = []
    for config in candidates:
        last_run: dt.datetime | None = config.last_run
        if last_run is None or last_run <= now - dt.timedelta(days=config.cadence_days):
            due.append(config)
    return due


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------
def _generate(
    config: AutopilotConfig, *, kind: str, cost: int, brief: str, entitlements: Entitlements
) -> Generation:
    """One `mode=AUTOPILOT` generation, run through the ordinary pipeline.

    Bypasses `pipeline.create_generation`'s mode check the same way
    `ai.services.revisions` does, and validates the same preconditions in its
    place: the product is generation-ready (I7, checked once per run) and the
    workspace can afford the cost (I5's preflight — the debit inside
    `run_generation` is still the authoritative check).

    `cost` is resolved once per run by the caller rather than here: every slot
    in a run prices the same two `(kind, AUTOPILOT)` pairs, so re-resolving per
    generation would be the same `GenerationCost` lookup repeated for nothing.

    `is_batch=True` on both: D8 puts autopilot on the image Batch API, which is
    half the price and asynchronous, and autopilot is inherently asynchronous.
    """
    product = config.product
    entitlements.require_credits(cost)

    generation = Generation.objects.create(
        workspace=product.workspace,
        # The owner, not the operator: nobody pressed a button, and
        # `Generation.user` has to point at someone who still exists when the
        # ledger row it backs is audited.
        user=product.workspace.owner,
        kind=kind,
        mode=GenerationMode.AUTOPILOT,
        prompt=brief,
        product=product,
        category=product.workspace.category,
        is_batch=True,
    )
    return pipeline.run_generation(generation, n=1)


def _draft_slot(
    config: AutopilotConfig,
    *,
    slot: dt.datetime,
    strategy: str,
    platform: str,
    image_cost: int,
    text_cost: int,
    entitlements: Entitlements,
) -> AutopilotDraft | None:
    """Generates one slot's visual and caption, or `None` if the visual failed.

    The visual is generated first because it is the expensive half and the one
    the quality gate actually rejects — paying for a caption whose image never
    arrives would be the wrong order. A failed *caption* still yields a draft:
    an image with an empty caption is something the review queue can finish,
    and discarding it would throw away credits already spent on a picture that
    passed.
    """
    brief = "\n".join([STRATEGY_BRIEFS[strategy], _latitude_line(config.latitude)])

    visual = _generate(
        config, kind=GenerationKind.IMAGE, cost=image_cost, brief=brief, entitlements=entitlements
    )
    if visual.status != GenerationStatus.SUCCEEDED:
        return None

    caption = _generate(
        config, kind=GenerationKind.TEXT, cost=text_cost, brief=brief, entitlements=entitlements
    )
    body = ""
    if caption.status == GenerationStatus.SUCCEEDED:
        variant = caption.variants.first()
        body = variant.body if variant else ""

    return AutopilotDraft.objects.create(
        product=config.product,
        generation=visual,
        kind=AutopilotDraftKind.IMAGE,
        platform=platform,
        caption=body,
        scheduled_for=slot,
        strategy=strategy,
    )


def run_config(config: AutopilotConfig, *, now: dt.datetime | None = None) -> AutopilotJob:
    """One cadence tick for one config. Always returns a job row."""
    now = now or timezone.now()
    job = AutopilotJob.objects.create(config=config, run_at=now)
    product = config.product
    entitlements = entitlements_for(product.workspace)

    try:
        entitlements.require_feature("autopilot")
        ensure_generation_ready(product)
    except OCCSError as exc:
        job.status = AutopilotJobStatus.FAILED
        job.detail = {"reason": exc.code}
        job.save(update_fields=["status", "detail"])
        return job

    slots = planned_slots(config, now=now, horizon_days=_horizon_days(config, entitlements))
    taken = set(
        AutopilotDraft.objects.filter(product=product, scheduled_for__in=slots).values_list(
            "scheduled_for", flat=True
        )
    )
    platforms = _platform_rotation(config)
    video_headroom = entitlements.included_video_units_remaining()
    # Resolved once: every slot in this run prices the same two `(kind,
    # AUTOPILOT)` pairs, so re-resolving per generation would repeat the same
    # `GenerationCost` lookup for nothing.
    image_cost = resolve_cost(kind=GenerationKind.IMAGE, mode=GenerationMode.AUTOPILOT)
    text_cost = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.AUTOPILOT)

    drafts: list[AutopilotDraft] = []
    videos_deferred = 0
    blocked: str | None = None

    for index, slot in enumerate(slots):
        if slot in taken:
            continue
        strategy = _weighted_pick(
            config.strategy_weights,
            choices=AutopilotStrategy.values,
            fallback=config.strategy,
            index=index,
        )
        kind = _weighted_pick(
            config.format_mix,
            choices=AutopilotDraftKind.values,
            fallback=AutopilotDraftKind.IMAGE,
            index=index,
        )

        if kind == AutopilotDraftKind.VIDEO:
            # I3: the *included* allowance, never a pack the workspace bought.
            if video_headroom != UNLIMITED and videos_deferred >= video_headroom:
                blocked = "included_video_allowance"
                break
            videos_deferred += 1
            continue

        try:
            draft = _draft_slot(
                config,
                slot=slot,
                strategy=strategy,
                platform=platforms[index % len(platforms)],
                image_cost=image_cost,
                text_cost=text_cost,
                entitlements=entitlements,
            )
        except InsufficientCredits:
            blocked = "credits"
            break
        if draft is not None:
            drafts.append(draft)

    return _finish(
        job,
        config,
        entitlements=entitlements,
        drafts=drafts,
        videos_deferred=videos_deferred,
        blocked=blocked,
    )


def _platform_rotation(config: AutopilotConfig) -> list[str]:
    """Where the drafts go, narrowest declaration first: the config's own list,
    then the product's, then the workspace's, then the platform every account
    starts with."""
    product = config.product
    for candidate in (config.platforms, product.platforms, product.workspace.platforms):
        if candidate:
            return [str(value) for value in candidate]
    return [Platform.INSTAGRAM]


def _finish(
    job: AutopilotJob,
    config: AutopilotConfig,
    *,
    entitlements: Entitlements,
    drafts: list[AutopilotDraft],
    videos_deferred: int,
    blocked: str | None,
) -> AutopilotJob:
    # `auto_approve` was already gated when it was written; re-checked here
    # because a plan can be downgraded between configuring autopilot and the
    # run that acts on it (I5's shape).
    to_calendar = (
        config.auto_approve
        and config.landing == AutopilotLanding.AUTO_CALENDAR
        and bool(entitlements.feature("autopilot_auto_approve"))
    )
    reason = blocked
    landed = 0
    if to_calendar:
        for draft in drafts:
            try:
                approve_draft(draft, entitlements=entitlements)
            except OCCSError as exc:
                # Most likely no connected account yet (Phase 9's
                # `NoConnectedAccountsError`). The drafts are generated and paid
                # for either way, so they fall back to the review queue rather
                # than being lost — and the rest of the run is not abandoned for
                # a reason that would apply identically to every one of them.
                logger.warning(
                    "autopilot could not auto-schedule; leaving drafts for review",
                    extra={"config_id": config.pk, "code": exc.code},
                )
                reason = reason or exc.code
                break
            landed += 1

    if blocked is not None:
        job.status = AutopilotJobStatus.BLOCKED_QUOTA
    elif to_calendar and reason is None:
        job.status = AutopilotJobStatus.GENERATED
    else:
        # Including a failed auto-schedule: the drafts really are in the queue,
        # so that is what the status says, with `reason` explaining why they
        # did not go straight to the calendar.
        job.status = AutopilotJobStatus.QUEUED

    job.detail = {
        "drafts_created": len(drafts),
        "drafts_scheduled": landed,
        # Slots that resolved to video: gated now, generated in Phase 14.
        "videos_deferred": videos_deferred,
        **({"reason": reason} if reason else {}),
    }
    job.save(update_fields=["status", "detail"])
    return job


def run_due(*, now: dt.datetime | None = None) -> int:
    """Every config whose cadence is up. Returns the number of runs made.

    One failing config must not take the rest of the estate with it — this is
    the only loop in the phase that spans workspaces, so it is the only place
    where swallowing an exception is the right call.
    """
    now = now or timezone.now()
    ran = 0
    for config in due_configs(now=now):
        try:
            run_config(config, now=now)
        except Exception:
            logger.exception("autopilot run failed", extra={"config_id": config.pk})
            continue
        ran += 1
    return ran


# -----------------------------------------------------------------------------
# Review queue
# -----------------------------------------------------------------------------
@transaction.atomic
def approve_draft(
    draft: AutopilotDraft, *, entitlements: Entitlements | None = None
) -> AutopilotDraft:
    """Turns a draft into a real, scheduled `Post`.

    Delivery mode is resolved from the plan rather than assumed: autopilot
    needs Pro or better and every such plan has auto-publish today, but D4's
    reminders-only fallback is one plan edit away and this is not the place to
    hardcode which tier is which (I8).

    `entitlements` is an optional override: `_finish` auto-approving a whole
    run's worth of drafts for one workspace already has one resolved, and
    passing it through avoids re-resolving (and re-hitting the cache) once per
    draft. `AutopilotApproveView` has no such instance and leaves it to resolve
    here as before.
    """
    if draft.status not in {AutopilotDraftStatus.PENDING, AutopilotDraftStatus.APPROVED}:
        raise AutopilotNotConfigurableError(
            "This draft has already been acted on.",
            detail={"draft": draft.pk, "status": draft.status},
        )

    product = draft.product
    workspace = product.workspace
    entitlements = entitlements or entitlements_for(workspace)

    variant = draft.generation.variants.first()
    post = create_post(
        workspace=workspace,
        author=workspace.owner,
        master_body=draft.caption,
        category=workspace.category,
        media_assets=[variant.media_asset] if variant and variant.media_asset else [],
    )
    # Not settable through `create_post` — `PostSerializer` marks all three
    # read-only (A49) precisely so only the phase that owns them writes them.
    update_post(post, source=PostSource.AUTOPILOT, product=product, generation=draft.generation)

    mode = (
        DeliveryMode.AUTO_PUBLISH if entitlements.feature("auto_publish") else DeliveryMode.REMINDER
    )
    schedule_post(post=post, delivery_mode=mode, scheduled_at=draft.scheduled_for)

    draft.post = post
    draft.status = AutopilotDraftStatus.SCHEDULED
    draft.save(update_fields=["post", "status", "updated_at"])
    return draft


def reject_draft(draft: AutopilotDraft) -> AutopilotDraft:
    """Rejection is final for that slot, not just for that attempt: the
    uniqueness constraint keeps the row, so the next run sees the slot as
    covered and does not spend the credits again on something the user has
    already said no to."""
    if draft.status != AutopilotDraftStatus.PENDING:
        raise AutopilotNotConfigurableError(
            "This draft has already been acted on.",
            detail={"draft": draft.pk, "status": draft.status},
        )
    draft.status = AutopilotDraftStatus.REJECTED
    draft.save(update_fields=["status", "updated_at"])
    return draft


def update_config(config: AutopilotConfig, **fields: object) -> AutopilotConfig:
    """`PATCH /products/{id}/autopilot/`'s writer.

    `auto_approve` is Advanced-only (§4.1) and gated here as well as at run
    time, so it cannot be switched on and left looking active on a plan that
    will never honour it.
    """
    if fields.get("auto_approve"):
        entitlements_for(config.product.workspace).require_feature("autopilot_auto_approve")

    for name, value in fields.items():
        setattr(config, name, value)
    if fields:
        config.save(update_fields=[*fields.keys(), "updated_at"])
    return config
