"""Publishing through SocialAPI, exactly once (SA-31, SA-32).

Driven against `stub.StubVendor`, which keeps state the way the vendor does —
drafts, one publish per draft, `409` on the second — so a retry, a crash or a
replay is tested as a sequence, not as a single canned answer.
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

from channels.adapters.base import PlatformError, PublishMedia
from content.models import ProviderState
from social_providers.socialapi.publish import media_mode
from social_providers.tests.stub import MemoryJournal, StubVendor, error

pytestmark = pytest.mark.django_db

ACCOUNT = "acc_01HZ9X3Q4R5M6N7P8V2K0W1J"


@pytest.fixture(autouse=True)
def _upload_mode(settings: Any) -> None:
    settings.SOCIALAPI_MEDIA_MODE = "upload"


@pytest.fixture
def vendor() -> StubVendor:
    return StubVendor()


def payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "instagram",
        "post_format": "FEED",
        "body": "Spring is here. #new",
        "thread": [],
        "hashtags": ["#new"],
        "media": [],
        "options": {"share_to_feed": True},
        "truncated": False,
        "warnings": [],
    }
    base.update(over)
    return base


def item(media_id: int, *, size: int = 2048, mime: str = "image/jpeg") -> PublishMedia:
    return PublishMedia(
        id=media_id,
        url=f"https://cdn.example.com/{media_id}.jpg",
        filename=f"{media_id}.jpg",
        mime=mime,
        size=size,
        open=lambda: io.BytesIO(b"\xff\xd8 image bytes"),
    )


def publish(vendor: StubVendor, journal: MemoryJournal, **kwargs: Any) -> Any:
    return vendor.adapter().publish(
        platform=kwargs.pop("platform", "instagram"),
        provider_account_id=ACCOUNT,
        payload=kwargs.pop("payload", payload()),
        idempotency_key="key-1",
        journal=journal,
        media=kwargs.pop("media", ()),
    )


# -----------------------------------------------------------------------------
# The happy path, and what is journalled before each step
# -----------------------------------------------------------------------------
class RecordingJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.states: list[str] = []

    def record(self, **kwargs: Any) -> None:
        super().record(**kwargs)
        if kwargs.get("provider_state") is not None:
            self.states.append(kwargs["provider_state"])


def test_a_publish_is_validated_drafted_journalled_then_published(vendor: StubVendor) -> None:
    journal = RecordingJournal()

    result = publish(vendor, journal)

    assert list(vendor.calls) == [
        ("POST", "/posts/validate"),
        ("POST", "/posts"),
        ("POST", f"/posts/{journal.provider_post_id}/publish"),
    ]
    assert journal.states == [ProviderState.DRAFT_CREATED, ProviderState.PUBLISH_REQUESTED]
    assert result.pending is True
    assert result.provider_post_id == journal.provider_post_id


def test_the_draft_is_created_without_publish_now_or_a_schedule(vendor: StubVendor) -> None:
    publish(vendor, MemoryJournal())

    create = vendor.bodies[1]
    assert "publish_now" not in create
    assert "scheduled_at" not in create
    assert create["text"] == "Spring is here. #new"
    assert create["targets"] == [{"account_id": ACCOUNT}]


def test_a_fast_publish_comes_back_published(vendor: StubVendor) -> None:
    vendor.fast_publish = True

    result = publish(vendor, MemoryJournal())

    assert result.pending is False
    assert result.platform_post_id.startswith("native-")
    assert result.permalink.startswith("https://platform.test/")
    assert result.published_at is not None


# -----------------------------------------------------------------------------
# Exactly once: retries and crashes resume, never repeat
# -----------------------------------------------------------------------------
def test_a_retry_after_the_publish_was_requested_never_publishes_again(
    vendor: StubVendor,
) -> None:
    journal = MemoryJournal()
    publish(vendor, journal)

    again = publish(vendor, journal)

    assert vendor.creates == 1
    assert vendor.publishes == 1
    assert again.pending is True


def test_a_crash_between_draft_and_publish_publishes_that_draft_once(
    vendor: StubVendor,
) -> None:
    journal = MemoryJournal()
    vendor.unreachable_on = "POST /posts/post-"  # the publish call is lost on the wire

    with pytest.raises(PlatformError) as caught:
        publish(vendor, journal)
    assert caught.value.retryable is True
    assert journal.provider_state == ProviderState.DRAFT_CREATED

    result = publish(vendor, journal)

    assert vendor.creates == 1, "the existing draft is published, never a second one"
    assert result.pending is True


def test_a_publish_that_landed_but_whose_answer_was_lost_is_not_sent_twice(
    vendor: StubVendor,
) -> None:
    journal = MemoryJournal()
    publish(vendor, journal)
    # The journal never heard "publish_requested" (crash after the call).
    journal.provider_state = ProviderState.DRAFT_CREATED

    publish(vendor, journal)

    assert vendor.publishes == 1
    assert journal.provider_state == ProviderState.PUBLISH_REQUESTED


def test_a_409_on_publish_is_read_back_not_retried(vendor: StubVendor) -> None:
    journal = MemoryJournal()
    publish(vendor, journal)
    pid = journal.provider_post_id
    vendor.settle(pid)

    result = vendor.adapter()._request_publish(pid, journal)

    assert result.pending is False
    assert result.platform_post_id == f"native-{pid}"


def test_a_vanished_draft_is_drafted_again(vendor: StubVendor) -> None:
    journal = MemoryJournal(provider_post_id="post-gone", provider_state="draft_created")

    publish(vendor, journal)

    assert vendor.creates == 1
    assert journal.provider_post_id != "post-gone"


def test_a_vanished_requested_publish_is_not_guessed(vendor: StubVendor) -> None:
    journal = MemoryJournal(provider_post_id="post-gone", provider_state="publish_requested")

    with pytest.raises(PlatformError) as caught:
        publish(vendor, journal)

    assert caught.value.code == "publish_unconfirmed"
    assert caught.value.retryable is False
    assert vendor.creates == 0


# -----------------------------------------------------------------------------
# Refused before anything exists at the vendor
# -----------------------------------------------------------------------------
def test_validation_errors_refuse_before_a_draft_exists(vendor: StubVendor) -> None:
    vendor.validate_errors = [{"field": "text", "message": "Caption is too long", "platform": "x"}]

    with pytest.raises(PlatformError) as caught:
        publish(vendor, MemoryJournal())

    assert caught.value.code == "publish_invalid"
    assert "Caption is too long" in caught.value.message
    assert vendor.creates == 0


def test_a_warning_that_drops_a_setting_we_sent_refuses_the_publish(vendor: StubVendor) -> None:
    vendor.validate_warnings = [
        {"field": "platform_data.instagram.location_id", "message": "location_id not sent"}
    ]

    with pytest.raises(PlatformError) as caught:
        publish(vendor, MemoryJournal(), payload=payload(options={"location_id": "1105069"}))

    assert caught.value.code == "publish_invalid"
    assert vendor.creates == 0


def test_a_best_practice_warning_does_not_block(vendor: StubVendor) -> None:
    vendor.validate_warnings = [{"field": "text", "message": "Consider fewer hashtags"}]

    assert publish(vendor, MemoryJournal()).pending is True


def test_an_unsupported_setting_the_person_chose_is_refused_without_a_call(
    vendor: StubVendor,
) -> None:
    with pytest.raises(PlatformError) as caught:
        publish(
            vendor,
            MemoryJournal(),
            platform="facebook",
            payload=payload(platform="facebook", options={"targeting_interests": ["running"]}),
        )

    assert caught.value.code == "publish_refused"
    assert "interest targeting" in caught.value.message
    assert vendor.calls == []


def test_a_publish_without_a_journal_is_refused_before_any_call(vendor: StubVendor) -> None:
    with pytest.raises(PlatformError) as caught:
        vendor.adapter().publish(
            platform="instagram",
            provider_account_id=ACCOUNT,
            payload=payload(),
            idempotency_key="key-1",
        )

    assert caught.value.code == "publish_journal_missing"
    assert vendor.calls == []


# -----------------------------------------------------------------------------
# Read-back (G9): a 201 does not prove the fields were kept
# -----------------------------------------------------------------------------
def test_a_draft_stored_without_its_media_is_deleted_and_refused(vendor: StubVendor) -> None:
    vendor.drop_media = True

    with pytest.raises(PlatformError) as caught:
        publish(
            vendor,
            MemoryJournal(),
            payload=payload(media=[{"id": 7, "kind": "IMAGE", "url": "/m/7.jpg", "alt": ""}]),
            media=[item(7)],
        )

    assert caught.value.code == "publish_readback_mismatch"
    assert vendor.publishes == 0
    assert vendor.posts == {}, "the draft that lost its media is deleted"


# -----------------------------------------------------------------------------
# Errors that arrive with the publish call
# -----------------------------------------------------------------------------
def test_the_monthly_post_limit_is_terminal_not_retried(vendor: StubVendor) -> None:
    vendor.publish_failure = error(429, "validation.post_limit_exhausted", "Monthly limit")

    with pytest.raises(PlatformError) as caught:
        publish(vendor, MemoryJournal())

    assert caught.value.retryable is False


def test_the_team_rate_limit_is_retried_after_the_vendors_wait(vendor: StubVendor) -> None:
    vendor.publish_failure = httpx.Response(
        429,
        headers={"Retry-After": "17"},
        json={"error": {"code": "ratelimit.exceeded", "message": "slow down"}},
    )

    with pytest.raises(PlatformError) as caught:
        publish(vendor, MemoryJournal())

    assert caught.value.retryable is True
    assert caught.value.retry_after == 17


def test_all_deliveries_failing_is_terminal_and_journalled(vendor: StubVendor) -> None:
    journal = MemoryJournal()
    vendor.publish_failure = error(422, "publishing.delivery.all_failed", "Media rejected")

    with pytest.raises(PlatformError) as caught:
        publish(vendor, journal)

    assert caught.value.retryable is False
    assert journal.provider_state == ProviderState.FAILED


def test_a_lapsed_account_says_reconnect(vendor: StubVendor) -> None:
    vendor.publish_failure = error(403, "account.reconnection_required", "Reconnect")

    with pytest.raises(PlatformError) as caught:
        publish(vendor, MemoryJournal())

    assert caught.value.needs_reauth is True


# -----------------------------------------------------------------------------
# Media (SA-31)
# -----------------------------------------------------------------------------
def test_media_is_uploaded_journalled_and_sent_in_order(vendor: StubVendor) -> None:
    journal = RecordingJournal()
    media_payload = [
        {"id": 7, "kind": "IMAGE", "url": "/m/7.jpg", "alt": ""},
        {"id": 3, "kind": "IMAGE", "url": "/m/3.jpg", "alt": ""},
    ]

    publish(
        vendor,
        journal,
        payload=payload(media=media_payload, post_format="CAROUSEL"),
        media=[item(7), item(3)],
    )

    assert journal.provider_media_ids == ["media-1", "media-2"]
    assert ProviderState.MEDIA_UPLOADED in journal.states
    create = next(b for b in vendor.bodies if "targets" in b and "media" in b)
    assert create["media"] == [
        {"source_type": "media_id", "source": "media-1"},
        {"source_type": "media_id", "source": "media-2"},
    ]


def test_a_retry_reuses_media_already_uploaded(vendor: StubVendor) -> None:
    journal = MemoryJournal(provider_media_ids=["media-earlier"])
    media_payload = [
        {"id": 7, "kind": "IMAGE", "url": "/m/7.jpg", "alt": ""},
        {"id": 3, "kind": "IMAGE", "url": "/m/3.jpg", "alt": ""},
    ]

    publish(
        vendor,
        journal,
        payload=payload(media=media_payload, post_format="CAROUSEL"),
        media=[item(7), item(3)],
    )

    assert vendor.count("POST", "/media/upload") == 1, "only the one not yet uploaded"
    assert journal.provider_media_ids[0] == "media-earlier"


def test_a_large_file_takes_the_presigned_path_and_is_verified(vendor: StubVendor) -> None:
    big = item(9, size=80 * 1024 * 1024, mime="video/mp4")

    publish(
        vendor,
        MemoryJournal(),
        payload=payload(
            media=[{"id": 9, "kind": "VIDEO", "url": "/m/9.mp4", "alt": ""}], post_format="REEL"
        ),
        media=[big],
    )

    assert vendor.count("GET", "/media/upload-url") == 1
    assert ("PUT", "storage") in vendor.calls
    assert vendor.count("POST", "/media/media-1/verify") == 1
    assert vendor.media["media-1"] == "ready"


def test_url_mode_sends_our_signed_url_and_uploads_nothing(
    vendor: StubVendor, settings: Any
) -> None:
    settings.SOCIALAPI_MEDIA_MODE = "url"

    publish(
        vendor,
        MemoryJournal(),
        payload=payload(media=[{"id": 7, "kind": "IMAGE", "url": "/m/7.jpg", "alt": ""}]),
        media=[item(7)],
    )

    assert vendor.count("POST", "/media/upload") == 0
    create = next(b for b in vendor.bodies if "targets" in b and "media" in b)
    assert create["media"] == [{"source_type": "url", "source": "https://cdn.example.com/7.jpg"}]


def test_media_that_does_not_match_the_post_is_refused(vendor: StubVendor) -> None:
    with pytest.raises(PlatformError) as caught:
        publish(
            vendor,
            MemoryJournal(),
            payload=payload(media=[{"id": 7, "kind": "IMAGE", "url": "/m/7.jpg", "alt": ""}]),
            media=[item(8)],
        )

    assert caught.value.code == "publish_media_mismatch"
    assert vendor.calls == []


@pytest.mark.parametrize(
    ("fake_storage", "endpoint", "expected"),
    [
        (True, "", "upload"),
        (False, "http://localhost:9000", "upload"),
        (False, "http://127.0.0.1:9000", "upload"),
        (False, "http://10.0.0.5:9000", "upload"),
        (False, "http://minio:9000", "upload"),
        (False, "https://s3.eu-west-3.amazonaws.com", "url"),
    ],
)
def test_media_mode_is_upload_wherever_the_vendor_cannot_fetch(
    settings: Any, fake_storage: bool, endpoint: str, expected: str
) -> None:
    settings.SOCIALAPI_MEDIA_MODE = ""
    settings.USE_FAKE_STORAGE = fake_storage
    settings.AWS_S3_ENDPOINT_URL = endpoint

    assert media_mode() == expected


def test_released_media_keeps_what_is_still_in_use(vendor: StubVendor) -> None:
    vendor.media = {"media-1": "ready", "media-2": "ready"}
    vendor.media_in_use = {"media-2"}

    kept = vendor.adapter().release_media(media_ids=["media-1", "media-2", "media-gone"])

    assert kept == ["media-2"]
    assert vendor.deleted_media == ["media-1"]


# -----------------------------------------------------------------------------
# After the publish
# -----------------------------------------------------------------------------
def test_the_status_reads_published_failed_and_still_publishing(vendor: StubVendor) -> None:
    adapter = vendor.adapter()
    for pid in ("a", "b", "c"):
        journal = MemoryJournal()
        publish(vendor, journal)
        vendor.posts[pid] = vendor.posts.pop(journal.provider_post_id)
        vendor.posts[pid]["id"] = pid
    vendor.settle("a")
    vendor.settle("b", published=False)

    assert adapter.fetch_publish_status(provider_post_id="a").platform_post_id == "native-a"
    failed = adapter.fetch_publish_status(provider_post_id="b")
    assert failed.failed and "too long" in failed.error["message"]
    assert adapter.fetch_publish_status(provider_post_id="c").pending is True
    assert adapter.fetch_publish_status(provider_post_id="zzz").failed is True


def test_published_without_a_platform_id_is_still_pending(vendor: StubVendor) -> None:
    journal = MemoryJournal()
    publish(vendor, journal)
    vendor.posts[journal.provider_post_id]["targets"][0]["status"] = "published"

    status = vendor.adapter().fetch_publish_status(provider_post_id=journal.provider_post_id)

    assert status.pending is True, "the vendor's success rule: published and an id"


def test_a_draft_is_discarded_but_a_live_post_never_is(vendor: StubVendor) -> None:
    adapter = vendor.adapter()
    live = MemoryJournal()
    publish(vendor, live)
    drafted = vendor._create({"text": "x", "targets": [{"account_id": ACCOUNT}]}).json()["id"]

    adapter.discard_draft(provider_post_id=drafted)
    with pytest.raises(PlatformError, match="Refusing to delete"):
        adapter.discard_draft(provider_post_id=live.provider_post_id)
    adapter.discard_draft(provider_post_id="already-gone")

    assert drafted not in vendor.posts
    assert vendor.deleted_live == []


def test_stale_drafts_lists_only_old_drafts(vendor: StubVendor) -> None:
    import datetime as dt

    vendor._create({"text": "old", "targets": [{"account_id": ACCOUNT}]})
    journal = MemoryJournal()
    publish(vendor, journal)

    stale = vendor.adapter().stale_drafts(
        profile_id="brand-1", created_before=dt.datetime.now(dt.UTC)
    )

    assert stale == ["post-1"]
