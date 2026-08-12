"""The `TrendVendor` port (design.md §9, D12; implementation.md Phase 10).

One method, `fetch`, because that is the whole of what a trend source does:
hand back what it has published since a watermark. Everything after that —
normalising, scoring, clustering — is ours and identical whatever answered.

**An adapter per source kind, not per platform** (D12). Meta Ad Library and
TikTok Creative Center are reached through a data vendor (RapidAPI or
equivalent, which moves the ToS exposure to them); YouTube and Reddit are their
own public APIs; RSS is a feed. Four wire shapes, so four adapters, resolved by
`TrendSource.kind` — a vendor going dark is then one module, exactly the
localisation D12 asks for.

`RawTrendItem` is deliberately not `TrendItem`: an adapter returns values, and
the ingest stage is the only thing that writes rows. That keeps adapters
testable without a database and keeps upsert policy in one place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol

from common.exceptions import ProviderError
from trends.models import TrendModality


class TrendVendorError(ProviderError):
    default_code = "trend_vendor_error"
    default_detail = "A trend source could not be read."


@dataclass(frozen=True)
class RawTrendItem:
    """One item as a vendor described it, before OCCS has an opinion about it."""

    external_id: str
    posted_at: dt.datetime
    body: str = ""
    modality: str = TrendModality.TEXT
    author_handle: str = ""
    author_followers: int = 0
    media_url: str = ""
    lang: str = ""
    #: Whatever the vendor sent about reach. Kept verbatim so the scoring
    #: formula can be retuned against history rather than re-ingesting it.
    raw_metrics: dict[str, Any] = field(default_factory=dict)


class TrendVendor(Protocol):
    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        """Items published after `since`, newest first, at most `limit`.

        `since` is `None` on a source's first run. An adapter that cannot
        express a lower bound on its API filters client-side rather than
        returning the whole history — ingest is idempotent either way, but the
        watermark exists to keep the request small.
        """
        ...
