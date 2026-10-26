"""Deterministic `PlatformAdapter` for tests and dev (A8).

Two things it models faithfully, because the publish task's correctness
depends on them and a fake that glossed over either would make a green suite
meaningless:

* **Idempotency.** A repeated `idempotency_key` returns the *original*
  `provider_post_id` with `was_replay=True` and publishes nothing new — which
  is what `test_retried_publish_task_does_not_double_post` (I9) asserts by
  counting the entries in `published`.
* **Retryable vs terminal failure.** `fail_next` queues outcomes a test wants
  the next publish calls to hit, so back-off and the third-failure alert are
  exercised against the same code path a real 429 would take.
* **SocialAPI's shape, when asked** (SA-33). With `async_publish = True` it
  behaves like SocialAPI: a draft is journalled before it is published, the
  publish answers *pending*, the outcome is read later through
  `fetch_publish_status`, and a retry resumes from the journal instead of
  posting again. `crash_after_draft` stops an attempt between the draft and
  the publish — the window exactly-once has to survive.
  `social_providers/tests/test_publish_parity.py` runs the same scenarios
  against this and the real adapter, so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from channels.adapters.base import (
    REMOTE_OPTION_SOURCES,
    SELECTION_PLATFORMS,
    ConnectedAccount,
    ConnectResolution,
    ConnectTarget,
    PlatformError,
    PublishJournal,
    PublishMedia,
    PublishResult,
    echo_targets,
    find_offered_target,
)


class FakePlatformAdapter:
    #: Verified when echoed, never required — the fake's returns may omit it.
    requires_state = False

    def __init__(self) -> None:
        self.profiles: list[str] = []
        self.connect_urls: list[dict[str, str]] = []
        self.published: list[dict[str, Any]] = []
        self.disconnected: list[str] = []
        #: Outcomes the next publish calls will hit, oldest first. Each is
        #: either an exception to raise or None for "behave normally".
        self.fail_next: list[Exception | None] = []
        self._by_key: dict[str, PublishResult] = {}
        #: SocialAPI mode (SA-33): drafts, a journal, pending publishes.
        self.async_publish = False
        self.crash_after_draft = False
        #: provider post id → "draft" | "publishing" | "published" | "failed".
        self.posts: dict[str, str] = {}
        self.drafts_created = 0
        self.publish_requests: list[str] = []
        self.discarded: list[str] = []
        self.released_media: list[str] = []
        self.media_in_use: set[str] = set()
        #: Accounts the "provider" no longer holds, and every presence read.
        self.missing: set[str] = set()
        self.presence_checks: list[str] = []

    # --- connect ---------------------------------------------------------
    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        if existing_profile_id:
            return existing_profile_id
        self.profiles.append(name)
        return f"fake-profile-{len(self.profiles)}"

    def connect_url(
        self, *, platform: str, profile_id: str, redirect_url: str, state: str = ""
    ) -> str:
        self.connect_urls.append(
            {
                "platform": platform,
                "profile_id": profile_id,
                "redirect_url": redirect_url,
                "state": state,
            }
        )
        return f"https://fake-platform.test/oauth/{platform}?redirect={redirect_url}&state={state}"

    def resolve_callback(
        self, *, platform: str, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
        # A refused sign-in comes back the way real providers send it (OAuth's
        # `error`, SocialAPI's `status=error`), so the refusal path is testable.
        if params.get("error") or params.get("status") == "error":
            raise PlatformError(
                "The sign-in was cancelled, so nothing was connected.",
                code="connect_denied",
                detail={"error": str(params.get("error") or "")},
            )
        if platform in SELECTION_PLATFORMS:
            targets = [
                ConnectTarget(id=f"{platform}-target-{i}", name=f"{platform.title()} {i}")
                for i in (1, 2)
            ]
            return ConnectResolution(targets=targets, params=echo_targets(params, targets))
        return ConnectResolution(account=self._account(platform, str(params.get("code", "1"))))

    def select_target(
        self, *, platform: str, profile_id: str, params: dict[str, Any], target_id: str
    ) -> ConnectedAccount:
        find_offered_target(params, target_id)
        return self._account(platform, target_id)

    @staticmethod
    def _account(platform: str, suffix: str) -> ConnectedAccount:
        return ConnectedAccount(
            provider_account_id=f"acct-{platform}-{suffix}",
            handle=f"@{platform}-{suffix}",
            display_name=f"{platform.title()} {suffix}",
            followers=1234,
        )

    # --- publish ---------------------------------------------------------
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
        if self.async_publish and journal is not None:
            return self._publish_async(platform, provider_account_id, payload, journal)

        replay = self._by_key.get(idempotency_key)
        if replay is not None:
            return PublishResult(
                provider_post_id=replay.provider_post_id,
                platform_post_id=replay.platform_post_id,
                was_replay=True,
            )

        if self.fail_next:
            outcome = self.fail_next.pop(0)
            if outcome is not None:
                raise outcome

        self.published.append(
            {
                "platform": platform,
                "provider_account_id": provider_account_id,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        result = PublishResult(
            provider_post_id=f"fake-post-{len(self.published)}",
            platform_post_id=f"{platform}-{len(self.published)}",
        )
        self._by_key[idempotency_key] = result
        return result

    def _publish_async(
        self,
        platform: str,
        provider_account_id: str,
        payload: dict[str, Any],
        journal: PublishJournal,
    ) -> PublishResult:
        from content.models import ProviderState

        pid = journal.provider_post_id
        state = self.posts.get(pid) if pid else None
        if state is not None and state != "draft":
            journal.record(provider_state=ProviderState.PUBLISH_REQUESTED)
            return self.fetch_publish_status(provider_post_id=pid)

        if self.fail_next:
            outcome = self.fail_next.pop(0)
            if outcome is not None:
                raise outcome

        if state is None:
            self.drafts_created += 1
            pid = f"fake-draft-{self.drafts_created}"
            self.posts[pid] = "draft"
            self.published.append(
                {
                    "platform": platform,
                    "provider_account_id": provider_account_id,
                    "payload": payload,
                    "provider_post_id": pid,
                }
            )
            journal.record(provider_post_id=pid, provider_state=ProviderState.DRAFT_CREATED)
            if self.crash_after_draft:
                self.crash_after_draft = False
                raise PlatformError("Worker lost between draft and publish.", retryable=True)

        self.posts[pid] = "publishing"
        self.publish_requests.append(pid)
        journal.record(provider_state=ProviderState.PUBLISH_REQUESTED)
        return PublishResult(provider_post_id=pid, pending=True)

    def settle(self, provider_post_id: str, *, published: bool = True) -> None:
        """What the platform eventually reports — the test's stand-in for time."""
        self.posts[provider_post_id] = "published" if published else "failed"

    def fetch_publish_status(self, *, provider_post_id: str) -> PublishResult:
        state = self.posts.get(provider_post_id)
        if state == "published":
            return PublishResult(
                provider_post_id=provider_post_id,
                platform_post_id=f"native-{provider_post_id}",
                permalink=f"https://fake-platform.test/p/{provider_post_id}",
            )
        if state in ("failed", None):
            return PublishResult(
                provider_post_id=provider_post_id,
                failed=True,
                error={"code": "platform.rejected", "message": "The platform refused the post."},
            )
        return PublishResult(provider_post_id=provider_post_id, pending=True)

    def discard_draft(self, *, provider_post_id: str) -> None:
        state = self.posts.get(provider_post_id)
        if state in ("publishing", "published"):
            raise PlatformError(
                f"Refusing to delete a provider post that is '{state}'.", code="discard_refused"
            )
        self.posts.pop(provider_post_id, None)
        self.discarded.append(provider_post_id)

    def release_media(self, *, media_ids: Sequence[str]) -> list[str]:
        kept = [media_id for media_id in media_ids if media_id in self.media_in_use]
        self.released_media.extend(m for m in media_ids if m not in self.media_in_use)
        return kept

    def list_remote_options(
        self, *, platform: str, provider_account_id: str, source: str
    ) -> list[dict[str, Any]]:
        """Deterministic boards, derived from the account id (P4-02).

        Derived rather than a fixed list so two accounts do not answer
        identically — a composer that showed the same boards for every
        connection would pass a test built on a constant and fail the moment
        anyone looked at it.
        """
        if source not in REMOTE_OPTION_SOURCES:
            return []
        return [
            {"id": f"{provider_account_id}-board-{index}", "name": name}
            for index, name in enumerate(("Ideas", "Products", "Seasonal"), start=1)
        ]

    # --- analytics (Phase 11) --------------------------------------------
    def disconnect(self, *, provider_account_id: str) -> None:
        self.disconnected.append(provider_account_id)

    def account_present(self, *, profile_id: str, provider_account_id: str) -> bool:
        self.presence_checks.append(provider_account_id)
        return provider_account_id not in self.missing

    # --- test helpers ----------------------------------------------------
    def clear(self) -> None:
        self.profiles.clear()
        self.connect_urls.clear()
        self.published.clear()
        self.disconnected.clear()
        self.fail_next.clear()
        self._by_key.clear()
        self.async_publish = False
        self.crash_after_draft = False
        self.posts.clear()
        self.drafts_created = 0
        self.publish_requests.clear()
        self.discarded.clear()
        self.released_media.clear()
        self.media_in_use.clear()
        self.missing.clear()
        self.presence_checks.clear()

    def queue_failure(
        self,
        times: int = 1,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
        needs_reauth: bool = False,
        message: str = "Upstream failure.",
    ) -> None:
        """Queues the failures a test wants the next publish calls to hit.

        The three keyword flags are the whole of the port's failure taxonomy
        (`channels.adapters.base`), so a test says what *kind* of failure it is
        simulating rather than which provider status code produced it: a 429 is
        `retry_after=…`, a rejected caption is `retryable=False`, a lapsed
        platform grant is `needs_reauth=True`.
        """
        for _ in range(times):
            self.fail_next.append(
                PlatformError(
                    message,
                    retryable=retryable,
                    retry_after=retry_after,
                    needs_reauth=needs_reauth,
                )
            )

    def queue_vendor_error(
        self, status: int, code: str, *, retry_after: float | None = None
    ) -> None:
        """Queues a failure exactly as the selected vendor's error would arrive
        (SA-33): classified by the real taxonomy, so the fake's two 429s behave
        like production's — `ratelimit.exceeded` waits and retries, a used-up
        monthly post allowance does not."""
        from social_providers.socialapi.errors import (
            SocialAPIError,
            classify,
            to_platform_error,
        )

        self.fail_next.append(
            to_platform_error(
                SocialAPIError(
                    f"The provider refused this ({code}).",
                    kind=classify(status, code),
                    status=status,
                    vendor_code=code,
                    retry_after=retry_after,
                )
            )
        )


# Module-level so a test can inspect calls after a service or task has run
# (mirrors `ai.providers.fake._fake_text_provider`).
_fake_adapter = FakePlatformAdapter()
