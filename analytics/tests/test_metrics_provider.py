"""The measurement port's contract (P0-27, P0-30, P0-31, P0-32, P0-38).

Both implementations answer the same questions, and the whole file is written
around one rule: **absence is null, never zero.** The test this replaces —
`test_a_platform_that_reports_nothing_yields_zeroes_not_guesses` — asserted the
opposite, and asserted it correctly against code that was wrong. C-07 is the
correction; this is where it is pinned.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from analytics.providers.base import MetricsProviderRegistry, MetricsUnsupportedError
from analytics.providers.fake import FakeMetricsProvider
from analytics.providers.zernio import ZernioMetricsProvider, read_metric
from tests.httpx_transport import patch_httpx_transport


@pytest.fixture(params=["fake", "zernio"])
def provider(request: Any) -> Any:
    return FakeMetricsProvider() if request.param == "fake" else ZernioMetricsProvider()


# -----------------------------------------------------------------------------
# Null, never zero — C-07
# -----------------------------------------------------------------------------
def test_an_absent_metric_reads_as_none_not_zero() -> None:
    """The defect P0-38 named, at its source. `_count` returned `0` here and
    the docstring said so: "A platform that reports nothing leaves every field
    at zero." Reddit, Bluesky, Telegram, Snapchat and LinkedIn personal
    accounts were all recording hard zeros indistinguishable from measured
    ones."""
    assert read_metric({}, "impressions") is None
    assert read_metric({"likes": 5}, "impressions") is None


def test_a_reported_zero_is_still_zero() -> None:
    """The other half. Null is for absence; a platform that said zero measured
    zero, and flattening that to null would discard a real observation."""
    assert read_metric({"impressions": 0}, "impressions") == 0


def test_an_explicit_null_falls_through_to_the_next_alias() -> None:
    """Platforms disagree about what a number is called. A key that is present
    but null is not an answer, so the alias chain keeps looking rather than
    stopping at it."""
    assert read_metric({"impressions": None, "views": 42}, "impressions") == 42


def test_an_unparseable_value_is_none_rather_than_zero() -> None:
    """ "The vendor sent something we could not read" is a gap, not a
    measurement of nothing."""
    assert read_metric({"likes": "many"}, "likes") is None


# -----------------------------------------------------------------------------
# Both implementations
# -----------------------------------------------------------------------------
def test_every_provider_declares_a_key_and_a_schema_version(provider: Any) -> None:
    """Both are stored on every row (A-16). A provider without them produces
    snapshots whose provenance cannot be reconstructed."""
    assert provider.key
    assert provider.schema_version >= 1


def test_no_provider_carries_a_publish_method(provider: Any) -> None:
    for forbidden in ("publish", "connect_url", "resolve_callback", "select_target"):
        assert not hasattr(provider, forbidden), f"{forbidden} belongs to the publish port"


def test_tiktok_reports_no_comment_endpoint(provider: Any) -> None:
    """L-3: TikTok exposes no comment endpoint at any price. Silence there is
    our vendor's coverage, not the audience's opinion (P0-03)."""
    assert provider.supports_comments("tiktok") is False
    assert provider.supports_comments("instagram") is True


def test_asking_for_tiktok_comments_raises_rather_than_returning_empty(provider: Any) -> None:
    """An empty list would be indistinguishable from a post nobody commented
    on. The caller has to be forced to record unavailability instead."""
    with pytest.raises(MetricsUnsupportedError):
        provider.fetch_comments(platform="tiktok", provider_post_id="p-1")


# -----------------------------------------------------------------------------
# The fake
# -----------------------------------------------------------------------------
def test_the_fake_models_growth_between_captures() -> None:
    """The one property the analytics pipeline actually depends on: §8.9's
    decay slope is computed from the *differences* between captures, so a fake
    returning a constant would make every evergreen-vs-spike test pass without
    the classification working."""
    fake = FakeMetricsProvider()

    likes = [
        fake.fetch(platform="instagram", provider_post_id="p-1").body["likes"] for _ in range(3)
    ]

    assert likes[0] < likes[1] < likes[2]
    # Decelerating, so a post left alone converges rather than climbing forever.
    assert (likes[2] - likes[1]) < (likes[1] - likes[0])


def test_the_fake_can_be_told_a_platform_reports_nothing() -> None:
    """How the null paths get exercised without a live vendor."""
    fake = FakeMetricsProvider()
    fake.unsupported.add("linkedin")

    payload = fake.fetch(platform="linkedin", provider_post_id="p-1")

    assert payload.availability == "UNAVAILABLE"
    assert payload.body == {}


# -----------------------------------------------------------------------------
# Zernio
# -----------------------------------------------------------------------------
def test_zernio_reads_a_posts_analytics(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"metrics": {"views": 5000, "reactions": 120, "replies": 8, "reposts": 3}},
        )

    patch_httpx_transport(monkeypatch, handler)
    payload = ZernioMetricsProvider().fetch(platform="instagram", provider_post_id="zp-1")

    # Each platform's own vocabulary, translated on read: `views` is this
    # platform's word for impressions, `reactions` for likes.
    assert payload.availability == "MEASURED"
    assert read_metric(payload.body, "impressions") == 5000
    assert read_metric(payload.body, "likes") == 120
    assert read_metric(payload.body, "comments") == 8
    assert read_metric(payload.body, "shares") == 3
    # And the ones this platform did not report stay absent.
    assert read_metric(payload.body, "saves") is None


def test_a_202_is_pending_not_a_failure(monkeypatch: Any) -> None:
    """P0-32. Zernio's own sync has not finished. The old `_json` helper
    treated this as a provider error, so a pending sync read as an outage."""
    patch_httpx_transport(
        monkeypatch, lambda request: httpx.Response(202, json={}, headers={"Retry-After": "30"})
    )

    payload = ZernioMetricsProvider().fetch(platform="instagram", provider_post_id="zp-1")

    assert payload.availability == "PENDING"
    assert payload.retry_after == 30
    assert payload.body == {}


def test_a_424_is_unavailable_and_carries_no_numbers(monkeypatch: Any) -> None:
    """Every platform behind the request failed. That is an answer —
    unavailable — and it must not be recorded as a row of zeros."""
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(424, json={}))

    payload = ZernioMetricsProvider().fetch(platform="instagram", provider_post_id="zp-1")

    assert payload.availability == "UNAVAILABLE"
    assert payload.body == {}


def test_the_delta_feed_returns_snapshots_and_a_cursor(monkeypatch: Any) -> None:
    """P0-31: one call for every account that moved, rather than one per post."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"postId": "zp-1", "platform": "instagram", "metrics": {"views": 10}},
                    {"postId": "zp-2", "platform": "threads", "metrics": {"views": 20}},
                    {"platform": "threads", "metrics": {"views": 30}},
                ],
                "nextCursor": "cur-2",
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    payloads, cursor = ZernioMetricsProvider().changed_since("cur-1")

    assert [p.provider_post_id for p in payloads] == ["zp-1", "zp-2"]
    assert cursor == "cur-2"


