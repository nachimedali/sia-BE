"""`ZernioMetricsProvider` — the measurement half of the Zernio relationship.

Lifted out of `channels.adapters.zernio` (P0-30), which now publishes and
nothing else. Same vendor, same key, two ports: see `providers/base.py` for
why that separation is structural rather than tidy-minded.

**Three things this does that the old `fetch_metrics` did not.**

*It follows a cursor.* `GET /v1/analytics/delta` returns every snapshot that
moved across all accounts since a cursor, which is one call per tick instead
of one per post — the vendor measures 1,599 calls/hour down to 205 over ~1,600
accounts. The feed is a rolling seven-day log and cannot replay history, so
`fetch` remains the bootstrap and the repair path (P0-31).

*It can say "no answer".* `202` means the vendor's own sync is still in
flight, `424` means every platform behind the request failed. The old `_json`
helper raised on both, so a pending sync read as a provider failure. They are
now `PENDING` and `UNAVAILABLE` respectively, and neither writes a row of
zeros (P0-32).

*It never invents a zero.* `_count` returned `0` for an absent key and the
docstring said so outright — "A platform that reports nothing leaves every
field at zero." That is C-07, and it was live: Zernio returns nothing for
LinkedIn personal accounts beyond posts it published itself. Absence is now
`None` all the way to a nullable column (P0-38).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.conf import settings
from django.utils import timezone

from analytics.providers.base import (
    METRIC_KEYS,
    CommentSnapshot,
    MetricsError,
    MetricsUnsupportedError,
    RawMetricPayload,
)
from common.timestamps import parse_or_none

#: Bumped when a change to Zernio's response shape changes how the normaliser
#: reads it. Stored on every row so a reprocess knows which mapping wrote it.
SCHEMA_VERSION = 1

PROVIDER_KEY = "zernio"

#: Platforms Zernio reports *no* comments for at any price (L-3). Not a
#: pricing tier and not a bug — TikTok exposes no comment endpoint, so a
#: TikTok target's comment surface is `UNAVAILABLE`, never an empty thread.
NO_COMMENT_PLATFORMS = frozenset({"tiktok"})

#: Platform → the demographics endpoint that answers for it. **Declared, not
#: branched**: a platform missing from this table reports its audience
#: unavailable, which is the honest answer for Threads, LinkedIn and TikTok —
#: Zernio publishes a demographics endpoint for none of them.
DEMOGRAPHIC_ENDPOINTS: dict[str, str] = {
    "instagram": "/v1/analytics/instagram/demographics/{account}",
    "youtube": "/v1/analytics/youtube/demographics/{account}",
    "facebook": "/v1/analytics/facebook/page-insights/{account}",
}

#: `DemographicDimension` value → the key Zernio returns it under. Kept here
#: rather than in the model so the vendor's vocabulary stops at the port.
DEMOGRAPHIC_KEYS: dict[str, str] = {
    "AGE": "ageRanges",
    "GENDER": "genders",
    "COUNTRY": "countries",
    "CITY": "cities",
    "LANGUAGE": "languages",
}

#: Platforms whose analytics Zernio does not cover at all. Empty for the six
#: platforms this system publishes to — Zernio's gaps (Reddit, Bluesky,
#: Telegram, Snapchat) are all platforms we do not support. Kept as a named
#: constant anyway so adding a seventh platform has an obvious place to
#: declare "measurement not covered" rather than silently inheriting coverage.
NO_ANALYTICS_PLATFORMS: frozenset[str] = frozenset()

#: Where the same number lives under different platform vocabularies. Tried in
#: order; the first key actually present wins. A key that is present and null
#: is still an answer of "not reported" and falls through to the next alias.
_ALIASES: dict[str, tuple[str, ...]] = {
    "impressions": ("impressions", "views", "reach"),
    "likes": ("likes", "reactions"),
    "comments": ("comments", "replies"),
    "shares": ("shares", "reposts", "retweets"),
    "clicks": ("clicks", "linkClicks"),
    "saves": ("saves", "bookmarks"),
}


def _client() -> Any:
    """This port builds its own client rather than importing the publish
    adapter's. Ten duplicated lines is the price of the two being independently
    repointable — the whole reason the ports are separate."""
    import httpx

    return httpx.Client(
        base_url=settings.ZERNIO_BASE_URL,
        headers={"Authorization": f"Bearer {settings.ZERNIO_API_KEY}"},
        timeout=settings.ZERNIO_TIMEOUT_SECONDS,
    )


def read_metric(body: Any, metric: str) -> int | None:
    """One metric out of a provider payload, or `None` when it is not there.

    The whole of C-07 in one function. An absent key is not a zero: LinkedIn
    personal accounts report nothing at all for posts Zernio did not publish,
    and a stored `0` there is indistinguishable from a post nobody saw. The
    caller writes whatever comes back straight into a nullable column.

    A present-but-unparseable value is also `None` rather than `0`, for the
    same reason: "the vendor sent us something we could not read" is a gap,
    not a measurement.
    """
    if not isinstance(body, dict):
        return None
    for alias in _ALIASES.get(metric, (metric,)):
        if alias not in body:
            continue
        value = body[alias]
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return None
    return None


class ZernioMetricsProvider:
    key = PROVIDER_KEY
    schema_version = SCHEMA_VERSION

    # --- capability ------------------------------------------------------
    def supports(self, platform: str, metric: str) -> bool:
        """Whether Zernio reports `metric` on `platform` *at all*.

        Deliberately coarse. It answers the documented negatives and nothing
        else — the per-metric truth for a given post is settled by whether the
        key is in that post's payload, which `read_metric` decides without
        anyone having to maintain a matrix of guesses. Declaring coverage we
        have not verified would fabricate exactly the confidence C-07 removes.
        """
        if platform in NO_ANALYTICS_PLATFORMS:
            return False
        return metric in METRIC_KEYS

    def supports_comments(self, platform: str) -> bool:
        return platform not in NO_COMMENT_PLATFORMS

    # --- acquisition -----------------------------------------------------
    def fetch(self, *, platform: str, provider_post_id: str) -> RawMetricPayload:
        if not self.supports(platform, "impressions"):
            return self._unavailable(platform, provider_post_id)

        with _client() as client:
            response = client.get("/v1/analytics", params={"postId": provider_post_id})
        return self._payload_from(response, platform=platform, provider_post_id=provider_post_id)

    def changed_since(self, cursor: str) -> tuple[list[RawMetricPayload], str]:
        """The delta feed (P0-31).

        An empty `cursor` asks for everything the rolling window still holds,
        which is the resumption path after a gap — not a replacement for the
        bootstrap, because the window is seven days and a post older than that
        never appears in it again.
        """
        params = {"cursor": cursor} if cursor else {}
        with _client() as client:
            response = client.get("/v1/analytics/delta", params=params)

        if response.status_code == 202:
            # The feed itself is warming up. No cursor movement: asking again
            # from the same place is correct, and advancing would skip
            # whatever the vendor is still assembling.
            return [], cursor
        body = self._json(response, label="Zernio analytics delta")

        moment = timezone.now()
        payloads = [
            RawMetricPayload(
                provider_key=self.key,
                platform=str(entry.get("platform", "")),
                provider_post_id=str(entry.get("postId", "") or entry.get("_id", "")),
                schema_version=self.schema_version,
                body=entry.get("metrics", entry) if isinstance(entry, dict) else {},
                availability="MEASURED",
                fetched_at=moment,
            )
            for entry in body.get("items", body.get("snapshots", []))
            if isinstance(entry, dict) and (entry.get("postId") or entry.get("_id"))
        ]
        return payloads, str(body.get("nextCursor", "") or cursor)

    # --- audience --------------------------------------------------------
    def fetch_comments(
        self, *, platform: str, provider_post_id: str, since: dt.datetime | None = None
    ) -> list[CommentSnapshot]:
        """Comments on one post. Raises `MetricsUnsupported` where the platform
        has no comment endpoint, so the caller records `UNAVAILABLE` rather
        than an empty list that reads as "nobody commented"."""
        if not self.supports_comments(platform):
            raise MetricsUnsupportedError(
                "This platform exposes no comment endpoint.", detail={"platform": platform}
            )

        with _client() as client:
            body = self._json(
                client.get(f"/v1/inbox/comments/{provider_post_id}"), label="Zernio comments"
            )

        comments = [
            CommentSnapshot(
                external_id=str(entry.get("id", "")),
                body=str(entry.get("text") or entry.get("body") or ""),
                author=str(entry.get("author") or entry.get("username") or ""),
                posted_at=parse_or_none(entry.get("createdAt") or entry.get("timestamp")),
                reactions=_reaction_counts(entry.get("reactions")),
            )
            for entry in body.get("comments", [])
            if entry.get("id")
        ]
        if since is not None:
            comments = [c for c in comments if c.posted_at and c.posted_at > since]
        return comments

    def fetch_reactions(self, *, platform: str, provider_post_id: str) -> dict[str, int] | None:
        """Per-type reaction counts, or `None` where the platform does not
        break them down.

        `None` and `{}` are different answers and the tiering in L-4a depends
        on the difference: `{}` is "we asked and there were none", `None` is
        "this platform does not say", which the surface renders as unavailable.
        """
        endpoint = {
            "facebook": f"/v1/accounts/{provider_post_id}/facebook-post-reactions",
            "linkedin": f"/v1/accounts/{provider_post_id}/linkedin-post-reactions",
        }.get(platform)
        if endpoint is None:
            return None

        with _client() as client:
            response = client.get(endpoint)
        if response.status_code in (202, 424):
            return None
        body = self._json(response, label="Zernio reactions")
        breakdown = body.get("reactions", body)
        if not isinstance(breakdown, dict):
            return None
        counts: dict[str, int] = {}
        for name, value in breakdown.items():
            try:
                counts[str(name)] = max(int(value), 0)
            except (TypeError, ValueError):
                continue
        return counts

    def reply(self, *, platform: str, comment_external_id: str, body: str) -> str:
        with _client() as client:
            payload = self._json(
                client.post(
                    "/v1/inbox/comments/reply",
                    json={"commentId": comment_external_id, "message": body},
                ),
                label="Zernio comment reply",
            )
        return str(payload.get("id", "") or payload.get("replyId", ""))

    def fetch_account_stats(self, *, provider_account_id: str) -> dict[str, int | None]:
        """Follower counts. `None` per field where the account record does not
        carry it — `engagement_rate`'s fallback denominator is better off
        knowing it has no denominator than dividing by a fabricated zero."""
        with _client() as client:
            body = self._json(
                client.get(f"/v1/accounts/{provider_account_id}"), label="Zernio account"
            )
        account = body.get("account", body)
        return {
            "followers": _first(account, "followerCount", "followers"),
            "following": _first(account, "followingCount", "following"),
            "total_posts": _first(account, "postCount", "posts"),
        }

    def fetch_demographics(
        self, *, platform: str, provider_account_id: str
    ) -> dict[str, Any] | None:
        """Who follows this account, or `None` where the vendor will not say.

        **Coverage is a table, not a branch** — `DEMOGRAPHIC_ENDPOINTS` says
        which platforms Zernio answers for at all, and a platform absent from
        it returns `None` rather than an empty breakdown. The caller writes
        that as `UNAVAILABLE`.

        `None` also covers the two ways the vendor itself declines: fewer than
        100 followers, and a breakdown that has not yet caught up (it lags up
        to 48 hours). Both are "we cannot see this", and neither is a zero.
        """
        endpoint = DEMOGRAPHIC_ENDPOINTS.get(platform)
        if endpoint is None:
            return None

        with _client() as client:
            response = client.get(endpoint.format(account=provider_account_id))
        # 404 is the vendor's answer for an account below the follower
        # threshold, and 202 for a breakdown still being computed. Neither is
        # an error worth retrying inside a daily job.
        if response.status_code in (202, 404):
            return None
        body = self._json(response, label="Zernio demographics")

        breakdowns: dict[str, Any] = {}
        for dimension, key in DEMOGRAPHIC_KEYS.items():
            shares = _shares(body.get(key))
            if shares:
                breakdowns[dimension] = shares
        return breakdowns or None

    # --- plumbing --------------------------------------------------------
    def _payload_from(
        self, response: Any, *, platform: str, provider_post_id: str
    ) -> RawMetricPayload:
        """The 202/424 fork (P0-32).

        `202` is the vendor saying its own sync has not finished — the rung
        reschedules and nothing is written, because a row recorded now would
        be a permanent statement about a moment the vendor had not measured.
        `424` is every platform behind the request having failed, which is an
        answer: unavailable, recorded as such, carrying no metric values.
        """
        moment = timezone.now()
        if response.status_code == 202:
            retry_after = response.headers.get("Retry-After", "")
            return RawMetricPayload(
                provider_key=self.key,
                platform=platform,
                provider_post_id=provider_post_id,
                schema_version=self.schema_version,
                availability="PENDING",
                fetched_at=moment,
                retry_after=float(retry_after) if retry_after.isdigit() else None,
            )
        if response.status_code == 424:
            return self._unavailable(platform, provider_post_id)

        body = self._json(response, label="Zernio analytics")
        metrics = body.get("metrics", body)
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            body=metrics if isinstance(metrics, dict) else {},
            availability="MEASURED",
            fetched_at=moment,
        )

    def _unavailable(self, platform: str, provider_post_id: str) -> RawMetricPayload:
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            availability="UNAVAILABLE",
            fetched_at=timezone.now(),
        )

    @staticmethod
    def _json(response: Any, *, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            retry_after = response.headers.get("Retry-After", "")
            raise MetricsError(
                f"{label} returned {response.status_code}.",
                detail={"status": response.status_code, "body": response.text[:500]},
                retryable=response.status_code == 429 or response.status_code >= 500,
                retry_after=float(retry_after) if retry_after.isdigit() else None,
            )
        parsed: dict[str, Any] = response.json()
        return parsed


def _first(payload: Any, *names: str) -> int | None:
    """The first of `names` this payload carries, as an int, else `None`.

    Not `read_metric`: these are account-level keys with their own aliases,
    and chaining `read_metric(...) or read_metric(...)` would turn a genuine
    zero-follower account into a `None` on the first alias's falsiness.
    """
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return None
    return None


def _reaction_counts(value: Any) -> dict[str, int] | None:
    """A reaction breakdown, or `None` when there was not one.

    `None` and `{}` are different answers: the first says this platform does
    not break reactions down, the second says it does and there were none.
    Collapsing them is the C-07 mistake wearing a different hat.
    """
    if not isinstance(value, dict):
        return None
    counts: dict[str, int] = {}
    for name, raw in value.items():
        try:
            counts[str(name)] = max(int(raw), 0)
        except (TypeError, ValueError):
            continue
    return counts


def _shares(raw: Any) -> dict[str, float] | None:
    """A `{bucket: share}` map summing to 1, from either counts or shares.

    Zernio returns absolute follower counts on some dimensions and percentages
    on others, so normalising here is what stops every reader downstream from
    having to guess which it is holding. Shares rather than counts because a
    demographic breakdown is only ever read as a proportion, and a count would
    additionally leak the follower total into a chart that never uses it.

    `None` for a payload that is missing, malformed or entirely zero — an
    all-zero breakdown is the vendor saying it has nothing, and dividing by it
    would be the fabricated row Part 7 rule 12 forbids.
    """
    if not isinstance(raw, dict):
        return None

    values: dict[str, float] = {}
    for bucket, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            values[str(bucket)] = number

    total = sum(values.values())
    if not values or total <= 0:
        return None
    return {bucket: round(number / total, 4) for bucket, number in values.items()}
