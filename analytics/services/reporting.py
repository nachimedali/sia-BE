"""Aggregation and the horizon refusal (P6-02, P6-03, P6-09).

Three rules govern everything here, and each one exists because the obvious
shortcut produces a document that is wrong and looks right:

* **Refuse a window the plan cannot cover, never truncate it.** A client-facing
  PDF headed "the last year" that quietly contains seven days is worse than an
  error, because nothing in it says so and the reader has no way to find out.
* **Read through `analysable()`.** An `UNAVAILABLE` row records that we asked
  and got no answer; in a denominator it becomes a zero and drags every average
  it touches (C-07, Part 7 rule 12).
* **Only the latest capture per target.** Snapshots are cumulative, not
  incremental — a post captured at T+1h and again at T+24h is one post with two
  rows, and summing both double-counts every number in the report.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from django.core.files.base import ContentFile
from django.db.models import Count, Max, Sum
from rest_framework.exceptions import ValidationError

from analytics.models import Availability, PostMetric
from billing.models import UNLIMITED
from billing.services.entitlements import entitlements_for
from common.exceptions import PaymentRequired
from workspaces.models import Workspace

#: The columns a report totals. Declared rather than reflected off the model:
#: a new metric column should reach reports by a deliberate edit, not by
#: appearing in every customer's PDF the moment it is added.
SUMMABLE: tuple[str, ...] = ("impressions", "likes", "comments", "shares", "clicks", "saves")


class HorizonExceeded(PaymentRequired):
    """402, with the number. "Too far back" alone leaves the user guessing at a
    value only we know."""

    default_code = "analytics_horizon_exceeded"
    default_detail = "That range reaches further back than this plan keeps analytics."


def ensure_within_horizon(
    workspace: Workspace, *, starts_at: dt.datetime, ends_at: dt.datetime
) -> None:
    """**Refuses rather than clipping** (P6-09, gate P6-G1).

    A 402 rather than a 400: the range is not malformed, it is further than
    this plan reaches, and that is something the workspace can buy past — the
    same reading `require_scheduling_horizon` already takes for the other end
    of the same idea.
    """
    if ends_at < starts_at:
        raise ValidationError({"window": "A report window ends after it starts."})

    days = entitlements_for(workspace).analytics_horizon_days()
    if days == UNLIMITED:
        return

    from django.utils import timezone

    earliest = timezone.now() - dt.timedelta(days=days)
    if starts_at < earliest:
        raise HorizonExceeded(
            f"This plan keeps {days} days of analytics; that range starts earlier.",
            detail={"horizon_days": days, "earliest": earliest.isoformat()},
        )


def latest_per_target(workspace: Workspace, *, starts_at: Any, ends_at: Any) -> Any:
    """The newest analysable capture for each target in the window.

    Two queries rather than a window function: the ids come back first, then
    the rows. A `DISTINCT ON` would be one query and Postgres-only, and this
    module is read by the report renderer, the campaign digest and Phase 7 —
    none of which should inherit a database lock-in for one aggregate.
    """
    scoped = PostMetric.objects.analysable().filter(
        post_target__post__workspace=workspace,
        captured_at__gte=starts_at,
        captured_at__lte=ends_at,
    )
    latest = scoped.values("post_target").annotate(newest=Max("captured_at"))
    keys = {(row["post_target"], row["newest"]) for row in latest}
    if not keys:
        return PostMetric.objects.none()

    ids = [
        row["id"]
        for row in scoped.values("id", "post_target", "captured_at")
        if (row["post_target"], row["captured_at"]) in keys
    ]
    return PostMetric.objects.filter(id__in=ids)


def channel_totals(
    workspace: Workspace, *, starts_at: dt.datetime, ends_at: dt.datetime
) -> list[dict[str, Any]]:
    """Per-platform totals over the window (P6-02).

    A platform with no analysable capture is **absent**, not a row of zeros:
    "we published nothing there" and "we measured nothing there" are different
    facts, and a zero row states the first while meaning the second.
    """
    rows = (
        latest_per_target(workspace, starts_at=starts_at, ends_at=ends_at)
        .values("post_target__platform")
        .annotate(
            # `distinct=True` because one target can contribute only one row
            # here already — but the annotation travels with this query into
            # Phase 7's digest, and a count that is right only by construction
            # is one nobody can safely reuse.
            posts=Count("post_target", distinct=True),
            **{column: Sum(column) for column in SUMMABLE},
        )
        .order_by("post_target__platform")
    )

    return [
        {
            "platform": row["post_target__platform"],
            "posts": row["posts"],
            **{column: row[column] for column in SUMMABLE},
        }
        for row in rows
    ]


def campaign_totals(campaign: Any) -> dict[str, Any]:
    """Everything one campaign measured, and how its goals actually did (P6-03).

    Aggregated over `CampaignItem` rather than over a date range: a campaign is
    the posts somebody put in it, and a window would silently include work that
    happened to run at the same time.
    """
    metrics = (
        PostMetric.objects.analysable()
        .filter(post_target__post__campaign_items__campaign=campaign)
        .values("post_target")
        .annotate(newest=Max("captured_at"))
    )
    keys = {(row["post_target"], row["newest"]) for row in metrics}
    rows = [
        row
        for row in PostMetric.objects.analysable()
        .filter(post_target__post__campaign_items__campaign=campaign)
        .values("id", "post_target", "captured_at", *SUMMABLE, "engagement_rate")
        if (row["post_target"], row["captured_at"]) in keys
    ]

    totals: dict[str, Any] = {"posts": len(rows)}
    for column in SUMMABLE:
        measured = [row[column] for row in rows if row[column] is not None]
        totals[column] = sum(measured) if measured else None

    rates = [row["engagement_rate"] for row in rows if row["engagement_rate"] is not None]
    # **Null, not zero.** No posts means no engagement rate to report, and 0.0
    # would be a measurement nobody took.
    totals["engagement_rate"] = (sum(rates) / len(rates)) if rates else None

    totals["goals"] = [_goal_progress(goal, totals) for goal in (campaign.goals or [])]
    return totals


def _goal_progress(goal: dict[str, Any], totals: dict[str, Any]) -> dict[str, Any]:
    """One declared goal against what was measured.

    **Unavailable is not failure.** A goal on a metric nothing reported has not
    been missed, and saying it was would be a fabricated verdict against the
    customer — so `met` is `None`, which the surface renders as "we could not
    measure this" rather than a red cross.
    """
    metric = str(goal.get("metric") or "")
    target = goal.get("target")
    actual = totals.get(metric)
    return {
        "metric": metric,
        "target": target,
        "actual": actual,
        "met": None if actual is None else actual >= target,
    }


# -----------------------------------------------------------------------------
# Report runs (P6-04, P6-05, P6-09)
# -----------------------------------------------------------------------------
#: Section kind → what it renders. Declared as data, so adding a section is an
#: entry plus a test rather than a branch in a renderer — and an unknown kind
#: is **refused at the write**, because a saved report carrying a section
#: nothing renders would produce a document with a silent hole in it.
SectionRenderer = Callable[..., dict[str, Any]]

SECTION_KINDS: dict[str, SectionRenderer] = {}


def section(kind: str) -> Callable[[SectionRenderer], SectionRenderer]:
    """Registers a section renderer under `kind`."""

    def register(renderer: SectionRenderer) -> SectionRenderer:
        SECTION_KINDS[kind] = renderer
        return renderer

    return register


@section("channel_totals")
def _channel_section(
    workspace: Workspace, *, starts_at: Any, ends_at: Any, **options: Any
) -> dict[str, Any]:
    return {"rows": channel_totals(workspace, starts_at=starts_at, ends_at=ends_at)}


@section("campaign_totals")
def _campaign_section(
    workspace: Workspace, *, starts_at: Any, ends_at: Any, **options: Any
) -> dict[str, Any]:
    """Campaigns overlapping the window.

    Scoped to the workspace inside the query rather than trusting the id in
    the section's options: a report is stored data, and a stored id from
    another tenant would otherwise be honoured every month forever.
    """
    from planning.models import Campaign

    campaigns = Campaign.objects.filter(
        workspace=workspace, starts_at__lte=ends_at, ends_at__gte=starts_at
    )
    return {
        "campaigns": [
            {"id": campaign.pk, "name": campaign.name, **campaign_totals(campaign)}
            for campaign in campaigns
        ]
    }


@section("audience")
def _audience_section(
    workspace: Workspace, *, starts_at: Any, ends_at: Any, **options: Any
) -> dict[str, Any]:
    """Follower counts and demographics, from the daily account snapshot.

    **Below the provider's threshold this says `unavailable`, never zero.**
    Demographics need ≥100 followers and lag up to 48 hours (L-5), and a
    breakdown rendered as all-zeros would read as "your audience is nobody".
    """
    from analytics.models import AccountSnapshot, AudienceDemographic

    snapshots = (
        AccountSnapshot.objects.filter(
            social_account__workspace=workspace,
            captured_at__gte=starts_at,
            captured_at__lte=ends_at,
        )
        .order_by("social_account", "-captured_at")
        .select_related("social_account")
    )
    seen: set[int] = set()
    accounts: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if snapshot.social_account_id in seen:
            continue
        seen.add(snapshot.social_account_id)
        accounts.append(
            {
                "platform": snapshot.social_account.platform,
                "followers": snapshot.followers,
            }
        )

    # The newest row per `(account, dimension)`, exactly like the snapshots
    # above. Demographics are captured daily, so listing the whole window would
    # put thirty near-identical age breakdowns per account into one report —
    # a section nobody can read, stating the same thing thirty times.
    demographic_rows = (
        AudienceDemographic.objects.filter(
            social_account__workspace=workspace,
            captured_at__gte=starts_at,
            captured_at__lte=ends_at,
        )
        .order_by("social_account", "dimension", "-captured_at")
        .select_related("social_account")
    )
    newest: dict[tuple[int, str], Any] = {}
    for row in demographic_rows:
        newest.setdefault((row.social_account_id, row.dimension), row)

    demographics = [
        {
            "platform": row.social_account.platform,
            "dimension": row.dimension,
            "breakdown": row.breakdown if row.availability == Availability.MEASURED else None,
            "availability": row.availability,
        }
        for row in newest.values()
    ]
    return {"accounts": accounts, "demographics": demographics}


def validate_sections(sections: Any) -> list[dict[str, Any]]:
    """Refused, never ignored.

    A saved report carrying a section nothing renders would produce a document
    with a hole where the customer expected a chart — and it would do it every
    month, silently.
    """
    if not isinstance(sections, list):
        raise ValidationError({"sections": "Sections are a list of {kind, options} objects."})

    for index, entry in enumerate(sections):
        if not isinstance(entry, dict):
            raise ValidationError({"sections": f"Section {index}: each section is an object."})
        kind = entry.get("kind")
        if kind not in SECTION_KINDS:
            raise ValidationError(
                {
                    "sections": f"Section {index}: unknown kind {kind!r}.",
                    "allowed": sorted(SECTION_KINDS),
                }
            )
        if "options" in entry and not isinstance(entry["options"], dict):
            raise ValidationError({"sections": f"Section {index}: 'options' is an object."})

    return sections


def render_report(
    report: Any, *, starts_at: dt.datetime, ends_at: dt.datetime, requested_by: Any = None
) -> Any:
    """Compute every section, then hand the result to the render port.

    **The horizon is checked first, and refuses** (P6-09, gate P6-G1). Nothing
    is computed and no run row is written for a window this plan cannot cover:
    a `ReportRun` that existed but failed would show up in the list as a
    document somebody might try to open.
    """
    from analytics.models import ReportRun, ReportRunStatus
    from analytics.providers.rendering import get_report_renderer

    ensure_within_horizon(report.workspace, starts_at=starts_at, ends_at=ends_at)

    payload = {
        "report": report.name,
        "window": {"start": starts_at.isoformat(), "end": ends_at.isoformat()},
        "sections": [
            {
                "kind": entry["kind"],
                "data": SECTION_KINDS[entry["kind"]](
                    report.workspace,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    **(entry.get("options") or {}),
                ),
            }
            for entry in report.sections
        ],
    }

    run = ReportRun.objects.create(
        report=report,
        window_start=starts_at,
        window_end=ends_at,
        payload=payload,
        requested_by=requested_by,
        status=ReportRunStatus.PENDING,
    )

    renderer = get_report_renderer()
    try:
        document = renderer.render(title=report.name, payload=payload)
    except Exception as error:
        # Recorded, not raised: the numbers were computed and are worth keeping
        # even when the document could not be drawn, and a caller left holding
        # an exception has no way to show the customer what was measured.
        run.status = ReportRunStatus.FAILED
        run.error_detail = {"message": str(error)}
        run.save(update_fields=["status", "error_detail"])
        return run

    run.document.save(f"report-{run.pk}.{renderer.extension}", ContentFile(document), save=False)
    run.status = ReportRunStatus.READY
    run.save(update_fields=["document", "status"])
    return run
