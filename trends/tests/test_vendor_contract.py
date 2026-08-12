"""`TrendVendor` contract (design.md §9, A8, D12).

Same shape as `ai/tests/test_provider_contract.py` and
`channels/tests/test_adapter_contract.py`: the fake runs everywhere, and each
real adapter runs against `httpx.MockTransport` so its **parameter assembly** is
covered without credentials. A missing `publishedAfter`, a misread `created_utc`
or a watermark that never narrows the request are exactly the bugs that only
show up in production, and exactly the ones that make D12's "a dead provider is
a swap" false.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from django.test import override_settings

from tests.httpx_transport import patch_httpx_transport
from trends.models import TrendSourceKind
from trends.providers.base import RawTrendItem, TrendVendorError
from trends.providers.fake import FakeTrendVendor
from trends.providers.http import VendorBusyError, parse_timestamp
from trends.providers.vendors import (
    RapidAPIVendor,
    RedditVendor,
    RSSVendor,
    YouTubeVendor,
    get_trend_vendor,
)

SINCE = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


@pytest.fixture(params=[kind for kind in TrendSourceKind if kind != TrendSourceKind.ACCOUNT])
def any_kind(request: Any) -> str:
    return str(request.param)


# -----------------------------------------------------------------------------
# The contract every vendor keeps
# -----------------------------------------------------------------------------
def test_the_fake_keeps_the_contract_for_every_kind(any_kind: str) -> None:
    items = FakeTrendVendor(any_kind).fetch(query={}, since=None, limit=100)

    assert items
    assert all(isinstance(item, RawTrendItem) for item in items)
    assert all(item.external_id for item in items)
    assert all(item.posted_at.tzinfo is not None for item in items)


def test_every_vendor_honours_the_watermark(any_kind: str) -> None:
    vendor = FakeTrendVendor(any_kind)
    everything = vendor.fetch(query={}, since=None, limit=100)
    newest = max(item.posted_at for item in everything)

    assert vendor.fetch(query={}, since=newest, limit=100) == []


def test_every_vendor_honours_the_limit() -> None:
    items = FakeTrendVendor(TrendSourceKind.CREATIVE_CENTER).fetch(
        query={}, since=None, limit=2
    )

    assert len(items) == 2


# -----------------------------------------------------------------------------
# Real wire assembly
# -----------------------------------------------------------------------------
@override_settings(YOUTUBE_API_KEY="yt-key")
def test_youtube_asks_only_for_what_is_new(monkeypatch: Any) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": {"videoId": "abc"},
                        "snippet": {
                            "publishedAt": "2026-08-04T09:00:00Z",
                            "title": "Throwing a mug",
                            "channelTitle": "Pottery",
                        },
                    }
                ]
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    items = YouTubeVendor().fetch(query={"q": "ceramics"}, since=SINCE, limit=10)

    params = captured[-1].url.params
    assert params["publishedAfter"] == "2026-08-01T00:00:00Z"
    assert params["q"] == "ceramics"
    assert params["key"] == "yt-key"
    assert items[0].external_id == "abc"
    assert items[0].media_url.endswith("abc")


@override_settings(TREND_VENDOR_BASE_URL="https://vendor.test", TREND_VENDOR_API_KEY="rk")
def test_rapidapi_passes_the_source_query_through_untouched(monkeypatch: Any) -> None:
    """D12's whole point: the query lives on the row, so retargeting a vendor
    is an admin edit rather than a deploy. If the adapter rewrote it, that
    would stop being true."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ad-9",
                        "published_at": "2026-08-04T09:00:00Z",
                        "caption": "Kiln fired",
                        "video_url": "https://cdn.test/a.mp4",
                        "advertiser": "Clay & Co",
                        "followers": 1200,
                        "language": "en",
                        "metrics": {"likes": 10},
                    },
                    {"published_at": "2026-08-04T09:00:00Z"},
                ]
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    items = RapidAPIVendor("meta-ad-library").fetch(
        query={"q": "ceramics", "country": "PT"}, since=SINCE, limit=5
    )

    request = captured[-1]
    assert request.url.path.endswith("/meta-ad-library")
    assert request.url.params["country"] == "PT"
    assert request.url.params["since"] == SINCE.isoformat()
    assert request.headers["x-rapidapi-key"] == "rk"
    # An entry with no id is not an item — it has no identity to upsert on.
    assert [item.external_id for item in items] == ["ad-9"]
    assert items[0].modality == "VIDEO"
    assert items[0].author_followers == 1200


