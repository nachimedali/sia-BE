"""Deterministic `MetricsProvider` for tests and for a checkout with no keys.

**This is a test fixture and the system enforces that it stays one** (P0-37,
Part 7 rule 17). Every row it produces is stamped `source=FAKE`, and the
`analysable` manager on `PostMetric` filters those out at the queryset — so a
digest, finding, rule proposal or benchmark cannot be built from fabricated
numbers even by a caller who forgot to think about it. `USE_FAKE_PLATFORM_ADAPTER`
additionally cannot be true under production settings.

The curve matches the publish adapter's old fake for a reason: the analytics
suite's evergreen-vs-spike assertions were written against it, and a fake that
returned a constant would let the decay classification pass without working.
"""

from __future__ import annotations

import datetime as dt

from django.utils import timezone

from analytics.providers.base import (
    METRIC_KEYS,
    CommentSnapshot,
    MetricsUnsupportedError,
    RawMetricPayload,
)
from analytics.providers.zernio import NO_COMMENT_PLATFORMS

PROVIDER_KEY = "fake"
SCHEMA_VERSION = 1


class FakeMetricsProvider:
    key = PROVIDER_KEY
    schema_version = SCHEMA_VERSION

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: list[str] = []
        self.comment_calls: list[str] = []
        self.replies: list[tuple[str, str]] = []
        #: Test hooks. `payloads_for` dictates a curve for one post;
        #: `unsupported` marks a platform as reporting nothing at all, which is
        #: how the null-not-zero paths are exercised without a live vendor.
        self.payloads_for: dict[str, list[RawMetricPayload]] = {}
        self.comments_for: dict[str, list[CommentSnapshot]] = {}
        self.reactions_for: dict[str, dict[str, int] | None] = {}
        self.unsupported: set[str] = set()
        self.pending: set[str] = set()
        self.delta_cursor = ""
        self.delta_batches: list[list[RawMetricPayload]] = []

    # --- capability ------------------------------------------------------
    def supports(self, platform: str, metric: str) -> bool:
        if platform in self.unsupported:
            return False
        return metric in METRIC_KEYS

    def supports_comments(self, platform: str) -> bool:
        return platform not in NO_COMMENT_PLATFORMS and platform not in self.unsupported

    # --- acquisition -----------------------------------------------------
    def fetch(self, *, platform: str, provider_post_id: str) -> RawMetricPayload:
        queued = self.payloads_for.get(provider_post_id)
        if queued:
            return queued.pop(0)
        if not self.supports(platform, "impressions"):
            return self._bare(platform, provider_post_id, "UNAVAILABLE")
        if provider_post_id in self.pending:
            return self._bare(platform, provider_post_id, "PENDING")

        seen = self.calls.count(provider_post_id)
        self.calls.append(provider_post_id)
        # Diminishing returns: 100, 150, 175, 187…
        total = int(200 * (1 - 0.5 ** (seen + 1)))
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            body={
                "impressions": total * 20,
                "likes": total,
                "comments": total // 10,
                "shares": total // 20,
                "clicks": total // 5,
                "saves": total // 8,
                "fake": True,
                "capture": seen + 1,
            },
            availability="MEASURED",
            fetched_at=timezone.now(),
        )

    def changed_since(self, cursor: str) -> tuple[list[RawMetricPayload], str]:
        if not self.delta_batches:
            return [], cursor
        batch = self.delta_batches.pop(0)
        self.delta_cursor = f"c{len(self.calls)}-{len(batch)}"
        return batch, self.delta_cursor

    # --- audience --------------------------------------------------------
    def fetch_comments(
        self, *, platform: str, provider_post_id: str, since: dt.datetime | None = None
    ) -> list[CommentSnapshot]:
        if not self.supports_comments(platform):
            raise MetricsUnsupportedError(
                "This platform exposes no comment endpoint.", detail={"platform": platform}
            )
        self.comment_calls.append(provider_post_id)
        queued = self.comments_for.get(provider_post_id, [])
        return [c for c in queued if since is None or (c.posted_at and c.posted_at > since)]

    def fetch_reactions(self, *, platform: str, provider_post_id: str) -> dict[str, int] | None:
        return self.reactions_for.get(provider_post_id)

    def reply(self, *, platform: str, comment_external_id: str, body: str) -> str:
        self.replies.append((comment_external_id, body))
        return f"fake-reply-{len(self.replies)}"

    def fetch_account_stats(self, *, provider_account_id: str) -> dict[str, int | None]:
        seen = self.calls.count(provider_account_id)
        self.calls.append(provider_account_id)
        return {"followers": 1000 + seen * 25, "following": 180, "total_posts": 42 + seen}

    # --- plumbing --------------------------------------------------------
    def _bare(self, platform: str, provider_post_id: str, availability: str) -> RawMetricPayload:
        """A payload carrying no numbers at all. Not a payload of zeros — the
        distinction this whole port exists to preserve."""
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            body={},
            availability=availability,
            fetched_at=timezone.now(),
        )


_fake_metrics_provider = FakeMetricsProvider()


def fake_provider() -> FakeMetricsProvider:
    """The module-level instance, so a test and the code under test share the
    same call log without threading an object through every call site."""
    return _fake_metrics_provider
