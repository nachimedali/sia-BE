"""The `Collector` port, ported from trendgen (`feed/apps/collectors/base.py`).

Two methods, because a source does two things: work out what to ask for, and
ask. Everything after — upserting rows, scoring, describing structure — is
ours and behaves identically whichever provider answered. (trendgen's third
method, `cost`, is left out: both providers here are free, and a port method
nothing can implement would be a promise rather than a contract.)

`CollectedPost` is deliberately not `PlaybookPost`: a collector returns values
and never touches the database, which keeps it testable against a recorded
payload and keeps upsert policy in one place (`playbook.services.collect`).

**A count a provider does not report is `None`, never `0`** (rule 4). Every
count defaults to `None` and a collector passes only what it actually saw.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from common.exceptions import ProviderError

if TYPE_CHECKING:
    from playbook.models import PlaybookSource


class CollectorError(ProviderError):
    """A provider could not be read. Isolated per source: one dead provider
    must not fail a run the other can still serve."""

    default_code = "playbook_collector_error"
    default_detail = "A trend provider could not be read."


@dataclass(frozen=True)
class CollectedPost:
    """One post as a provider described it, before the Playbook has an opinion."""

    external_id: str
    posted_at: dt.datetime
    format: str
    url: str
    title: str
    caption: str = ""
    thumbnail_url: str = ""
    thumbnail_credit: str = ""
    creator: str = ""
    hashtags: list[str] = field(default_factory=list)
    lang: str = ""
    duration_seconds: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    traffic_floor: int | None = None
    traffic_label: str = ""
    news: list[dict[str, str]] = field(default_factory=list)
    #: The provider's own response for this item, verbatim.
    payload: dict[str, Any] = field(default_factory=dict)
    is_fake: bool = False


@dataclass(frozen=True)
class CollectRequest:
    """One call a collector intends to make. `plan` returns these so a run's
    shape — and its quota cost — is visible before any of it happens."""

    provider: str
    params: dict[str, Any] = field(default_factory=dict)
    #: The source's `config`, so `fetch` honours its limits without the row.
    config: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Collector(Protocol):
    provider: str

    @property
    def reports(self) -> frozenset[str]:
        """The counts this provider publishes. A name absent here is
        unavailable for *every* item — a different fact from a null on one."""
        ...

    def plan(self, source: PlaybookSource) -> list[CollectRequest]:
        """The calls this source implies, already inside its quota budget."""
        ...

    def fetch(self, request: CollectRequest) -> Iterable[CollectedPost]:
        """Run one planned call. Raises `CollectorError` on a provider failure."""
        ...
