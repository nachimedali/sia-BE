"""Trend feed adapters.

`LocalTrendFeed` is today's behaviour, reading `trends/` in process. The HTTP
adapter that reads trendgen's `GET /v1/feeds` arrives in Phase 12; until then
the flag has one implementation to choose and defaults off.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from billing.services.flags import TRENDGEN_FEED
from integrations.ports import ClusterSummary, TrendFeedPort
from integrations.seam import Seam

if TYPE_CHECKING:
    from workspaces.models import Organization


class LocalTrendFeed:
    """Reads the in-process trend corpus. Pre-phase behaviour, exactly."""

    def top_cluster(self, *, category_id: int, platform: str) -> ClusterSummary | None:
        from trends.services import top_cluster

        cluster = top_cluster(category_id, platform)
        if cluster is None:
            return None
        return ClusterSummary(
            label=cluster.label,
            platform_display=cluster.get_platform_display(),
            item_count=cluster.item_count,
        )

    def corpus_bodies(
        self, *, category_id: int, since: dt.datetime, limit: int | None = None
    ) -> list[str]:
        from trends.models import TrendItem

        rows = TrendItem.objects.filter(
            source__category_id=category_id, posted_at__gte=since, excluded_reason=""
        ).values_list("body", flat=True)
        if limit is not None:
            rows = rows[:limit]
        return [body or "" for body in rows]

    def corpus_size(self, *, category_id: int, since: dt.datetime) -> int:
        from trends.models import TrendItem

        return TrendItem.objects.filter(
            source__category_id=category_id, posted_at__gte=since, excluded_reason=""
        ).count()


@dataclass
class FakeTrendFeed:
    """Recording fake. Answers from what a test put in, and remembers the asks.

    A fresh checkout runs the whole suite on this — no trend vendor, no
    trendgen instance (Part 7 rule 6).
    """

    clusters: dict[tuple[int, str], ClusterSummary] = field(default_factory=dict)
    bodies: dict[int, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def top_cluster(self, *, category_id: int, platform: str) -> ClusterSummary | None:
        self.calls.append(("top_cluster", {"category_id": category_id, "platform": platform}))
        return self.clusters.get((category_id, platform))

    def corpus_bodies(
        self, *, category_id: int, since: dt.datetime, limit: int | None = None
    ) -> list[str]:
        self.calls.append(
            ("corpus_bodies", {"category_id": category_id, "since": since, "limit": limit})
        )
        rows = self.bodies.get(category_id, [])
        return rows[:limit] if limit is not None else list(rows)

    def corpus_size(self, *, category_id: int, since: dt.datetime) -> int:
        self.calls.append(("corpus_size", {"category_id": category_id, "since": since}))
        return len(self.bodies.get(category_id, []))


_seam: Seam[TrendFeedPort] = Seam(
    flag=TRENDGEN_FEED, local=LocalTrendFeed, remote_phase="Phase 12 (P12-02)"
)


def set_override(port: TrendFeedPort | None) -> None:
    _seam.override(port)


def get_trend_feed(organization: Organization) -> TrendFeedPort:
    return _seam.resolve(organization)