def test_reddit_filters_client_side_because_its_listing_has_no_since(
    monkeypatch: Any,
) -> None:
    """Not every API can express a lower bound. The watermark still has to be
    honoured, or the pipeline re-reads the whole listing every run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "old",
                                "created_utc": 1_753_000_000,
                                "title": "Older thread",
                                "ups": 5,
                            }
                        },
                        {
                            "data": {
                                "id": "new",
                                "created_utc": 1_786_000_000,
                                "title": "Newer thread",
                                "ups": 900,
                                "num_comments": 30,
                            }
                        },
                    ]
                }
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    items = RedditVendor().fetch(query={"subreddit": "ceramics"}, since=SINCE, limit=10)

    assert [item.external_id for item in items] == ["new"]
    assert items[0].raw_metrics == {"likes": 900, "comments": 30}


def test_rss_reads_a_feed(monkeypatch: Any) -> None:
    feed = """<?xml version="1.0"?><rss><channel>
      <item><guid>a</guid><title>Warm palettes</title>
        <link>https://t.test/a</link><pubDate>2026-08-04T09:00:00Z</pubDate></item>
    </channel></rss>"""

    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(200, text=feed))
    items = RSSVendor().fetch(query={"url": "https://t.test/feed"}, since=SINCE, limit=10)

    assert items[0].external_id == "a"
    assert items[0].body == "Warm palettes"


def test_rss_without_a_url_is_a_configuration_error() -> None:
    with pytest.raises(TrendVendorError):
        RSSVendor().fetch(query={}, since=None, limit=10)


def test_a_malformed_feed_is_a_vendor_error_not_a_crash(monkeypatch: Any) -> None:
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(200, text="not xml"))

    with pytest.raises(TrendVendorError):
        RSSVendor().fetch(query={"url": "https://t.test/feed"}, since=None, limit=10)


@override_settings(YOUTUBE_API_KEY="yt-key")
def test_a_vendor_error_carries_the_status(monkeypatch: Any) -> None:
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(503, text="down"))

    with pytest.raises(TrendVendorError) as caught:
        YouTubeVendor().fetch(query={}, since=None, limit=10)

    assert caught.value.payload["status"] == 503


@override_settings(YOUTUBE_API_KEY="yt-key")
def test_harvesting_yields_to_higher_priority_traffic(monkeypatch: Any) -> None:
    """`trends_q` is the lowest-priority queue (design.md §5.1) and Phase 6
    built the shared bucket that makes that true. A refusal here is a deferral,
    not an incident — the cached corpus is still servable."""
    from common.ratelimit import RateLimitResult

    monkeypatch.setattr(
        "trends.providers.http.ProviderRateLimiter.consume_for_background",
        lambda self, *args, **kwargs: RateLimitResult(allowed=False, remaining=0, retry_after=12),
    )
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(200, json={}))

    with pytest.raises(VendorBusyError):
        YouTubeVendor().fetch(query={}, since=None, limit=10)


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------
def test_the_fake_resolves_under_the_test_settings(any_kind: str) -> None:
    assert isinstance(get_trend_vendor(any_kind), FakeTrendVendor)


@override_settings(USE_FAKE_TREND_VENDORS=False)
def test_each_kind_resolves_to_its_own_adapter() -> None:
    assert isinstance(get_trend_vendor(TrendSourceKind.YOUTUBE), YouTubeVendor)
    assert isinstance(get_trend_vendor(TrendSourceKind.REDDIT), RedditVendor)
    assert isinstance(get_trend_vendor(TrendSourceKind.RSS), RSSVendor)


@override_settings(USE_FAKE_TREND_VENDORS=False)
def test_the_deferred_account_kind_has_no_adapter() -> None:
    """A port entry with nothing behind it is a promise, not a contract (A92) —
    tracked accounts arrive with Phase 11's metrics methods."""
    with pytest.raises(TrendVendorError):
        get_trend_vendor(TrendSourceKind.ACCOUNT)


@pytest.mark.parametrize(
    ("value", "expected_year"),
    [(1_786_000_000, 2026), ("2026-08-04T09:00:00Z", 2026), ("2026-08-04T09:00:00+02:00", 2026)],
)
def test_vendors_disagree_about_time_and_all_three_shapes_parse(
    value: Any, expected_year: int
) -> None:
    assert parse_timestamp(value).year == expected_year


def test_an_unreadable_timestamp_keeps_the_item() -> None:
    """A wrong recency score is fixed by the next extraction; a dropped item is
    gone until the source re-publishes it."""
    assert parse_timestamp("last tuesday") is not None
