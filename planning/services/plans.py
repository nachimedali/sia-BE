"""The campaign plan and seeding the calendar (steps-plan S5, S5-02 and S5-03).

```
estimate   ready brief → priced lines + an estimate id        (nothing is held)
start      re-price, compare ids, HOLD the credits, freeze the inputs → QUEUED
run_plan   messages → channel plan → metrics → dated calendar → every number checked
             pass → debit once                                  → SUCCEEDED
seed       one DRAFT per calendar slot, never twice, never scheduled
```

**What the model writes, and what it does not.** The calendar's dates, the
split of posts across channels, the success metrics and every baseline are
computed here, from the brief and from measured posts. The model is asked for
words only: three message titles with an angle each, and a one-line role per
channel. Its reply is then held to the Phase 7 rules (`learn.services.narrate`)
— every numeral must already be in the inputs, and nothing may read as a
prediction. A reply that fails either is **replaced** by the template wording,
which is built from the brief's own text and passes by construction. The plan
records which of the two wrote it.

**The hold.** A plan in flight holds its confirmed price, so two confirmations
cannot both be promised the same credits. The ledger is written once, in the
transaction that marks the plan `SUCCEEDED`; a retried task finds it no longer
`QUEUED` and stops — the video render's discipline (`ai.services.video`).

**Seeding only makes drafts.** `create_post`, status `DRAFT`, no
`scheduled_at`, no delivery mode. Each slot's key is unique per campaign
(`CampaignItem.plan_slot`), so a second seed of the same plan creates nothing.
A draft reaches a calendar only through the schedule service, which asks for
an approved decision like it does for any other post (L-2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from accounts.models import User
from ai.models import CreativeKind, CreativeOption
from ai.providers.llm_text import get_text_provider
from ai.services.video import held_credits as video_held_credits
from billing.models import UNLIMITED
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import InsufficientCredits, OCCSError, ProviderError, StateConflict
from content.services.posts import create_post
from content.services.rules import formats_for
from learn.services.narrate import numerals_are_grounded, reads_as_a_prediction
from planning.models import Campaign, CampaignItem, CampaignPlan, PlanStatus
from planning.services import briefs
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

ACTIVE = (PlanStatus.QUEUED, PlanStatus.RUNNING)

#: The phases a running plan reports, with the progress each starts at.
PHASES: tuple[tuple[str, float], ...] = (
    ("BRIEF", 0.1),
    ("BRAND", 0.25),
    ("MESSAGES", 0.45),
    ("CHANNELS", 0.65),
    ("CALENDAR", 0.8),
    ("CHECK", 0.95),
)

#: The success metrics watched beside the goal. Watched, never targeted: the
#: only target in a plan is the one the user set.
LEADING_METRICS: tuple[str, ...] = ("impressions", "saves", "clicks")
#: The hour a slot is proposed at, in the workspace's own clock. A proposal
#: for the draft, not a schedule — the person picks the real time at approval.
SLOT_HOUR = 12
#: A ceiling on one plan's calendar, so a year-long window at 21 posts a week
#: does not seed a thousand drafts in one click.
MAX_SLOTS = 120

SYSTEM_PROMPT = (
    "You write the words of a social media campaign plan. You are given a JSON "
    "brief. Reply with JSON only: "
    '{"messages": [{"title": str, "angle": str, "evidence": [proof key]}] '
    '(exactly three), "channels": {platform: {"role": str}}}. '
    "Each title is a short claim a customer would read; each angle says how to "
    "show it, in one sentence. Use only the proof keys given. Never write a "
    "number that is not in the brief, never estimate or predict a result, and "
    "never say what will happen."
)
PROMPT_TEMPLATE_VERSION = "campaign-plan-v1"


class BriefIncompleteError(OCCSError):
    default_code = "brief_incomplete"
    default_detail = "The brief is not finished yet."


class PlanPriceNotConfiguredError(OCCSError):
    default_code = "plan_price_not_configured"
    default_detail = "No price is configured for a campaign plan."


class EstimateChangedError(StateConflict):
    default_code = "estimate_changed"
    default_detail = "The price changed since you confirmed it. Review the new estimate."


class PlanInProgressError(StateConflict):
    default_code = "plan_in_progress"
    default_detail = "A plan is already being written for this campaign."


class PlanMissingError(StateConflict):
    default_code = "plan_missing"
    default_detail = "Generate a plan before seeding the calendar."


# -----------------------------------------------------------------------------
# Price
# -----------------------------------------------------------------------------
def quote() -> dict[str, Any]:
    rows = list(
        CreativeOption.objects.filter(kind=CreativeKind.PLAN_PRICE, is_active=True).order_by(
            "sort_order", "pk"
        )
    )
    lines: list[dict[str, Any]] = []
    for row in rows:
        credits = (row.metadata or {}).get("credits")
        # A priced row with no price is a configuration error, never free.
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            raise PlanPriceNotConfiguredError(detail={"row": row.key})
        lines.append({"key": row.key, "label": row.label, "credits": credits})
    if not lines:
        raise PlanPriceNotConfiguredError()
    credits = sum(line["credits"] for line in lines)
    digest = hashlib.sha256(json.dumps([lines, credits]).encode()).hexdigest()[:10]
    return {"lines": lines, "credits": credits, "estimate_id": f"est_{digest}"}


def held_credits(workspace: Workspace) -> int:
    """Credits promised to plans in flight. A query, every time (rule 5)."""
    total = CampaignPlan.objects.filter(campaign__workspace=workspace, status__in=ACTIVE).aggregate(
        total=Sum("credits")
    )["total"]
    return int(total or 0)


def _available(workspace: Workspace) -> int | None:
    entitlements = entitlements_for(workspace)
    if entitlements.quota("monthly_ai_credits") == UNLIMITED:
        return None
    held = held_credits(workspace) + video_held_credits(workspace)
    return max(entitlements.credits_remaining() - held, 0)


def _ensure_ready(campaign: Campaign) -> None:
    gaps = briefs.missing(campaign, briefs.brief_of(campaign))
    if gaps:
        raise BriefIncompleteError(
            f"Finish the brief first: {', '.join(gaps)}.", detail={"missing": gaps}
        )


def estimate(campaign: Campaign) -> dict[str, Any]:
    briefs.ensure_open(campaign)
    _ensure_ready(campaign)
    return {**quote(), "available": _available(campaign.workspace)}


# -----------------------------------------------------------------------------
# Starting a plan
# -----------------------------------------------------------------------------
def build_inputs(campaign: Campaign) -> dict[str, Any]:
    """Everything the plan is written from, and therefore everything a number
    in it may come from."""
    brief = briefs.brief_of(campaign)
    goal = briefs.goal_of(campaign) or {}
    constraints = dict(brief.constraints or {})
    channels = list(constraints.get("channels") or [])
    medians = [
        row for row in briefs.measured_medians(campaign.workspace) if row["platform"] in channels
    ]
    return {
        "campaign": campaign.name,
        "goal": {
            "metric": goal.get("metric"),
            "label": briefs.METRIC_NOUNS.get(str(goal.get("metric")), str(goal.get("metric"))),
            "target": goal.get("target"),
        },
        "window": {
            "start": campaign.starts_at.date().isoformat(),
            "end": campaign.ends_at.date().isoformat(),
        },
        "audience": brief.audience,
        "segments": list(brief.segments),
        "offer": brief.offer,
        "proof": briefs.resolved_proof(campaign.workspace, brief),
        "constraints": constraints,
        "voice": briefs.voice_of(campaign.workspace),
        "baselines": medians,
        "baseline_window_days": briefs.MEASURED_WINDOW_DAYS,
        "formats": {platform: list(formats_for(platform))[:2] for platform in channels},
    }


def start(campaign: Campaign, *, user: User, estimate_id: str) -> CampaignPlan:
    """Hold the confirmed price, freeze the inputs and queue the plan.

    The price is worked out again here and must match the estimate the person
    confirmed: an edited price row in between is a 409 carrying the new
    estimate, never a charge they did not see.
    """
    briefs.ensure_open(campaign)
    _ensure_ready(campaign)
    priced = quote()
    if priced["estimate_id"] != estimate_id:
        raise EstimateChangedError(
            detail={"estimate": {**priced, "available": _available(campaign.workspace)}}
        )

    workspace = campaign.workspace
    entitlements = entitlements_for(workspace)
    with transaction.atomic():
        # The lock every spend takes: two confirmations racing for the last
        # free credits serialise here, and the second sees the first's hold.
        Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
        if CampaignPlan.objects.filter(campaign=campaign, status__in=ACTIVE).exists():
            raise PlanInProgressError(detail={"campaign": campaign.pk})
        entitlements.require_credits(
            priced["credits"], held=held_credits(workspace) + video_held_credits(workspace)
        )
        latest = CampaignPlan.objects.filter(campaign=campaign).aggregate(v=Max("version"))["v"]
        plan = CampaignPlan.objects.create(
            campaign=campaign,
            version=(latest or 0) + 1,
            estimate_id=priced["estimate_id"],
            credits=priced["credits"],
            lines=priced["lines"],
            inputs=build_inputs(campaign),
            created_by=user,
        )

    from planning.tasks import run_campaign_plan

    run_campaign_plan.delay(plan.pk)
    plan.refresh_from_db()  # under eager Celery the plan has already run
    return plan


# -----------------------------------------------------------------------------
# Writing the plan
# -----------------------------------------------------------------------------
def _clean(text: str, inputs: dict[str, Any]) -> bool:
    return numerals_are_grounded(text, inputs) and not reads_as_a_prediction(text)


def _template_words(inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Three messages from the brief's own words. Every title is a proof the
    user picked or the offer they wrote, so it passes the checks by
    construction — which is what makes it safe to fall back to."""
    proof = inputs["proof"]

    def evidence(index: int) -> list[dict[str, str]]:
        return [proof[min(index, len(proof) - 1)]] if proof else []

    segments = inputs.get("segments") or []
    second = (
        proof[1]["text"]
        if len(proof) > 1
        else f"Made for {segments[0].lower()}"
        if segments
        else "Who it is made for"
    )
    messages = [
        {
            "title": proof[0]["text"] if proof else inputs["offer"],
            "angle": "Lead with this, in your own words, and show where it comes from.",
            "evidence": evidence(0),
        },
        {
            "title": second,
            "angle": "Show the people and the place behind it, not the claim alone.",
            "evidence": evidence(1),
        },
        {
            "title": inputs["offer"],
            "angle": "Say the offer plainly, once per post, with the date it ends.",
            "evidence": evidence(2),
        },
    ]
    return messages, {}


