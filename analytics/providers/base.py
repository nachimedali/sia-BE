"""`MetricsProvider` — the port measurement is acquired through (P0-27, P0-28).

**Why this is not a method on `PlatformAdapter`.** Publishing and measuring are
two vendor relationships wearing one name. Zernio happens to do both today, but
the failure modes have nothing in common: a publish that fails is a post the
customer can see did not go out, while a capture that fails is a number nobody
was waiting for. Keeping them in one port means the day metrics move to another
vendor, every publish call site is in the blast radius. So: two ports, neither
carrying the other's methods, asserted by test.

**Three states, not two.** A provider can answer "here it is" (`MEASURED`),
"this platform will never tell you" (`UNAVAILABLE`), or "ask again shortly"
(`PENDING`, Zernio's `202`). Collapsing the last two loses the distinction
between a permanent gap and a sync in flight — the first is recorded, the
second is rescheduled, and writing either as a zero is the C-07 defect.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from common.exceptions import ProviderError

#: The metric vocabulary this system normalises to. Provider-specific names are
#: mapped onto these inside each provider, so `MetricCapability` rows and every
#: downstream aggregate speak one language.
METRIC_KEYS: tuple[str, ...] = (
    "impressions",
    "likes",
    "comments",
    "shares",
    "clicks",
    "saves",
    #: Unique accounts that saw the post, and times it was shown or played —
    #: two different questions, kept apart (SocialAPI reports both on
    #: Instagram; most platforms report one or neither, and the missing one is
    #: null, never borrowed from the other).
    "reach",
    "views",
)


class MetricsError(ProviderError):
    default_code = "metrics_error"
    default_detail = "The measurement provider could not complete this request."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, detail=detail, code=code)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class RawMetricPayload:
    """One provider answer, before normalisation.

    Deliberately unparsed. `body` is exactly what the vendor sent, and it is
    what gets stored compressed (C-07b) so a normalisation bug is fixed by
    reprocessing rather than by re-polling a post whose retention window has
    already closed.

    `availability` is the provider's own verdict about whether an answer
    exists at all. Normalisation may narrow it per metric — a payload that is
    `MEASURED` overall can still have no `saves` on a platform that does not
    report saves — but it may never widen it.
    """

    provider_key: str
    platform: str
    provider_post_id: str
    schema_version: int
    body: dict[str, Any] = field(default_factory=dict)
    availability: str = "MEASURED"
    fetched_at: dt.datetime | None = None
    #: Set on a `PENDING` answer where the provider said how long to wait.
    retry_after: float | None = None
    #: What this answer proved about the account's insights permission:
    #: `False` when the platform withheld every insight figure it should have
    #: reported (the account needs reconnecting, SA-53), `True` when one came
    #: back, `None` when it proves nothing either way.
    insights_permission: bool | None = None

    @property
    def is_measured(self) -> bool:
        return self.availability == "MEASURED"

    @property
    def is_pending(self) -> bool:
        return self.availability == "PENDING"


@dataclass(frozen=True)
class CommentSnapshot:
    """One audience comment as the provider reported it.

    Lives here rather than on the publish port: a comment is measurement, and
    P0-28 says the publish port carries no measurement types. `reactions` is
    `None` where the provider does not break them down — distinct from `{}`,
    which is "it broke them down and there were none".
    """

    external_id: str
    body: str = ""
    author: str = ""
    posted_at: dt.datetime | None = None
    reactions: dict[str, int] | None = None
    #: What the platform allows on this comment, where the provider says.
    capabilities: dict[str, bool] | None = None


@runtime_checkable
class MetricsProvider(Protocol):
    """What a measurement vendor must do. No publishing.

    A vendor with a change feed adds `changed_since(cursor) -> (payloads,
    next_cursor)` — every snapshot that moved since `cursor`, a rolling log
    rather than an archive (Zernio's is seven days), so a consumer that falls
    behind re-bootstraps rather than rewinds. It is optional because not every
    vendor has one (SocialAPI does not, plan G2); `ingest.follow_delta` uses it
    where it exists.
    """

    #: Stable, short, stored on every row this provider produced. Changing it
    #: orphans the provenance of every historical snapshot, so it does not
    #: change.
    key: str

    #: Bumped when the provider's response shape changes in a way that alters
    #: normalisation. Stored per row so a reprocess knows which mapping wrote it.
    schema_version: int

    def supports(self, platform: str, metric: str) -> bool:
        """Whether this provider can report `metric` on `platform` at all.

        Consulted before a call is made and again during normalisation. A
        `False` here is what puts a null — never a zero — in the column.
        """
        ...

    def read_metric(self, body: Any, metric: str) -> int | None:
        """One of `METRIC_KEYS` out of a payload body this provider produced,
        or `None` when it is not there. The vendor's vocabulary stops here, so
        normalisation never learns a provider's field names."""
        ...

    def fetch(self, *, platform: str, provider_post_id: str) -> RawMetricPayload:
        """Totals-to-date for one published post.

        The bootstrap path and the single-post repair path. Routine capture
        follows `changed_since` instead: per-post polling is what P0-31 exists
        to remove.
        """
        ...


@runtime_checkable
class AudienceProvider(Protocol):
    """Comments and reaction breakdowns (L-3, L-4a).

    Separate from `MetricsProvider` because coverage genuinely differs: Zernio
    measures TikTok but cannot read a TikTok comment at any price, so a
    provider may implement one and not the other.
    """

    key: str

    def supports_comments(self, platform: str) -> bool: ...

    def fetch_comments(
        self,
        *,
        platform: str,
        provider_post_id: str,
        since: dt.datetime | None = None,
        platform_post_id: str = "",
        provider_account_id: str = "",
    ) -> list[Any]:
        """Comments newer than `since`. Providers that address a post by the
        platform's id and its account (SocialAPI) read the last two."""
        ...

    def fetch_reactions(self, *, platform: str, provider_post_id: str) -> dict[str, int] | None:
        """Per-type reaction counts, or `None` where the platform does not
        break them down. `None` is not `{}` — one means "no answer", the other
        means "answered, and it was empty"."""
        ...

    def reply(
        self,
        *,
        platform: str,
        comment_external_id: str,
        body: str,
        platform_post_id: str = "",
        provider_account_id: str = "",
    ) -> str:
        """Post a reply, returning its external id. Advanced only, metered
        against a pooled org allowance (P0-36)."""
        ...


def proportions(raw: Any) -> dict[str, float] | None:
    """A `{bucket: share}` map summing to 1, from either counts or shares.

    Vendors return absolute follower counts on some dimensions and percentages
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


class MetricsUnsupportedError(MetricsError):
    """The provider structurally cannot answer, so retrying is pointless and
    an empty result would be a lie.

    Distinct from `MetricsError`, which means the call failed and may succeed
    later. The caller records `UNAVAILABLE` on this one and reschedules on the
    other — conflating them is how a permanent gap becomes an infinite retry,
    or how "TikTok has no comment endpoint" becomes "this post has no
    comments".
    """

    default_code = "metrics_unsupported"
    default_detail = "This provider cannot report that on this platform."


class MetricsProviderRegistry:
    """Resolves `(platform, metric)` to whoever can answer it.

    A registry rather than a single configured provider because coverage is
    per-pair, and the honest answer for an uncovered pair is `None` — which
    normalisation turns into a null. A platform nobody covers reports
    unavailable; it never reports zero (A-19).
    """

    def __init__(
        self,
        providers: list[MetricsProvider] | None = None,
        *,
        audience: AudienceProvider | None = None,
        audience_ready: bool = True,
    ) -> None:
        self._providers: list[MetricsProvider] = list(providers or [])
        #: The audience port, when it is served by its own provider. Absent,
        #: comments are looked for among the metrics providers (Zernio and
        #: the fake implement both).
        self._audience = audience
        #: `False` while the selected provider's audience port is not built:
        #: comment capture then waits rather than recording "this platform
        #: has no comments", which would be a statement about the platform.
        self.audience_ready = audience_ready

    def register(self, provider: MetricsProvider) -> None:
        self._providers = [p for p in self._providers if p.key != provider.key]
        self._providers.append(provider)

    def all(self) -> list[MetricsProvider]:
        return list(self._providers)

    def resolve(self, platform: str, metric: str) -> MetricsProvider | None:
        for provider in self._providers:
            if provider.supports(platform, metric):
                return provider
        return None

    def for_comments(self, platform: str) -> AudienceProvider | None:
        """Whoever can read this platform's comments.

        A separate lookup from `for_platform` because coverage genuinely
        differs: Zernio measures TikTok but cannot read a TikTok comment at any
        price (L-3), and a single resolver would have to pretend otherwise.
        """
        if self._audience is not None:
            return self._audience if self._audience.supports_comments(platform) else None
        for provider in self._providers:
            audience = provider if isinstance(provider, AudienceProvider) else None
            if audience is not None and audience.supports_comments(platform):
                return audience
        return None

    def for_platform(self, platform: str) -> MetricsProvider | None:
        """Whoever can answer *anything* on this platform — the provider a
        capture call is routed to before per-metric narrowing happens."""
        for provider in self._providers:
            if any(provider.supports(platform, metric) for metric in METRIC_KEYS):
                return provider
        return None
