"""The two free providers, against recorded payloads — no network.

What each adapter must get right is the mapping, and above all what it leaves
out: a like count a channel hid is null, never 0 (rule 4); a search's traffic
bucket is a floor, never a count; a news photograph is credited, never the
search's own picture.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from playbook.collectors import registry
from playbook.collectors.base import CollectorError, CollectRequest
from playbook.collectors.fake import FakeCollector
from playbook.collectors.google_trends import GoogleTrendsCollector
from playbook.collectors.http import iso_duration_seconds
from playbook.collectors.youtube import YouTubeCollector
from playbook.models import PlaybookSource, PostFormat

pytestmark = pytest.mark.django_db

VIDEO: dict[str, Any] = {
    "id": "abc123",
    "snippet": {
        "publishedAt": "2026-10-10T08:00:00Z",
        "title": "Pouring the first press at sunrise",
        "description": "Olive harvest in Midoun.",
        "channelTitle": "Djerba Olive Co.",
        "tags": ["Olive Oil", "#Djerba"],
        "thumbnails": {
            "high": {"url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"},
            "maxres": {"url": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"},
        },
    },
    "statistics": {"viewCount": "120000", "commentCount": "88"},
    "contentDetails": {"duration": "PT45S"},
}

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item>
  <title>Olive harvest</title>
  <ht:approx_traffic>2,000+</ht:approx_traffic>
  <pubDate>Sat, 10 Oct 2026 06:00:00 +0100</pubDate>
  <ht:picture>https://encrypted-tbn0.gstatic.com/images?q=1</ht:picture>
  <ht:picture_source>La Presse</ht:picture_source>
  <ht:news_item>
    <ht:news_item_title>The harvest starts early</ht:news_item_title>
    <ht:news_item_url>https://example.tn/harvest</ht:news_item_url>
    <ht:news_item_source>La Presse</ht:news_item_source>
  </ht:news_item>
</item>
<item><title></title></item>
</channel></rss>"""


# --- YouTube ---------------------------------------------------------------------------
def test_a_video_maps_with_hidden_counts_left_null() -> None:
    post = YouTubeCollector.item(VIDEO)

    assert post.format == PostFormat.SHORT
    assert post.duration_seconds == 45
    assert post.thumbnail_url.endswith("maxresdefault.jpg")
    assert post.views == 120000
    assert post.comments == 88
    assert post.likes is None  # hidden by the channel: unknown, not zero
    assert post.hashtags == ["#djerba", "#oliveoil"]
    assert post.url == "https://www.youtube.com/watch?v=abc123"


def test_a_video_over_a_minute_is_not_a_short() -> None:
    longer = {**VIDEO, "contentDetails": {"duration": "PT4M2S"}}
    assert YouTubeCollector.item(longer).format == PostFormat.VIDEO


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT45S", 45), ("PT1M3S", 63), ("PT1H2M", 3720), ("P0D", None), ("", None), ("x", None)],
)
def test_iso_durations(value: str, seconds: int | None) -> None:
    assert iso_duration_seconds(value) == seconds


def test_plan_spends_the_rows_budget_and_stops(food: Any) -> None:
    source = PlaybookSource(
        provider="youtube",
        region="TN",
        category=food,
        config={
            "chart": True,
            "search_terms": ["recipe", "harissa", "tea"],
            "run_quota_units": 202,
        },
    )
    planned = YouTubeCollector().plan(source)

    # 1 for the chart + 101 for the first search; the second would need 203.
    assert [r.params["endpoint"] for r in planned] == ["videos", "search"]
    assert planned[1].params["q"].startswith("recipe (")
    assert "-trailer" in planned[1].params["q"]
    assert planned[1].params["videoDuration"] == "short"
    assert all(r.params["regionCode"] == "TN" for r in planned)


def _video(video_id: str, title: str, description: str = "") -> dict[str, Any]:
    return {
        **VIDEO,
        "id": video_id,
        "snippet": {**VIDEO["snippet"], "title": title, "description": description},
    }


@respx.mock
@pytest.mark.parametrize("endpoint", ["search", "videos"])
def test_only_ads_reach_the_wall(settings: Any, endpoint: str) -> None:
    """Both the chart and a search: a trailer out-views every ad, so the filter
    cannot live in the query alone."""
    settings.YOUTUBE_API_KEY = "key"
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(
        return_value=httpx.Response(200, json={"items": [{"id": {"videoId": "a"}}]})
    )
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    _video("a", "Olive oil TV commercial"),
                    _video("b", "Official Trailer", "an ad for the film"),
                    _video("c", "Harvest vlog", "Sponsored by Acme"),
                    _video("d", "Harvest vlog", "just a day out"),
                    _video("e", "TORTILLA KEBAB IN FRIGGITRICE AD ARIA"),  # Italian "ad"
                    _video("f", "Girish AD vs Rahul #shorts"),
                ]
            },
        )
    )

    posts = list(YouTubeCollector().fetch(CollectRequest("youtube", params={"endpoint": endpoint})))

    assert [p.external_id for p in posts] == ["a", "c"]


