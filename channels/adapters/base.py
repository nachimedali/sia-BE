"""The `PlatformAdapter` port (design.md §9, D2/D3).

Every external dependency sits behind a port with a real adapter and a
deterministic fake (A8). This one is the whole of D2: ComposeVision never talks to
Meta, TikTok or YouTube, so swapping Zernio for bundle.social is a change of
one settings value and one module in this package.

**This port publishes and nothing else** (P0-28). `fetch_metrics`,
`fetch_comments` and `fetch_account_stats` used to live here and now live on
`analytics.providers.MetricsProvider`. Publishing sends data out; measurement
pulls it in, and the day measurement moves to another vendor, no publish call
site should be in the blast radius. `analytics/tests/test_port_separation.py`
asserts the split rather than trusting a comment to preserve it.

**Six methods, not design.md §9's five.** The connect flow needs three rather
than one, because V1 (design.md §14) resolved to a *headless* flow:
`connect_url` starts OAuth, `resolve_callback` handles the return, and
`select_target` finishes the platforms that make the user pick a page or
organisation. Rendering that picker is ComposeVision's job, not the provider's — that is
what "the user never sees the provider" costs, and it is a method on the port
rather than provider-specific glue in a view.

**Failure taxonomy.** `PlatformError.retryable` and `needs_reauth` are the only
things the publish task branches on: a 429 or a 5xx will succeed later and must
back off (implementation.md Phase 9), while a rejected caption never will and
marking it `FAILED` immediately is the honest answer; a lapsed platform grant
will not recover until the user reauthorises, whatever we retry. Adapters
classify all three; the task does not re-derive any of them from status codes or
error strings it would have to know provider internals to interpret.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Protocol
from urllib.parse import urljoin

from django.conf import settings

from common.exceptions import ProviderError

#: Platforms where authorising the user is not yet the same as choosing what
#: to post as: Facebook publishes as a Page, LinkedIn as an organisation. A
#: platform fact rather than a provider one — both adapters read this table, and
#: the contract test asserts they agree about which flow a platform takes.
SELECTION_PLATFORMS = frozenset({"facebook", "linkedin"})

#: Provider-backed option lists, by the `source` a `rules.py` `remote_choice`
#: option names (P4-02). Declared here rather than accepted as a free string
#: from the caller: an endpoint that forwarded any source to the vendor would
#: be a proxy for every call the vendor exposes.
#:
#: `platform` is part of the row because a source belongs to one platform — a
#: Pinterest board list requested for an Instagram account is a bug worth a
#: 400, not an empty list that looks like "no boards yet".
REMOTE_OPTION_SOURCES: dict[str, str] = {
    "pinterest_boards": "pinterest",
}


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
class PublishResult:
    provider_post_id: str
    platform_post_id: str = ""
    #: True when the provider recognised this as a replay of a call it had
    #: already carried out, rather than publishing anything new. The task
    #: treats it exactly like a fresh success — that is what makes I9 hold
    #: across a retry that outlived the provider's own dedup window.
    was_replay: bool = False
    #: The provider accepted the publish and is still delivering it (SocialAPI
    #: publishes in the background). The target stays `PUBLISHING` until the
    #: reconciler — or, later, a webhook — reads the outcome. Never retried:
    #: some platforms confirm an upload minutes before it shows, and a second
    #: request would post twice.
    pending: bool = False
    #: Set when the provider reports delivery failed for good (read back by
    #: the reconciler); carries the platform's reason.
    failed: bool = False
    error: dict[str, Any] = field(default_factory=dict)
    permalink: str = ""
    published_at: dt.datetime | None = None

    @classmethod
    def unconfirmed(cls, provider_post_id: str, message: str) -> PublishResult:
        """A publish nobody can confirm any more: terminal, for a person to
        check on the platform — never sent again."""
        return cls(
            provider_post_id=provider_post_id,
            failed=True,
            error={"code": "publish_unconfirmed", "message": message},
        )


def absolute_media_url(url: str) -> str:
    """A media URL a provider can fetch itself. Local/dev media is served
    relatively; object storage already yields an absolute URL and passes
    through untouched."""
    if url.startswith(("http://", "https://")):
        return url
    return str(urljoin(settings.PUBLIC_MEDIA_BASE_URL, url))


@dataclass(frozen=True)
class PublishMedia:
    """One media item of a target, as the publish task hands it to an adapter
    (SA-31). Built from the workspace's own `MediaAsset` rows at publish time,
    so the URL is fresh and long-lived enough for a provider to fetch, and the
    bytes can be uploaded when no public URL exists (dev, private storage)."""

    id: int
    url: str
    filename: str
    mime: str
    size: int
    open: Callable[[], IO[bytes]]


class PublishJournal(Protocol):
    """Where an adapter records each step **before** taking the next one, so a
    crash or retry resumes instead of repeating (SA-32). Implemented by the
    publish task over `PostTarget`; each `record` is committed at once."""

    @property
    def provider_post_id(self) -> str: ...

    @property
    def provider_state(self) -> str: ...

    @property
    def provider_media_ids(self) -> list[str]: ...

    def record(
        self,
        *,
        provider_post_id: str | None = None,
        provider_state: str | None = None,
        media_ids: list[str] | None = None,
    ) -> None: ...


class PlatformAdapter(Protocol):
    #: Whether every return carries the `state` we signed. When true, a return
    #: without one is refused (`channels.services.complete_connect`).
    requires_state: bool

    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        """The provider-side tenant for one workspace, created on first use.
        Idempotent: given a live `existing_profile_id` it returns it unchanged.
        """
        ...

    def connect_url(
        self, *, platform: str, profile_id: str, redirect_url: str, state: str = ""
    ) -> str:
        """`state` is our signed correlation token (`channels.services`). A
        provider that echoes it back gets it verified on return; one that does
        not may ignore it."""
        ...

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
        journal: PublishJournal | None = None,
        media: Sequence[PublishMedia] = (),
    ) -> PublishResult:
        """`payload` is `render_post`'s output, sent as-is (rule 2). `journal`
        is consulted first and written at every step by providers without an
        idempotency key; `media` carries fresh URLs and bytes for providers
        that cannot fetch `payload`'s own URLs."""
        ...

    def fetch_publish_status(self, *, provider_post_id: str) -> PublishResult:
        """The provider's current answer for a publish it accepted as pending.
        `pending=True` while it is still delivering."""
        ...

    def discard_draft(self, *, provider_post_id: str) -> None:
        """Delete a draft that was never published (a failed or cancelled
        target, or a crash between create and journal). Idempotent."""
        ...

    def release_media(self, *, media_ids: Sequence[str]) -> list[str]:
        """Free provider-side media once a publish is terminal. Returns the ids
        still in use, to be retried later."""
        ...

    def disconnect(self, *, provider_account_id: str) -> None: ...

    def list_remote_options(
        self, *, platform: str, provider_account_id: str, source: str
    ) -> list[dict[str, Any]]:
        """Reference data only the provider knows — a Pinterest board list.

        Returns `[{"id", "name"}]`, empty for a source this adapter does not
        serve. Empty rather than raising: an unrecognised source is a composer
        bug, and a form that renders an empty select is recoverable where a
        500 is not.

        **Read-only, and outside the publish path.** It answers the composer
        while someone is typing, so it must never be on the path that sends a
        post.
        """
        ...


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