def test_a_warming_delta_feed_does_not_advance_the_cursor(monkeypatch: Any) -> None:
    """Advancing past a page the vendor is still assembling would skip it, and
    the feed is a rolling window that cannot be rewound."""
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(202, json={}))

    payloads, cursor = ZernioMetricsProvider().changed_since("cur-1")

    assert payloads == []
    assert cursor == "cur-1"


def test_zernio_reads_comments_and_applies_the_watermark(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "comments": [
                    {"id": "c-old", "text": "older", "createdAt": "2026-08-01T09:00:00Z"},
                    {"id": "c-new", "text": "newer", "createdAt": "2026-08-09T09:00:00Z"},
                    {"text": "no id, not an item"},
                ]
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    since = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)

    comments = ZernioMetricsProvider().fetch_comments(
        platform="instagram", provider_post_id="zp-1", since=since
    )

    assert [c.external_id for c in comments] == ["c-new"]


def test_reactions_are_none_where_the_platform_does_not_break_them_down() -> None:
    """`None` and `{}` are different answers, and L-4a's tiering depends on the
    difference: one renders "unavailable", the other renders "no reactions"."""
    assert ZernioMetricsProvider().fetch_reactions(platform="tiktok", provider_post_id="p") is None


# -----------------------------------------------------------------------------
# The registry
# -----------------------------------------------------------------------------
def test_an_uncovered_platform_resolves_to_no_provider() -> None:
    """A-19: a platform nobody measures reports unavailable, never zero. The
    mechanism is `resolve` returning `None`, not a caller remembering."""
    fake = FakeMetricsProvider()
    fake.unsupported.add("linkedin")
    registry = MetricsProviderRegistry([fake])

    assert registry.resolve("instagram", "likes") is fake
    assert registry.resolve("linkedin", "likes") is None
    assert registry.for_platform("linkedin") is None


def test_registering_the_same_key_twice_replaces_rather_than_duplicates() -> None:
    registry = MetricsProviderRegistry()
    registry.register(FakeMetricsProvider())
    registry.register(FakeMetricsProvider())

    assert len(registry.all()) == 1
