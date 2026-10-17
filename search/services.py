"""Workspace search — posts, products and campaigns, closing A-14's dead
`/app/search` link.

**Scope is the list pages' scope, never a wider one.** Every source starts from
the rows its own list endpoint would return: the request's workspace, and for
posts the request's audience (`common.visibility`). Another organization's or a
sibling brand's rows cannot match because they are never in the queryset, which
is the same guarantee `checks.views._post` relies on.

**Matching** is every term in at least one field — AND across terms, OR across
fields — case-insensitive. Accents are *not* folded: that needs the `unaccent`
extension, which no migration installs yet.

**History.** Posts and campaigns are searched only inside the plan's
`analytics_history_days`, measured on `updated_at`. Older matches are counted,
never returned, so the page can say how many the window cut off and which plan
would reach them. Products are a catalogue, not a timeline, so they are never
windowed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import reduce
from operator import and_, or_
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from billing.models import Plan
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from common.visibility import Audience, visible_values
from content.models import Post, PostStatus
from planning.models import Campaign, CampaignStatus
from products.models import Product
from workspaces.models import Workspace

TYPES = ("all", "posts", "products", "campaigns")
PAGE_SIZE = 8
ALL_PREVIEW = 3
MAX_PAGE = 20
MIN_QUERY = 2
MAX_QUERY = 120
MAX_TERMS = 6
TITLE_LENGTH = 90

_SCHEDULED = {PostStatus.SCHEDULED, PostStatus.REMINDER_ARMED, PostStatus.PUBLISHING}


@dataclass(frozen=True)
class Source:
    key: str
    fields: tuple[str, ...]
    #: `None` for a source the history window does not apply to.
    window_field: str | None
    base: Callable[[Workspace, Audience], QuerySet[Any]]
    hit: Callable[[Any], dict[str, Any]]


def _first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


def _post_hit(post: Post) -> dict[str, Any]:
    if post.status == PostStatus.PUBLISHED:
        label, date = "Published", post.scheduled_at or post.updated_at
    elif post.status in _SCHEDULED and post.scheduled_at:
        label, date = "Scheduled", post.scheduled_at
    else:
        label, date = "Edited", post.updated_at
    platforms = sorted({target.platform for target in post.targets.all()}) or list(
        post.planned_platforms
    )
    return {
        "type": "post",
        "id": post.pk,
        "title": _first_line(post.master_body) or "Untitled post",
        "text": post.master_body,
        "status": post.status,
        "platforms": platforms,
        "date": date,
        "date_label": label,
    }


def _product_hit(product: Product) -> dict[str, Any]:
    return {
        "type": "product",
        "id": product.pk,
        "title": product.name,
        "text": product.description,
        "status": "READY" if product.is_generation_ready else "NEEDS_REFERENCE",
        "platforms": list(product.platforms),
        "date": product.updated_at,
        "date_label": "Edited",
    }


def _campaign_hit(campaign: Campaign) -> dict[str, Any]:
    if campaign.status == CampaignStatus.PLANNING:
        label, date = "Starts", campaign.starts_at
    elif campaign.status == CampaignStatus.ACTIVE:
        label, date = "Ends", campaign.ends_at
    else:
        label, date = "Ended", campaign.ends_at
    volume = campaign.planned_volume if isinstance(campaign.planned_volume, dict) else {}
    return {
        "type": "campaign",
        "id": campaign.pk,
        "title": campaign.name,
        "text": "",
        "status": campaign.status,
        "platforms": sorted(volume),
        "date": date,
        "date_label": label,
    }


SOURCES: tuple[Source, ...] = (
    Source(
        key="posts",
        fields=(
            "master_body",
            "product__name",
            "author__email",
            "author__first_name",
            "author__last_name",
        ),
        window_field="updated_at",
        base=lambda workspace, audience: Post.objects.filter(
            workspace=workspace, visibility__in=visible_values(audience)
        ).prefetch_related("targets"),
        hit=_post_hit,
    ),
    Source(
        key="products",
        fields=("name", "description"),
        window_field=None,
        base=lambda workspace, _audience: Product.objects.filter(workspace=workspace),
        hit=_product_hit,
    ),
    Source(
        key="campaigns",
        fields=("name", "created_by__email", "created_by__first_name", "created_by__last_name"),
        window_field="updated_at",
        base=lambda workspace, _audience: Campaign.objects.filter(workspace=workspace),
        hit=_campaign_hit,
    ),
)


def terms_of(q: str) -> list[str]:
    return q.split()[:MAX_TERMS]


def _match(fields: Iterable[str], terms: list[str]) -> Q:
    per_term = (
        reduce(or_, (Q(**{f"{field}__icontains": term}) for field in fields)) for term in terms
    )
    return reduce(and_, per_term)


def validate(q: str, kind: str, page: Any) -> tuple[str, str, int]:
    """The query as the endpoint accepts it, or the 400 that says why not."""
    q = q.strip()
    if len(q.replace(" ", "")) < MIN_QUERY or len(q) > MAX_QUERY:
        raise OCCSError(
            f"Search for between {MIN_QUERY} and {MAX_QUERY} characters.",
            code="invalid_query",
            detail={"min": MIN_QUERY, "max": MAX_QUERY},
        )
    if kind not in TYPES:
        raise OCCSError(
            f"Unknown result type: {kind}.", code="invalid_type", detail={"type": list(TYPES)}
        )
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 0
    if not 1 <= number <= MAX_PAGE:
        raise OCCSError(
            f"Page must be between 1 and {MAX_PAGE}.", code="invalid_page", detail={"max": MAX_PAGE}
        )
    return q, kind, number


def _next_plan(current: Plan, days: int) -> dict[str, Any] | None:
    """The cheapest public plan that searches further back, if any does."""
    for plan in Plan.objects.filter(is_public=True, sort_order__gt=current.sort_order).order_by(
        "sort_order"
    ):
        reach = int(plan.feature("analytics_history_days") or 0)
        if reach > days:
            return {"name": plan.display_name, "days": reach}
    return None


def search(
    *, workspace: Workspace, audience: Audience, q: str, kind: str = "all", page: int = 1
) -> dict[str, Any]:
    entitlements = entitlements_for(workspace)
    days = entitlements.analytics_horizon_days()
    cutoff = timezone.now() - dt.timedelta(days=days)
    terms = terms_of(q)

    counts: dict[str, int] = {}
    results: dict[str, list[dict[str, Any]]] = {}
    older = 0
    for source in SOURCES:
        rows = source.base(workspace, audience).filter(_match(source.fields, terms))
        if source.window_field:
            older += rows.filter(**{f"{source.window_field}__lt": cutoff}).count()
            rows = rows.filter(**{f"{source.window_field}__gte": cutoff})
        rows = rows.order_by("-updated_at", "-pk")
        counts[source.key] = rows.count()
        if kind == "all":
            results[source.key] = [source.hit(row) for row in rows[:ALL_PREVIEW]]
        elif kind == source.key:
            results[source.key] = [source.hit(row) for row in rows[: PAGE_SIZE * page]]
    counts["all"] = sum(counts[source.key] for source in SOURCES)

    plan = entitlements.plan
    return {
        "q": q,
        "type": kind,
        "page": page,
        "page_size": PAGE_SIZE,
        "counts": counts,
        "results": results,
        "history": {
            "plan": plan.display_name,
            "days": days,
            "older_matches": older,
            "next_plan": _next_plan(plan, days),
        },
    }
