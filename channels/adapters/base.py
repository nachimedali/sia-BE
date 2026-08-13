"""The `PlatformAdapter` port (design.md §9, D2/D3).

Every external dependency sits behind a port with a real adapter and a
deterministic fake (A8). This one is the whole of D2: OCCS never talks to
Meta, TikTok or YouTube, so swapping Zernio for bundle.social is a change of
one settings value and one module in this package.

**Eight methods, not design.md §9's five.** The table there names `connect_url`,
`publish`, `fetch_metrics`, `fetch_comments`, `disconnect`. The connect flow
needs three methods rather than one, because V1 (design.md §14) resolved to a
*headless* flow: `connect_url` starts OAuth, `resolve_callback` handles the
return, and `select_target` finishes the platforms that make the user pick a
page or organisation. Rendering that picker is OCCS's job, not the provider's —
that is what "the user never sees the provider" costs, and it is a method on the
port rather than provider-specific glue in a view.

`fetch_metrics`/`fetch_comments` were deferred out of Phase 9 (A92) on the rule
that a port method with no implementation behind it is a promise, not a
contract. Phase 11 is the implementation, so they land here now.

**Failure taxonomy.** `PlatformError.retryable` and `needs_reauth` are the only
things the publish task branches on: a 429 or a 5xx will succeed later and must
back off (implementation.md Phase 9), while a rejected caption never will and
marking it `FAILED` immediately is the honest answer; a lapsed platform grant
will not recover until the user reauthorises, whatever we retry. Adapters
classify all three; the task does not re-derive any of them from status codes or
error strings it would have to know provider internals to interpret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from common.exceptions import ProviderError

#: Platforms where authorising the user is not yet the same as choosing what
#: to post as: Facebook publishes as a Page, LinkedIn as an organisation. A
#: platform fact rather than a provider one — both adapters read this table, and
#: the contract test asserts they agree about which flow a platform takes.
SELECTION_PLATFORMS = frozenset({"facebook", "linkedin"})


class PlatformError(ProviderError):
    default_code = "platform_error"
    default_detail = "The publishing provider could not complete this request."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        needs_reauth: bool = False,
    ) -> None:
        super().__init__(message, detail=detail, code=code)
        self.retryable = retryable
        self.retry_after = retry_after
        #: The platform grant behind this account is gone. Nothing will publish
        #: through it until the user reauthorises, so the connections screen has
        #: to say so instead of the task retrying into a wall.
        self.needs_reauth = needs_reauth


@dataclass(frozen=True)
class ConnectTarget:
    """One page / organisation / channel the user may attach, in the platforms
    where authorising the account is not yet the same as choosing what to post
    as (Facebook Pages, LinkedIn organisations).

    `id` and `name` are all the picker UI renders; `payload` is whatever the
    provider needs handed back verbatim to attach this particular one.
    """

    id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectedAccount:
    provider_account_id: str
    handle: str = ""
    display_name: str = ""
    followers: int = 0


@dataclass(frozen=True)
class ConnectResolution:
    """What came back from the OAuth callback: either a finished account, or
    the choices the user still has to pick from.

    `params` is the provider state `select_target` needs echoed back to it,
    and the client is what echoes it. That is not a shortcut: the provider's
    one-time, ten-minute pending-data token cannot be read twice, so the state
    it unlocks has to be held *somewhere* between the two calls, and the
    browser is already holding it — the headless redirect landed on our own
    page carrying exactly these values in its query string. A server-side
    cache would add a TTL, a key, and an eviction story to keep a copy of
    something the client cannot avoid seeing anyway.
    """

    account: ConnectedAccount | None = None
    targets: list[ConnectTarget] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_selection(self) -> bool:
        return self.account is None


@dataclass(frozen=True)
class MetricSnapshot:
    """One reading of what a published post has earned so far.

    Cumulative, not incremental: every platform reports totals-to-date, and the
    decay slope §8.9 needs is computed from the differences between captures
    rather than trusted from the provider.

    `impressions` is `0` where the platform does not report it — LinkedIn
    personal accounts and several others simply do not — which is why
    `engagement_rate` falls back to a follower denominator (§8.9) rather than
    dividing by zero and calling it engagement.
    """

    impressions: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    clicks: int = 0
    saves: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommentSnapshot:
    external_id: str
    body: str = ""
    author: str = ""
    posted_at: Any = None


@dataclass(frozen=True)
class AccountStats:
    followers: int = 0
    following: int = 0
    total_posts: int = 0


@dataclass(frozen=True)
class PublishResult:
    provider_post_id: str
    platform_post_id: str = ""
    #: True when the provider recognised this as a replay of a call it had
    #: already carried out, rather than publishing anything new. The task
    #: treats it exactly like a fresh success — that is what makes I9 hold
    #: across a retry that outlived the provider's own dedup window.
    was_replay: bool = False


class PlatformAdapter(Protocol):
    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        """The provider-side tenant for one workspace, created on first use.
        Idempotent: given a live `existing_profile_id` it returns it unchanged.
        """
        ...

    def connect_url(self, *, platform: str, profile_id: str, redirect_url: str) -> str: ...

    def resolve_callback(
        self, *, platform: str, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution: ...

    def select_target(
        self, *, platform: str, profile_id: str, params: dict[str, Any], target_id: str
    ) -> ConnectedAccount:
        """`params` is `ConnectResolution.params` handed straight back; it
        carries the candidate targets, so `target_id` is resolved against
        them rather than re-fetched from a token that has already been spent.
        """
        ...

    def publish(
        self,
        *,
        platform: str,
        provider_account_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> PublishResult: ...

    def fetch_metrics(self, *, platform: str, provider_post_id: str) -> MetricSnapshot:
        """Totals-to-date for one published post. A platform that reports
        nothing for a field leaves it at zero rather than guessing."""
        ...

    def fetch_comments(
        self, *, platform: str, provider_post_id: str, since: Any = None
    ) -> list[CommentSnapshot]:
        """Comments on one published post, newest first. `since` narrows the
        request where the platform supports it and is applied client-side where
        it does not — ingestion is idempotent on `external_id` either way."""
        ...

    def fetch_account_stats(self, *, provider_account_id: str) -> AccountStats:
        """Follower counts for one connected account, for `AccountSnapshot`."""
        ...

    def disconnect(self, *, provider_account_id: str) -> None: ...


def echo_targets(params: dict[str, Any], targets: list[ConnectTarget]) -> dict[str, Any]:
    """The `ConnectResolution.params` an adapter hands back: whatever provider
    state it was given, plus the candidate targets, so `select_target` resolves
    the user's pick against what was actually offered rather than re-reading a
    token that has already been spent."""
    return {**params, "targets": [{"id": t.id, "name": t.name, **t.payload} for t in targets]}


def find_offered_target(params: dict[str, Any], target_id: str) -> dict[str, Any]:
    """The other half of `echo_targets`. Shared so the fake cannot be laxer
    than the real adapter about what counts as a valid pick — a caller that
    stopped echoing `params` would otherwise pass every fake-backed test and
    fail against the provider."""
    for target in params.get("targets", []):
        if str(target.get("id")) == target_id:
            found: dict[str, Any] = target
            return found
    raise PlatformError(
        "That target is not one of the choices offered.", detail={"target": target_id}
    )