@respx.mock
def test_a_row_can_opt_out_of_the_ad_filter(settings: Any) -> None:
    settings.YOUTUBE_API_KEY = "key"
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(200, json={"items": [_video("d", "Harvest vlog")]})
    )

    posts = list(
        YouTubeCollector().fetch(
            CollectRequest("youtube", params={"endpoint": "videos"}, config={"ads_only": False})
        )
    )

    assert [p.external_id for p in posts] == ["d"]


@respx.mock
def test_a_search_is_hydrated_with_statistics(settings: Any) -> None:
    settings.YOUTUBE_API_KEY = "key"
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(
        return_value=httpx.Response(200, json={"items": [{"id": {"videoId": "abc123"}}]})
    )
    videos = respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(200, json={"items": [_video("abc123", "Olive oil commercial")]})
    )

    posts = list(
        YouTubeCollector().fetch(CollectRequest("youtube", params={"endpoint": "search", "q": "x"}))
    )

    assert [p.external_id for p in posts] == ["abc123"]
    assert videos.calls.last.request.url.params["id"] == "abc123"


def test_a_missing_key_fails_before_the_wire(settings: Any) -> None:
    settings.YOUTUBE_API_KEY = ""
    with pytest.raises(CollectorError, match="not configured"):
        list(YouTubeCollector().fetch(CollectRequest("youtube", params={"endpoint": "videos"})))


@respx.mock
def test_a_provider_error_is_a_collector_error(settings: Any) -> None:
    settings.YOUTUBE_API_KEY = "key"
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(403, json={"error": "quota"})
    )
    with pytest.raises(CollectorError, match="403"):
        list(YouTubeCollector().fetch(CollectRequest("youtube", params={"endpoint": "videos"})))


# --- Google Trends ---------------------------------------------------------------------
@respx.mock
def test_a_search_trend_keeps_its_bucket_and_credits_its_photo() -> None:
    respx.get("https://trends.google.com/trending/rss").mock(
        return_value=httpx.Response(200, text=RSS)
    )

    posts = list(
        GoogleTrendsCollector().fetch(CollectRequest("google_trends", params={"geo": "TN"}))
    )

    assert len(posts) == 1  # the untitled item is dropped, not stored blank
    post = posts[0]
    assert post.format == PostFormat.SEARCH
    assert post.traffic_floor == 2000
    assert post.traffic_label == "2,000+"
    assert post.views is None and post.likes is None
    assert post.thumbnail_credit == "La Presse"
    assert post.news == [
        {
            "title": "The harvest starts early",
            "url": "https://example.tn/harvest",
            "source": "La Presse",
        }
    ]
    assert post.url == "https://www.google.com/search?q=Olive+harvest"
    assert post.external_id == "TN:olive harvest"


@respx.mock
def test_unreadable_rss_is_a_collector_error() -> None:
    respx.get("https://trends.google.com/trending/rss").mock(
        return_value=httpx.Response(200, text="<rss><broken")
    )
    with pytest.raises(CollectorError):
        list(GoogleTrendsCollector().fetch(CollectRequest("google_trends", params={"geo": "TN"})))


# --- registry --------------------------------------------------------------------------
def test_an_unconfigured_provider_uses_the_fake_only_where_samples_show(settings: Any) -> None:
    settings.USE_FAKE_PLAYBOOK_COLLECTORS = False
    settings.YOUTUBE_API_KEY = ""

    settings.PLAYBOOK_SHOW_SAMPLE = True
    assert isinstance(registry.get_collector("youtube"), FakeCollector)

    # Production: the real adapter answers and refuses before the wire, rather
    # than writing fixture rows into a real database.
    settings.PLAYBOOK_SHOW_SAMPLE = False
    assert isinstance(registry.get_collector("youtube"), YouTubeCollector)
    # Google Trends needs no key, so it is real whenever fakes are not forced.
    assert isinstance(registry.get_collector("google_trends"), GoogleTrendsCollector)


def test_the_fake_marks_its_rows_and_leaves_unreported_counts_null(market_source: Any) -> None:
    fake = FakeCollector("youtube")
    posts = [p for r in fake.plan(market_source) for p in fake.fetch(r)]

    assert posts and all(p.is_fake for p in posts)
    assert any(p.likes is None for p in posts)
    search = FakeCollector("google_trends").fetch(
        CollectRequest("google_trends", params={"geo": "TN"})
    )
    assert all(p.views is None and p.traffic_floor for p in search)
