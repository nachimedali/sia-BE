"""Reading the wall: filters, facets, one page, one post.

**Filtered on the server.** Every chip's count is what that chip would bring
back with the *other* filters applied — the shape the template's chips show —
so a person never clicks a chip and lands on nothing without being told first.

**Fixtures stay fixtures.** A fake collector's rows are served only when
`PLAYBOOK_SHOW_SAMPLE` is on (dev, test, e2e); production settings refuse it.
The response says when it is serving samples, and the page says so on screen.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import F, Max, Q, QuerySet
from django.utils import timezone

from common.exceptions import OCCSError
from playbook.models import PlaybookPost, PlaybookSource, PostFormat, Provider
from playbook.services.scoring import WINDOW_DAYS

PAGE_SIZE = 36
MAX_PAGE = 50
MAX_QUERY = 80
MAX_TERMS = 6
#: A post the providers stopped returning a week ago is no longer trending.
STALE_DAYS = 7
POSTED: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}
SORTS = ("trending", "views", "engagement", "newest")


@dataclass(frozen=True)
class Filters:
    category: tuple[str, ...] = ()
    provider: tuple[str, ...] = ()
    format: str = ""
    posted: str = ""
    q: str = ""
    sort: str = "trending"
    page: int = 1


def _csv(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(part.strip() for part in str(value or "").split(",") if part.strip())
    )


def parse(params: Any) -> Filters:
    """Read the query string. An unknown value is a 400 naming the field —
    never ignored, which would show a filtered-looking page that is not."""
    providers = _csv(params.get("platform"))
    if bad := [p for p in providers if p not in Provider.values]:
        raise OCCSError("Unknown platform.", code="invalid_platform", detail={"platform": bad})
    # `kind`, not `format`: DRF reserves `?format=` for content negotiation,
    # and a filter named after it answers 404 before reaching this view.
    fmt = str(params.get("kind") or "")
    if fmt and fmt not in PostFormat.values:
        raise OCCSError("Unknown kind of post.", code="invalid_kind", detail={"kind": fmt})
    posted = str(params.get("posted") or "")
    if posted and posted not in POSTED:
        raise OCCSError("Unknown time window.", code="invalid_posted", detail={"posted": posted})
    sort = str(params.get("sort") or "trending")
    if sort not in SORTS:
        raise OCCSError("Unknown sort.", code="invalid_sort", detail={"sort": sort})
    q = str(params.get("q") or "").strip()
    if len(q) > MAX_QUERY:
        raise OCCSError(
            f"Search for at most {MAX_QUERY} characters.",
            code="invalid_query",
            detail={"q": q[:20]},
        )
    try:
        page = int(params.get("page") or 1)
    except (TypeError, ValueError):
        page = 0
    if not 1 <= page <= MAX_PAGE:
        raise OCCSError("Unknown page.", code="invalid_page", detail={"page": params.get("page")})
    return Filters(
        category=_csv(params.get("category")),
        provider=providers,
        format=fmt,
        posted=posted,
        q=q,
        sort=sort,
        page=page,
    )


def region_for(workspace: Any) -> str:
    """The workspace's market when the Playbook collects for it, otherwise the
    default market — said on screen, never silently swapped."""
    market = str(getattr(workspace, "market", "") or "").upper()
    if market and PlaybookSource.objects.filter(region=market, is_active=True).exists():
        return market
    return str(settings.PLAYBOOK_DEFAULT_REGION).upper()


def visible(region: str, *, now: dt.datetime | None = None) -> QuerySet[PlaybookPost]:
    now = now or timezone.now()
    rows = PlaybookPost.objects.filter(
        region=region,
        posted_at__gte=now - dt.timedelta(days=WINDOW_DAYS),
        last_seen_at__gte=now - dt.timedelta(days=STALE_DAYS),
    )
    if not settings.PLAYBOOK_SHOW_SAMPLE:
        rows = rows.filter(is_fake=False)
    return rows


def _apply(
    rows: QuerySet[PlaybookPost], f: Filters, *, now: dt.datetime, skip: str = ""
) -> QuerySet[PlaybookPost]:
    if skip != "category" and f.category:
        rows = rows.filter(category__slug__in=f.category)
    if skip != "platform" and f.provider:
        rows = rows.filter(provider__in=f.provider)
    if skip != "format" and f.format:
        rows = rows.filter(format=f.format)
    if skip != "posted" and f.posted:
        rows = rows.filter(posted_at__gte=now - dt.timedelta(days=POSTED[f.posted]))
    for term in f.q.split()[:MAX_TERMS]:
        rows = rows.filter(
            Q(title__icontains=term) | Q(caption__icontains=term) | Q(creator__icontains=term)
        )
    return rows


def _counts(rows: QuerySet[PlaybookPost], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in rows.values_list(field, flat=True):
        if value:
            out[value] = out.get(value, 0) + 1
    return out


ORDER: dict[str, list[Any]] = {
    "trending": [F("trend_score").desc(nulls_last=True), "-posted_at", "-pk"],
    "views": [F("views").desc(nulls_last=True), "-posted_at", "-pk"],
    "engagement": [F("engagement").desc(nulls_last=True), "-posted_at", "-pk"],
    "newest": ["-posted_at", "-pk"],
}


def wall(workspace: Any, f: Filters, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    region = region_for(workspace)
    base = visible(region, now=now)
    rows = _apply(base, f, now=now).select_related("category")
    count = rows.count()
    start = (f.page - 1) * PAGE_SIZE
    results = list(rows.order_by(*ORDER[f.sort])[start : start + PAGE_SIZE])

    categories: dict[str, dict[str, Any]] = {}
    for slug, name in (
        _apply(base, f, now=now, skip="category")
        .filter(category__isnull=False)
        .values_list("category__slug", "category__name")
    ):
        entry = categories.setdefault(slug, {"slug": slug, "name": name, "count": 0})
        entry["count"] += 1
    posted_base = _apply(base, f, now=now, skip="posted")
    refreshed = base.aggregate(at=Max("last_seen_at"))["at"]

    return {
        "region": region,
        "market": str(getattr(workspace, "market", "") or "").upper(),
        "count": count,
        "page": f.page,
        "pages": max(1, -(-count // PAGE_SIZE)),
        "page_size": PAGE_SIZE,
        "total": base.count(),
        "refreshed_at": refreshed,
        "sample": base.filter(is_fake=True).exists(),
        "results": results,
        "facets": {
            "category": sorted(categories.values(), key=lambda c: (-c["count"], c["name"])),
            "platform": _counts(_apply(base, f, now=now, skip="platform"), "provider"),
            "format": _counts(_apply(base, f, now=now, skip="format"), "format"),
            "posted": {
                key: posted_base.filter(posted_at__gte=now - dt.timedelta(days=days)).count()
                for key, days in POSTED.items()
            },
        },
    }


def post(pk: int) -> PlaybookPost | None:
    """One post, if it is still servable. Not limited to the window: a person
    who picked it yesterday can still compose from it today."""
    rows = PlaybookPost.objects.filter(pk=pk).select_related("category")
    if not settings.PLAYBOOK_SHOW_SAMPLE:
        rows = rows.filter(is_fake=False)
    return rows.first()