def _model_words(inputs: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]] | None:
    """The provider's words, or `None` when they do not pass."""
    try:
        result = get_text_provider().generate(
            system=SYSTEM_PROMPT, prompt=json.dumps(inputs, ensure_ascii=False), n=1
        )
        reply = json.loads(result.variants[0].body)
        raw_messages = reply["messages"]
        raw_channels = reply.get("channels") or {}
    except (ProviderError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        logger.info("campaign plan: model reply unusable, using the template", exc_info=True)
        return None

    if not isinstance(raw_messages, list) or len(raw_messages) != 3:
        return None
    proof = {row["key"]: row for row in inputs["proof"]}
    fallback, _ = _template_words(inputs)
    messages: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            return None
        title, angle = raw.get("title"), raw.get("angle")
        if not isinstance(title, str) or not isinstance(angle, str):
            return None
        title, angle = title.strip()[:140], angle.strip()[:300]
        if not title or not _clean(title, inputs) or not _clean(angle, inputs):
            return None
        raw_keys = raw.get("evidence")
        keys: list[Any] = raw_keys if isinstance(raw_keys, list) else []
        cited = [proof[key] for key in keys if isinstance(key, str) and key in proof]
        messages.append(
            {"title": title, "angle": angle, "evidence": cited or fallback[index]["evidence"]}
        )

    roles: dict[str, str] = {}
    if isinstance(raw_channels, dict):
        for platform in inputs["constraints"].get("channels") or []:
            role = (
                (raw_channels.get(platform) or {}).get("role")
                if isinstance(raw_channels.get(platform), dict)
                else None
            )
            if isinstance(role, str) and role.strip():
                if not _clean(role, inputs):
                    return None
                roles[platform] = role.strip()[:120]
    return messages, roles


def _baseline(inputs: dict[str, Any], platform: str, metric: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in inputs["baselines"]
            if row["platform"] == platform and row["metric"] == metric
        ),
        None,
    )


