"""Fixture-backed `TrendVendor` for tests and dev (A8).

The corpus lives in `trends/fixtures/trend_items.json` rather than in this
module, and it is shaped to be *hard*: near-duplicate pairs, a non-English item
and a spam item per kind, so stage 2 has something real to reject and stage 4
has something real to group. A fake that returned five clean, unrelated items
would make the whole pipeline suite pass without proving any stage works.

It honours the watermark and the limit, because ingest's idempotency is exactly
what `test_trend_ingest_is_idempotent_on_external_id` asserts, and a fake that
ignored `since` would make the second run look identical to the first for the
wrong reason.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from trends.providers.base import RawTrendItem, TrendVendor
from trends.providers.http import newer_than, parse_timestamp

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "trend_items.json"


@lru_cache(maxsize=1)
def _fixture() -> dict[str, list[dict[str, Any]]]:
    data = json.loads(FIXTURE_PATH.read_text())
    return {key: value for key, value in data.items() if not key.startswith("_")}


class FakeTrendVendor:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[dict[str, Any]] = []

    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        self.calls.append({"query": query, "since": since, "limit": limit})
        items = [
            RawTrendItem(
                external_id=str(entry["external_id"]),
                posted_at=parse_timestamp(entry["posted_at"]),
                body=str(entry.get("body", "")),
                modality=str(entry.get("modality", "TEXT")),
                author_handle=str(entry.get("author_handle", "")),
                author_followers=int(entry.get("author_followers", 0)),
                media_url=str(entry.get("media_url", "")),
                lang=str(entry.get("lang", "")),
                raw_metrics=dict(entry.get("raw_metrics", {})),
            )
            for entry in _fixture().get(self.kind, [])
        ]
        return newer_than(items, since, key=lambda item: item.posted_at)[:limit]

    def clear(self) -> None:
        self.calls.clear()


# One instance per kind, module-level, so a test can inspect what a service or
# task asked for after the fact (mirrors `ai.providers.fake._fake_text_provider`).
_fake_vendors: dict[str, FakeTrendVendor] = {}


def get_fake_vendor(kind: str) -> TrendVendor:
    return _fake_vendors.setdefault(kind, FakeTrendVendor(kind))


def clear_fake_vendors() -> None:
    for vendor in _fake_vendors.values():
        vendor.clear()
