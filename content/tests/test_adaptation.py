"""The Adaptation Engine (design.md §8.6)."""

from __future__ import annotations

from content.models import MediaKind, Platform
from content.services.adaptation import adapt_for_platform
from content.services.rules import PLATFORM_RULES


class _FakeAsset:
    """A stand-in for `MediaAsset` — the engine only reads `id`, `kind` and
    `file.url`, so a unit test has no reason to hit the database or storage."""

    def __init__(self, id: int, kind: str, url: str = "") -> None:
        self.id = id
        self.kind = kind
        self.file = _FakeFile(url) if url else None


class _FakeFile:
    def __init__(self, url: str) -> None:
        self.url = url


def test_short_body_passes_through_untouched() -> None:
    payload = adapt_for_platform(
        master_body="Hello world", media_assets=[], platform=Platform.LINKEDIN
    )

    assert payload.body == "Hello world"
    assert payload.truncated is False
    assert payload.thread == []
    assert payload.warnings == []


def test_adaptation_truncates_and_threads_per_platform_rules() -> None:
    long_body = "word " * 700  # ~3500 chars, over every platform's limit.

    # LinkedIn does not support threading: truncates with an ellipsis.
    linkedin_rule = PLATFORM_RULES[Platform.LINKEDIN]
    linkedin_payload = adapt_for_platform(
        master_body=long_body, media_assets=[], platform=Platform.LINKEDIN
    )
    assert linkedin_payload.truncated is True
    assert linkedin_payload.thread == []
    assert len(linkedin_payload.body) <= linkedin_rule.char_limit
    assert linkedin_payload.body.endswith("…")
    assert linkedin_payload.warnings

    # Threads supports threading: splits into numbered chunks, nothing lost.
    threads_rule = PLATFORM_RULES[Platform.THREADS]
    threads_payload = adapt_for_platform(
        master_body=long_body, media_assets=[], platform=Platform.THREADS
    )
    assert threads_payload.truncated is False
    assert len(threads_payload.thread) > 1
    assert threads_payload.body == threads_payload.thread[0]
    for i, chunk in enumerate(threads_payload.thread, start=1):
        assert chunk.endswith(f"({i}/{len(threads_payload.thread)})")
        # The suffix itself must fit inside the platform's own limit.
        assert len(chunk) <= threads_rule.char_limit

    # Reassembling the thread loses no words from the original body.
    reassembled = " ".join(chunk.rsplit(" (", 1)[0] for chunk in threads_payload.thread)
    assert reassembled.split() == long_body.split()


def test_a_single_overlong_word_is_hard_split_in_a_thread() -> None:
    url = "x" * 900
    payload = adapt_for_platform(master_body=url, media_assets=[], platform=Platform.THREADS)

    assert len(payload.thread) > 1
    for chunk in payload.thread:
        assert len(chunk) <= PLATFORM_RULES[Platform.THREADS].char_limit


def test_instagram_moves_hashtags_to_a_trailing_block() -> None:
    body = "New drop is live #launch in the studio #newcollection today"
    payload = adapt_for_platform(master_body=body, media_assets=[], platform=Platform.INSTAGRAM)

    assert payload.hashtags == ["launch", "newcollection"]
    assert "#launch" not in payload.body.split("\n\n")[0]
    assert payload.body.endswith("#launch #newcollection")


def test_linkedin_keeps_hashtags_inline() -> None:
    body = "New drop is live #launch today"
    payload = adapt_for_platform(master_body=body, media_assets=[], platform=Platform.LINKEDIN)

    assert payload.hashtags == ["launch"]
    assert payload.body == body


def test_hashtags_are_deduplicated_case_insensitively() -> None:
    payload = adapt_for_platform(
        master_body="great #Launch day, the #launch is here",
        media_assets=[],
        platform=Platform.LINKEDIN,
    )
    assert payload.hashtags == ["Launch"]


def test_media_dropped_beyond_the_platform_cap() -> None:
    assets = [_FakeAsset(i, MediaKind.IMAGE, f"/media/{i}.png") for i in range(15)]
    payload = adapt_for_platform(
        master_body="carousel", media_assets=assets, platform=Platform.INSTAGRAM
    )

    assert len(payload.media) == PLATFORM_RULES[Platform.INSTAGRAM].max_media
    assert payload.media[0].id == 0
    assert any("allows at most" in warning for warning in payload.warnings)


def test_media_kind_not_supported_by_platform_is_dropped() -> None:
    assets = [_FakeAsset(1, MediaKind.IMAGE, "/media/1.png")]
    payload = adapt_for_platform(
        master_body="new video", media_assets=assets, platform=Platform.TIKTOK
    )

    assert payload.media == []
    assert any("does not support that media type" in warning for warning in payload.warnings)


def test_as_dict_is_json_shaped() -> None:
    payload = adapt_for_platform(
        master_body="hi #x",
        media_assets=[_FakeAsset(1, MediaKind.IMAGE, "/m/1.png")],
        platform=Platform.FACEBOOK,
    )
    as_dict = payload.as_dict()

    assert as_dict["media"] == [{"id": 1, "kind": MediaKind.IMAGE, "url": "/m/1.png"}]
    assert set(as_dict) == {
        "platform",
        "body",
        "thread",
        "hashtags",
        "media",
        "truncated",
        "warnings",
    }