def _channels(
    inputs: dict[str, Any], messages: list[dict[str, Any]], roles: dict[str, str]
) -> list[dict[str, Any]]:
    """The user's posts a week, split across their channels in the order they
    chose them. A split of the user's own number — not a recommendation."""
    channels = list(inputs["constraints"].get("channels") or [])
    per_week = int(inputs["constraints"].get("posts_per_week") or 0)
    keys = [message["key"] for message in messages]
    out: list[dict[str, Any]] = []
    for index, platform in enumerate(channels):
        share = per_week // len(channels) + (1 if index < per_week % len(channels) else 0)
        baseline = _baseline(inputs, platform, "impressions")
        days = inputs["baseline_window_days"]
        if baseline and baseline["median"] is not None:
            note = f"Median impressions per post, {baseline['n']} posts, last {days} days"
        elif baseline:
            note = f"Not enough data: {baseline['n']} measured posts in the last {days} days"
        else:
            note = f"Not enough data: no measured posts in the last {days} days"
        out.append(
            {
                "platform": platform,
                "role": roles.get(platform) or "Carries the campaign's messages",
                "formats": inputs["formats"].get(platform, []),
                "posts_per_week": share,
                "carries": keys if len(keys) <= 2 else keys[index % 3 :] + keys[: index % 3],
                "baseline": baseline if baseline and baseline["median"] is not None else None,
                "baseline_note": note,
            }
        )
    return out


