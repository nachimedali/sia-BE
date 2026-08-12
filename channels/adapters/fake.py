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
"""

from __future__ import annotations

from typing import Any

from channels.adapters.base import (
    SELECTION_PLATFORMS,
    ConnectedAccount,
    ConnectResolution,
    ConnectTarget,
    PlatformError,
    PublishResult,
    echo_targets,
    find_offered_target,
)


class FakePlatformAdapter:
    def __init__(self) -> None:
        self.profiles: list[str] = []
        self.connect_urls: list[dict[str, str]] = []
        self.published: list[dict[str, Any]] = []
        self.disconnected: list[str] = []
        #: Outcomes the next publish calls will hit, oldest first. Each is
        #: either an exception to raise or None for "behave normally".
        self.fail_next: list[Exception | None] = []
        self._by_key: dict[str, PublishResult] = {}

    # --- connect ---------------------------------------------------------
    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        if existing_profile_id:
            return existing_profile_id
        self.profiles.append(name)
        return f"fake-profile-{len(self.profiles)}"

    def connect_url(self, *, platform: str, profile_id: str, redirect_url: str) -> str:
        self.connect_urls.append(
            {"platform": platform, "profile_id": profile_id, "redirect_url": redirect_url}
        )
        return f"https://fake-platform.test/oauth/{platform}?redirect={redirect_url}"

    def resolve_callback(
        self, *, platform: str, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
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
    ) -> PublishResult:
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

    def disconnect(self, *, provider_account_id: str) -> None:
        self.disconnected.append(provider_account_id)

    # --- test helpers ----------------------------------------------------
    def clear(self) -> None:
        self.profiles.clear()
        self.connect_urls.clear()
        self.published.clear()
        self.disconnected.clear()
        self.fail_next.clear()
        self._by_key.clear()

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


# Module-level so a test can inspect calls after a service or task has run
# (mirrors `ai.providers.fake._fake_text_provider`).
_fake_adapter = FakePlatformAdapter()
