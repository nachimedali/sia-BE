"""The fake and the real SocialAPI adapter agree about publishing (SA-33).

Every scheduling test runs against the fake. If the fake were laxer than the
vendor about drafts, replays or pending publishes, the whole suite could be
green while production posted twice. So the scenarios exactly-once depends on
run against both, and must come out the same.
"""

from __future__ import annotations

from typing import Any

import pytest

from channels.adapters.base import PlatformError
from channels.adapters.fake import FakePlatformAdapter
from content.models import ProviderState
from social_providers.tests.stub import MemoryJournal, StubVendor

pytestmark = pytest.mark.django_db


class Side:
    """One adapter plus the means to read and steer its vendor."""

    def __init__(self, name: str) -> None:
        self.name = name
        if name == "fake":
            fake = FakePlatformAdapter()
            fake.async_publish = True
            self.adapter: Any = fake
            self._fake = fake
        else:
            self._vendor = StubVendor()
            self.adapter = self._vendor.adapter()

    def drafts(self) -> int:
        return self._fake.drafts_created if self.name == "fake" else self._vendor.creates

    def publishes(self) -> int:
        return len(self._fake.publish_requests) if self.name == "fake" else self._vendor.publishes

    def settle(self, pid: str, *, published: bool = True) -> None:
        if self.name == "fake":
            self._fake.settle(pid, published=published)
        else:
            self._vendor.settle(pid, published=published)

    def lose_the_publish_call(self) -> None:
        if self.name == "fake":
            self._fake.crash_after_draft = True
        else:
            self._vendor.unreachable_on = "POST /posts/post-"


@pytest.fixture(params=["fake", "socialapi"])
def side(request: pytest.FixtureRequest) -> Side:
    return Side(str(request.param))


def _publish(side: Side, journal: MemoryJournal) -> Any:
    return side.adapter.publish(
        platform="instagram",
        provider_account_id="acc_1",
        payload={"platform": "instagram", "body": "Hello", "media": [], "options": {}},
        idempotency_key="k",
        journal=journal,
    )


def test_a_publish_is_drafted_then_requested_and_answers_pending(side: Side) -> None:
    journal = MemoryJournal()

    result = _publish(side, journal)

    assert result.pending is True
    assert journal.provider_state == ProviderState.PUBLISH_REQUESTED
    assert journal.provider_post_id == result.provider_post_id
    assert (side.drafts(), side.publishes()) == (1, 1)


def test_a_replay_after_the_request_never_publishes_again(side: Side) -> None:
    journal = MemoryJournal()
    _publish(side, journal)

    _publish(side, journal)

    assert (side.drafts(), side.publishes()) == (1, 1)


def test_a_lost_publish_call_resumes_the_same_draft(side: Side) -> None:
    journal = MemoryJournal()
    side.lose_the_publish_call()

    with pytest.raises(PlatformError) as caught:
        _publish(side, journal)
    assert caught.value.retryable is True
    assert journal.provider_state == ProviderState.DRAFT_CREATED

    _publish(side, journal)

    assert (side.drafts(), side.publishes()) == (1, 1)


def test_the_outcome_is_read_back(side: Side) -> None:
    journal = MemoryJournal()
    _publish(side, journal)
    pid = journal.provider_post_id

    assert side.adapter.fetch_publish_status(provider_post_id=pid).pending is True
    side.settle(pid)
    done = side.adapter.fetch_publish_status(provider_post_id=pid)

    assert done.pending is False and done.failed is False
    assert done.platform_post_id


def test_a_platform_refusal_reads_back_as_failed(side: Side) -> None:
    journal = MemoryJournal()
    _publish(side, journal)
    side.settle(journal.provider_post_id, published=False)

    status = side.adapter.fetch_publish_status(provider_post_id=journal.provider_post_id)

    assert status.failed is True
    assert status.error.get("message")


def test_a_live_post_is_never_discarded(side: Side) -> None:
    journal = MemoryJournal()
    _publish(side, journal)

    with pytest.raises(PlatformError):
        side.adapter.discard_draft(provider_post_id=journal.provider_post_id)
