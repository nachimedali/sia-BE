"""The four real `TrendVendor` adapters (D12).

Each one does the same three things — ask, unwrap, translate into
`RawTrendItem` — and nothing else. No scoring, no filtering beyond the
watermark, no opinions: those belong to the pipeline, which must behave
identically whichever vendor answered.

* `RapidAPIVendor` — Meta Ad Library and TikTok Creative Center, through a data
  vendor rather than directly. That is D12's point: the ToS exposure of
  scraping those two sits with the vendor, and the `query` JSON on the source
  row is passed through, so a changed vendor parameter is a row edit.
* `YouTubeVendor` — the YouTube Data API, used directly (no ToS obstacle).
* `RedditVendor` — Reddit's public listing JSON, likewise.
* `RSSVendor` — a feed. No key, no vendor, no quota.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import Any
from xml.etree.ElementTree import ParseError

from django.conf import settings

from trends.models import TrendModality, TrendSourceKind
from trends.providers.base import RawTrendItem, TrendVendor, TrendVendorError
from trends.providers.http import client, get_json, newer_than, parse_timestamp


class RapidAPIVendor:
    """Ad Library and Creative Center, via the configured data vendor."""

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        with client(
            base_url=settings.TREND_VENDOR_BASE_URL,
            headers={"x-rapidapi-key": settings.TREND_VENDOR_API_KEY},
        ) as http:
            payload = get_json(
                "rapidapi",
                http,
                f"/{self.dataset}",
                params={
                    **query,
                    "limit": limit,
                    **({"since": since.isoformat()} if since else {}),
                },
            )

        return [
            RawTrendItem(
                external_id=str(entry.get("id", "")),
                posted_at=parse_timestamp(entry.get("published_at")),
                body=str(entry.get("caption") or entry.get("text") or ""),
                modality=TrendModality.VIDEO if entry.get("video_url") else TrendModality.IMAGE,
                author_handle=str(entry.get("advertiser") or entry.get("author") or ""),
                author_followers=int(entry.get("followers") or 0),
                media_url=str(entry.get("video_url") or entry.get("image_url") or ""),
                lang=str(entry.get("language") or ""),
                raw_metrics=dict(entry.get("metrics") or {}),
            )
            for entry in payload.get("items", [])
            if entry.get("id")
        ]


class YouTubeVendor:
    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "order": "viewCount",
            "maxResults": min(limit, 50),
            "key": settings.YOUTUBE_API_KEY,
            **query,
        }
        if since:
            params["publishedAfter"] = since.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")

        with client(base_url="https://www.googleapis.com/youtube/v3") as http:
            payload = get_json("youtube", http, "/search", params=params)

        return [
            RawTrendItem(
                external_id=str(entry.get("id", {}).get("videoId", "")),
                posted_at=parse_timestamp(entry.get("snippet", {}).get("publishedAt")),
                body=str(entry.get("snippet", {}).get("title", "")),
                modality=TrendModality.VIDEO,
                author_handle=str(entry.get("snippet", {}).get("channelTitle", "")),
                media_url=(
                    f"https://www.youtube.com/watch?v={entry.get('id', {}).get('videoId', '')}"
                ),
                lang=str(entry.get("snippet", {}).get("defaultLanguage", "")),
                raw_metrics={},
            )
            for entry in payload.get("items", [])
            if entry.get("id", {}).get("videoId")
        ]


class RedditVendor:
    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        subreddit = str(query.get("subreddit", "all"))
        with client(
            base_url="https://www.reddit.com", headers={"User-Agent": settings.REDDIT_USER_AGENT}
        ) as http:
            payload = get_json(
                "reddit",
                http,
                f"/r/{subreddit}/top.json",
                params={"t": str(query.get("t", "week")), "limit": limit},
            )

        items = [
            RawTrendItem(
                external_id=str(child.get("data", {}).get("id", "")),
                posted_at=parse_timestamp(child.get("data", {}).get("created_utc")),
                body=str(child.get("data", {}).get("title", "")),
                author_handle=str(child.get("data", {}).get("author", "")),
                media_url=str(child.get("data", {}).get("url", "") or ""),
                raw_metrics={
                    "likes": int(child.get("data", {}).get("ups") or 0),
                    "comments": int(child.get("data", {}).get("num_comments") or 0),
                },
            )
            for child in payload.get("data", {}).get("children", [])
            if child.get("data", {}).get("id")
        ]
        # Reddit's listing has no `since`, so the watermark is applied here.
        return newer_than(items, since, key=lambda item: item.posted_at)


class RSSVendor:
    def fetch(
        self, *, query: dict[str, Any], since: dt.datetime | None, limit: int
    ) -> list[RawTrendItem]:
        url = str(query.get("url", ""))
        if not url:
            raise TrendVendorError("This RSS source has no feed url.", detail={"query": query})

        with client(base_url="") as http:
            response = http.get(url)
            if response.status_code >= 400:
                raise TrendVendorError(
                    f"RSS feed returned {response.status_code}.", detail={"url": url}
                )
            body = response.text

        try:
            root = ET.fromstring(body)
        except ParseError as error:
            raise TrendVendorError("RSS feed is not valid XML.", detail={"url": url}) from error

        items = [
            RawTrendItem(
                external_id=_text(entry, "guid") or _text(entry, "link"),
                posted_at=parse_timestamp(_text(entry, "pubDate")),
                body=_text(entry, "title"),
                media_url=_text(entry, "link"),
            )
            for entry in root.iter("item")
            if _text(entry, "guid") or _text(entry, "link")
        ]
        return newer_than(items[:limit], since, key=lambda item: item.posted_at)


def _text(entry: ET.Element, tag: str) -> str:
    found = entry.find(tag)
    return (found.text or "").strip() if found is not None else ""


#: `TrendSource.kind` → the adapter that speaks to it. A dict rather than a
#: chain of `if`s so an unknown kind is a `KeyError` at the one place that
#: resolves them, not a silently-skipped source.
_VENDORS: dict[str, Any] = {
    TrendSourceKind.ADLIB: lambda: RapidAPIVendor("meta-ad-library"),
    TrendSourceKind.CREATIVE_CENTER: lambda: RapidAPIVendor("tiktok-creative-center"),
    TrendSourceKind.YOUTUBE: YouTubeVendor,
    TrendSourceKind.REDDIT: RedditVendor,
    TrendSourceKind.RSS: RSSVendor,
}


def get_trend_vendor(kind: str) -> TrendVendor:
    """Resolves the adapter for a source kind. Swapping the whole set for
    deterministic fakes is a settings change (A8), exactly like the AI and
    publishing ports.

    `ACCOUNT` is deliberately absent: a workspace's own tracked accounts are
    read through the `PlatformAdapter`'s metrics methods, which land with Phase
    11. A port entry with nothing behind it would be a promise, not a contract
    (A92), so the kind stays unreachable until the phase that can serve it.
    """
    if getattr(settings, "USE_FAKE_TREND_VENDORS", False):
        from trends.providers.fake import get_fake_vendor

        return get_fake_vendor(kind)

    factory = _VENDORS.get(kind)
    if factory is None:
        raise TrendVendorError("No vendor is configured for this source.", detail={"kind": kind})
    vendor: TrendVendor = factory()
    return vendor