def _metrics(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    goal = inputs["goal"]
    days = inputs["baseline_window_days"]
    rows: list[dict[str, Any]] = [
        {
            "metric": goal["metric"],
            "label": goal["label"],
            "role": "goal",
            "target": goal["target"],
            "by": inputs["window"]["end"],
            "baseline": None,
            "note": "Your target, measured from the campaign's posts from day one",
        }
    ]
    for metric in LEADING_METRICS:
        if metric == goal["metric"] or len(rows) == 3:
            continue
        readings = [row for row in inputs["baselines"] if row["metric"] == metric]
        measured = [row for row in readings if row["median"] is not None]
        if measured:
            note = "No target: watched, not promised"
        elif readings:
            posts = max(row["n"] for row in readings)
            note = f"Not enough data: {posts} measured posts in the last {days} days"
        else:
            note = f"Not enough data: no measured posts in the last {days} days"
        rows.append(
            {
                "metric": metric,
                "label": briefs.METRIC_NOUNS[metric],
                "role": "leading",
                "target": None,
                "by": None,
                "baseline": measured,
                "note": note,
            }
        )
    return rows


def _calendar(
    campaign: Campaign, channels: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Dated slots across the window: each week, the channel shares spread over
    the seven days, every message in turn. Starts on the first day whose slot
    hour is after both the campaign's start and now."""
    zone = ZoneInfo(campaign.workspace.timezone or "UTC")
    floor = max(campaign.starts_at, timezone.now())
    day = floor.astimezone(zone).replace(hour=SLOT_HOUR, minute=0, second=0, microsecond=0)
    if day < floor:
        day += dt.timedelta(days=1)

    per_week = sum(channel["posts_per_week"] for channel in channels)
    if per_week == 0:
        return []
    order = [
        channel["platform"]
        for round_ in range(max(c["posts_per_week"] for c in channels))
        for channel in channels
        if round_ < channel["posts_per_week"]
    ]
    formats = {channel["platform"]: (channel["formats"] or [""])[0] for channel in channels}
    weeks = math.ceil((campaign.ends_at - day) / dt.timedelta(days=7))

    slots: list[dict[str, Any]] = []
    for week in range(max(weeks, 0)):
        for index, platform in enumerate(order):
            at = day + dt.timedelta(days=week * 7 + (index * 7) // per_week)
            if at > campaign.ends_at or len(slots) >= MAX_SLOTS:
                continue
            slots.append(
                {
                    "key": f"s{len(slots) + 1}",
                    "at": at.astimezone(dt.UTC).isoformat(),
                    "platform": platform,
                    "message": messages[len(slots) % len(messages)]["key"],
                    "format": formats[platform],
                }
            )
    return slots


def _advance(plan: CampaignPlan, phase: str, progress: float) -> None:
    plan.phase, plan.progress = phase, progress
    plan.save(update_fields=["phase", "progress"])


def _fail(plan: CampaignPlan, code: str, message: str) -> CampaignPlan:
    plan.status = PlanStatus.FAILED
    plan.error_code, plan.error = code, message
    plan.finished_at = timezone.now()
    plan.save(update_fields=["status", "error_code", "error", "finished_at"])
    return plan


def compose(campaign: Campaign, inputs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The whole plan, and who wrote its words (`model` or `template`)."""
    words = _model_words(inputs)
    written_by = "model" if words else "template"
    messages, roles = words or _template_words(inputs)
    for index, message in enumerate(messages):
        message["key"] = f"M{index + 1}"
    channels = _channels(inputs, messages, roles)
    return (
        {
            "messages": messages,
            "channels": channels,
            "metrics": _metrics(inputs),
            "calendar": _calendar(campaign, channels, messages),
            "voice": inputs["voice"],
        },
        written_by,
    )


def run_plan(plan_id: int) -> CampaignPlan:
    """Idempotent: a plan that is not `QUEUED` is returned as it is, so a
    retried task can neither write twice nor charge twice."""
    with transaction.atomic():
        plan = CampaignPlan.objects.select_for_update().get(pk=plan_id)
        if plan.status != PlanStatus.QUEUED:
            return plan
        plan.status = PlanStatus.RUNNING
        plan.started_at = timezone.now()
        plan.phase, plan.progress = PHASES[0]
        plan.save(update_fields=["status", "started_at", "phase", "progress"])

    campaign = plan.campaign
    for phase, progress in PHASES[1:3]:
        _advance(plan, phase, progress)
    body, written_by = compose(campaign, plan.inputs)
    for phase, progress in PHASES[3:]:
        _advance(plan, phase, progress)

    entitlements = entitlements_for(campaign.workspace)
    try:
        with transaction.atomic():
            locked = CampaignPlan.objects.select_for_update().get(pk=plan.pk)
            if locked.status != PlanStatus.RUNNING:
                return locked
            locked.charge = ledger.debit_credits(
                campaign.workspace,
                locked.credits,
                quota=entitlements.quota("monthly_ai_credits"),
                note=f"campaign plan {locked.pk} (v{locked.version})",
            )
            locked.plan = body
            locked.written_by = written_by
            locked.status = PlanStatus.SUCCEEDED
            locked.phase, locked.progress = "DONE", 1.0
            locked.finished_at = timezone.now()
            locked.save()
            return locked
    except InsufficientCredits:
        return _fail(
            plan,
            "insufficient_credits",
            "There were not enough credits left when the plan was ready. Nothing was taken.",
        )


def latest(campaign: Campaign) -> CampaignPlan | None:
    return CampaignPlan.objects.filter(campaign=campaign).order_by("-version").first()


def read(plan: CampaignPlan) -> dict[str, Any]:
    """The plan, with each slot's seeded draft (or `None`)."""
    body = dict(plan.plan or {})
    if body.get("calendar"):
        prefix = f"v{plan.version}:"
        drafts = {
            item.plan_slot.removeprefix(prefix): {"id": item.post_id, "status": item.post.status}
            for item in CampaignItem.objects.filter(
                campaign=plan.campaign, plan_slot__startswith=prefix
            ).select_related("post")
        }
        body["calendar"] = [{**slot, "draft": drafts.get(slot["key"])} for slot in body["calendar"]]
    return {
        "id": plan.pk,
        "version": plan.version,
        "status": plan.status,
        "phase": plan.phase,
        "progress": plan.progress,
        "credits": plan.credits,
        "lines": plan.lines,
        "written_by": plan.written_by,
        "error_code": plan.error_code,
        "error": plan.error,
        "plan": body,
        "created_at": plan.created_at,
        "finished_at": plan.finished_at,
    }


# -----------------------------------------------------------------------------
# Seeding the calendar
# -----------------------------------------------------------------------------
def seed(campaign: Campaign, *, actor: User) -> dict[str, int]:
    briefs.ensure_open(campaign)
    plan = (
        CampaignPlan.objects.filter(campaign=campaign, status=PlanStatus.SUCCEEDED)
        .order_by("-version")
        .first()
    )
    if plan is None:
        raise PlanMissingError(detail={"campaign": campaign.pk})

    messages = {message["key"]: message for message in plan.plan.get("messages", [])}
    prefix = f"v{plan.version}:"
    created = skipped = 0
    with transaction.atomic():
        # Two seeds racing serialise on the campaign row; the unique slot
        # constraint is the backstop if anything ever bypasses this path.
        Campaign.objects.select_for_update().filter(pk=campaign.pk).exists()
        done = set(
            CampaignItem.objects.filter(
                campaign=campaign, plan_slot__startswith=prefix
            ).values_list("plan_slot", flat=True)
        )
        for slot in plan.plan.get("calendar", []):
            key = f"{prefix}{slot['key']}"
            if key in done:
                skipped += 1
                continue
            message = messages[slot["message"]]
            post = create_post(
                workspace=campaign.workspace,
                author=actor,
                master_body=f"{message['title']}\n\n{message['angle']}",
                planned_platforms=[slot["platform"]],
            )
            CampaignItem.objects.create(campaign=campaign, post=post, added_by=actor, plan_slot=key)
            created += 1
    return {"created": created, "skipped": skipped}
