"""Google Trends' daily searches, from the public RSS feed (ported from trendgen).

`trends.google.com/trending/rss?geo=XX` is published, needs no key and returns
the day's rising searches — the one provider whose real adapter runs on a fresh
checkout. Not `pytrends`: an unofficial client over an undocumented endpoint
would add a second fragility to a source that is already the most fragile one.

A search has a topic, a traffic bucket and the news stories Google attached to
it. It has no picture of its own: `ht:picture` is a news outlet's photograph,
kept with its credit and shown as such, and never handed to an image model.

**This source is allowed to fail.** Its error is isolated to its own source, so
YouTube still fills the wall.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import quote_plus
from xml.etree.ElementTree import ParseError

from playbook.collectors.base import CollectedPost, CollectorError, CollectRequest
from playbook.collectors.http import (
    as_int,
    client,
    fetch_response,
    normalise_hashtags,
    parse_timestamp,
)
from playbook.models import PostFormat

if TYPE_CHECKING:
    from playbook.models import PlaybookSource

BASE_URL = "https://trends.google.com"
NAMESPACE = {"ht": "https://trends.google.com/trending/rss"}
MAX_NEWS = 3


class GoogleTrendsCollector:
    provider = "google_trends"
    #: A daily trend list carries no engagement at all.
    reports: frozenset[str] = frozenset()

    def plan(self, source: PlaybookSource) -> list[CollectRequest]:
        geo = str((source.config or {}).get("region_override") or source.region).upper()
        return [CollectRequest(self.provider, params={"geo": geo}, config=source.config or {})]

    def fetch(self, request: CollectRequest) -> Iterable[CollectedPost]:
        geo = request.params["geo"]
        with client(base_url=BASE_URL) as http:
            response = fetch_response(self.provider, http, "/trending/rss", params={"geo": geo})
        try:
            root = ET.fromstring(response.text)
        except ParseError as exc:
            raise CollectorError(
                "google_trends returned unparseable XML", detail={"provider": self.provider}
            ) from exc
        return [post for post in (self.item(node, geo) for node in root.iter("item")) if post]

    @staticmethod
    def item(node: ET.Element, geo: str) -> CollectedPost | None:
        title = (node.findtext("title") or "").strip()
        if not title:
            return None
        traffic = (node.findtext("ht:approx_traffic", namespaces=NAMESPACE) or "").strip()
        news = [
            {
                "title": (entry.findtext("ht:news_item_title", namespaces=NAMESPACE) or "").strip(),
                "url": (entry.findtext("ht:news_item_url", namespaces=NAMESPACE) or "").strip(),
                "source": (
                    entry.findtext("ht:news_item_source", namespaces=NAMESPACE) or ""
                ).strip(),
            }
            for entry in node.findall("ht:news_item", namespaces=NAMESPACE)
        ][:MAX_NEWS]
        news = [story for story in news if story["title"]]
        return CollectedPost(
            external_id=f"{geo}:{title.lower()}"[:200],
            posted_at=parse_timestamp(node.findtext("pubDate")),
            format=PostFormat.SEARCH,
            # The feed's own link is the RSS URL; a person wants the search.
            url=f"https://www.google.com/search?q={quote_plus(title)}",
            title=title[:300],
            caption=" · ".join(story["title"] for story in news),
            thumbnail_url=(node.findtext("ht:picture", namespaces=NAMESPACE) or "").strip(),
            thumbnail_credit=(
                node.findtext("ht:picture_source", namespaces=NAMESPACE) or ""
            ).strip(),
            hashtags=normalise_hashtags([title]),
            # "200,000+" is a bucket, not a measurement: the floor is stored as
            # a floor and the label as written, never rounded into a count.
            traffic_floor=as_int(traffic.replace("+", "").replace(",", "")),
            traffic_label=traffic,
            news=news,
            payload={"approx_traffic": traffic, "geo": geo},
        )
