"""YouTube Data API v3 (ported from trendgen's `apps/collectors/youtube.py`).

Two shapes of call, because the API charges very differently for them:
`videos.list?chart=mostPopular` costs 1 quota unit and answers "what is big in
this market right now"; `search.list` costs **100** and is the only way to ask a
category's question. The day's default quota is 10,000 units, so `plan` spends
the budget declared on the source row and stops — a long `search_terms` list is
truncated rather than silently burning the day.

`search.list` returns no statistics, so its ids are hydrated with one
`videos.list` call (1 more unit) — the same two-step trendgen uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.conf import settings

from playbook.collectors.base import CollectedPost, CollectRequest
from playbook.collectors.http import (
    as_int,
    client,
    get_json,
    iso_duration_seconds,
    normalise_hashtags,
    parse_timestamp,
    require_credentials,
)
from playbook.models import PostFormat

if TYPE_CHECKING:
    from playbook.models import PlaybookSource

BASE_URL = "https://www.googleapis.com/youtube/v3"

#: Documented quota costs — the API's numbers, not ours. What a source may
#: spend lives on its `config`.
UNITS_POPULAR = 1
UNITS_SEARCH = 100
DEFAULT_RUN_UNITS = 201
DEFAULT_PAGE_SIZE = 25
#: A clip under a minute is filed as a short. A fact about the clip, not a
#: guess about how YouTube labelled it.
SHORT_MAX_SECONDS = 60
#: Optional search parameters a source row may set, passed through verbatim.
SEARCH_PASSTHROUGH = ("relevanceLanguage", "videoDuration", "publishedAfter")


class YouTubeCollector:
    provider = "youtube"
    #: Shares and saves are not published at all.
    reports = frozenset({"views", "likes", "comments"})

    def plan(self, source: PlaybookSource) -> list[CollectRequest]:
        config = source.config or {}
        budget = int(config.get("run_quota_units", DEFAULT_RUN_UNITS))
        size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
        requests: list[CollectRequest] = []
        spent = 0

        if config.get("chart") and spent + UNITS_POPULAR <= budget:
            params: dict[str, Any] = {
                "endpoint": "videos",
                "chart": "mostPopular",
                "regionCode": source.region,
                "maxResults": size,
            }
            if config.get("video_category_id"):
                params["videoCategoryId"] = str(config["video_category_id"])
            requests.append(CollectRequest(self.provider, params=params, config=config))
            spent += UNITS_POPULAR

        # Searches cost 100x, so they are taken in declared order and stop at
        # the budget (plus the 1-unit hydration each one needs).
        for term in config.get("search_terms", []):
            if spent + UNITS_SEARCH + 1 > budget:
                break
            params = {
                "endpoint": "search",
                "q": str(term),
                "regionCode": source.region,
                "maxResults": size,
                **{key: config[key] for key in SEARCH_PASSTHROUGH if config.get(key)},
            }
            requests.append(CollectRequest(self.provider, params=params, config=config))
            spent += UNITS_SEARCH + 1
        return requests

    def fetch(self, request: CollectRequest) -> Iterable[CollectedPost]:
        require_credentials(self.provider)
        params = dict(request.params)
        endpoint = params.pop("endpoint")
        key = settings.YOUTUBE_API_KEY
        with client(base_url=BASE_URL) as http:
            if endpoint == "search":
                found = get_json(
                    self.provider,
                    http,
                    "/search",
                    params={
                        **params,
                        "part": "id",
                        "type": "video",
                        "order": "viewCount",
                        "key": key,
                    },
                )
                ids = [
                    item["id"]["videoId"]
                    for item in found.get("items", [])
                    if item.get("id", {}).get("videoId")
                ]
                if not ids:
                    return []
                payload = get_json(
                    self.provider,
                    http,
                    "/videos",
                    params={
                        "part": "snippet,statistics,contentDetails",
                        "id": ",".join(ids),
                        "key": key,
                    },
                )
            else:
                payload = get_json(
                    self.provider,
                    http,
                    "/videos",
                    params={**params, "part": "snippet,statistics,contentDetails", "key": key},
                )
        return [self.item(entry) for entry in payload.get("items", []) if entry.get("id")]

    @staticmethod
    def item(entry: dict[str, Any]) -> CollectedPost:
        snippet = entry.get("snippet", {})
        stats = entry.get("statistics", {})
        thumbs = snippet.get("thumbnails", {})
        best = (
            thumbs.get("maxres")
            or thumbs.get("high")
            or thumbs.get("medium")
            or thumbs.get("default")
            or {}
        )
        seconds = iso_duration_seconds(entry.get("contentDetails", {}).get("duration"))
        video_id = str(entry["id"])
        return CollectedPost(
            external_id=video_id,
            posted_at=parse_timestamp(snippet.get("publishedAt")),
            format=(
                PostFormat.SHORT
                if seconds is not None and seconds <= SHORT_MAX_SECONDS
                else PostFormat.VIDEO
            ),
            url=f"https://www.youtube.com/watch?v={video_id}",
            title=str(snippet.get("title") or "")[:300],
            caption=str(snippet.get("description") or ""),
            thumbnail_url=str(best.get("url") or ""),
            creator=str(snippet.get("channelTitle") or ""),
            hashtags=normalise_hashtags(snippet.get("tags") or []),
            lang=str(snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or ""),
            duration_seconds=seconds,
            # Left unset where unpublished — a hidden like count is null, not 0.
            views=as_int(stats.get("viewCount")),
            likes=as_int(stats.get("likeCount")),
            comments=as_int(stats.get("commentCount")),
            payload=entry,
        )
