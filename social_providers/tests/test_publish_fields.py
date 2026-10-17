"""SA-30: the declared mapping from `render_post` output to SocialAPI's body.

The gate is the same one Phase 4 holds Zernio to — every option a platform
declares is accounted for — with one difference: SocialAPI cannot carry some
of them, so "accounted for" includes an explicit `Unsupported` with the reason
the person is shown. Then rule 2: the body comes from the rendered payload and
nothing else, so preview and publish send the same bytes.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from django.utils import timezone

from content.models import Platform, PostTarget
from content.services.options import OptionError
from content.services.options import validate as validate_options
from content.services.posts import create_post
from content.services.rules import PLATFORM_RULES, options_for
from scheduling.services import schedule_post
from social_providers.socialapi.publish_fields import (
    MAPPINGS,
    To,
    Unsupported,
    build_request,
    refusals,
)

pytestmark = pytest.mark.django_db

ACCOUNT = "acc_1"


def _payload(platform: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": platform,
        "post_format": "FEED",
        "body": "Hello",
        "thread": [],
        "media": [],
        "options": {},
    }
    base.update(over)
    return base


def _body(platform: str, **over: Any) -> dict[str, Any]:
    return build_request(
        platform=platform,
        provider_account_id=ACCOUNT,
        payload=_payload(platform, **over),
        media_sources=[],
    ).body


# -----------------------------------------------------------------------------
# The gate: every declared option has a row, every row a declaration
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("platform", sorted(PLATFORM_RULES))
def test_every_declared_option_is_mapped_or_explicitly_unsupported(platform: str) -> None:
    declared = set(options_for(platform))
    mapped = set(MAPPINGS[platform].options)

    assert declared == mapped, (
        f"{platform}: unaccounted {sorted(declared - mapped)}, "
        f"undeclared {sorted(mapped - declared)}"
    )


def test_every_platform_has_a_row() -> None:
    assert set(MAPPINGS) == set(Platform.values)


def test_every_unsupported_option_says_why() -> None:
    for platform, mapping in MAPPINGS.items():
        for key, rule in mapping.options.items():
            if isinstance(rule, Unsupported):
                assert len(rule.reason) > 20, f"{platform}.{key} needs a reason a person can read"


def test_every_destination_is_a_target_field_or_platform_data() -> None:
    for mapping in MAPPINGS.values():
        for rule in mapping.options.values():
            if isinstance(rule, Unsupported):
                continue
            for destination in rule:
                assert isinstance(destination, To)
                assert destination.path.split(".", 1)[0] in ("target", "data")


# -----------------------------------------------------------------------------
# What a row produces
# -----------------------------------------------------------------------------
def test_text_is_the_payload_body_byte_for_byte() -> None:
    text = "Café — “quoted” 🍵 #new\n\nline two"

    assert _body("instagram", body=text)["text"] == text


def test_instagram_options_nest_under_its_slug_and_formats_select_content_type() -> None:
    body = _body(
        "instagram",
        post_format="REEL",
        options={
            "first_comment": "More in bio",
            "location_id": "110506962309835",
            "collab_handles": ["@studio", "maker"],
            "share_to_feed": True,
        },
    )

    assert body["targets"] == [{"account_id": ACCOUNT, "first_comment": "More in bio"}]
    assert body["platform_data"] == {
        "instagram": {
            "location_id": "110506962309835",
            "collaborators": ["studio", "maker"],
            "share_to_feed": True,
            "content_type": "reel",
        }
    }


def test_a_reel_only_setting_is_not_sent_on_a_feed_post() -> None:
    body = _body("instagram", options={"share_to_feed": True})

    assert "platform_data" not in body, "a feed post would answer with a dropped-key warning"


def test_facebook_reads_flat_keys_on_the_target() -> None:
    body = _body(
        "facebook",
        options={
            "location_id": "123",
            "targeting_countries": ["fr", "be"],
            "targeting_min_age": 18,
        },
    )

    assert "platform_data" not in body
    assert body["targets"][0]["platform_data"] == {
        "place": "123",
        "countries": ["FR", "BE"],
        "min_age": 18,
    }


def test_tiktok_turns_allow_into_disable() -> None:
    body = _body(
        "tiktok",
        options={"privacy_level": "SELF_ONLY", "allow_comments": True, "allow_duet": False},
    )

    assert body["platform_data"]["tiktok"] == {
        "privacy_level": "SELF_ONLY",
        "disable_comment": False,
        "disable_duet": True,
    }


def test_youtube_title_and_privacy_are_target_fields() -> None:
    body = _body("youtube", options={"title": "Walkthrough", "privacy": "unlisted"})

    assert body["targets"][0] == {
        "account_id": ACCOUNT,
        "title": "Walkthrough",
        "visibility": "unlisted",
    }


def test_pinterest_board_is_a_target_field_never_platform_data() -> None:
    body = _body(
        "pinterest",
        options={"board_id": "8828", "title": "Salmon", "destination_link": "https://x.test"},
    )

    assert body["targets"][0]["board_id"] == "8828"
    assert body["targets"][0]["platform_data"] == {"title": "Salmon", "link": "https://x.test"}


def test_google_event_and_offer_fields_build_nested_objects() -> None:
    body = _body(
        "google_business",
        options={
            "post_type": "EVENT",
            "cta_type": "BOOK",
            "cta_url": "https://shop.test",
            "event_title": "Anniversary",
            "event_start": "2026-05-01T18:30:00+02:00",
            "event_end": "2026-05-01",
        },
    )

    google = body["platform_data"]["google"]
    assert google["topicType"] == "EVENT"
    assert google["callToAction"] == {"actionType": "BOOK", "url": "https://shop.test"}
    assert google["event"] == {
        "title": "Anniversary",
        "schedule": {
            "startDate": {"year": 2026, "month": 5, "day": 1},
            "startTime": {"hours": 18, "minutes": 30},
            "endDate": {"year": 2026, "month": 5, "day": 1},
        },
    }


def test_linkedin_visibility_and_alt_texts_by_position() -> None:
    body = _body(
        "linkedin",
        options={"visibility": "CONNECTIONS"},
        media=[{"id": 1, "alt": "First"}, {"id": 2, "alt": "Second"}],
    )

    assert body["targets"][0]["visibility"] == "connections_only"
    assert body["targets"][0]["platform_data"] == {"alt_texts": ["First", "Second"]}


def test_a_threads_thread_travels_as_continuation_segments() -> None:
    body = _body("threads", body="Part one (1/3)", thread=["Part one (1/3)", "two", "three"])

    assert body["text"] == "Part one (1/3)"
    assert body["segments"] == [{"text": "two"}, {"text": "three"}]


def test_the_body_never_asks_to_publish_or_schedule() -> None:
    for platform in MAPPINGS:
        body = _body(platform)
        assert "publish_now" not in body
        assert "scheduled_at" not in body


# -----------------------------------------------------------------------------
# Refusals: a chosen setting the vendor cannot carry stops the publish
# -----------------------------------------------------------------------------
def test_an_untouched_default_is_not_a_choice() -> None:
    assert refusals("linkedin", _payload("linkedin", options={"visibility": "PUBLIC"})) == []


def test_a_chosen_unsupported_setting_is_refused_with_its_reason() -> None:
    reasons = refusals("linkedin", _payload("linkedin", options={"first_comment": "Hi"}))

    assert len(reasons) == 1
    assert "first comment" in reasons[0]


def test_tiktok_requires_an_explicit_privacy_level_when_composed() -> None:
    """TikTok's rule, whichever provider publishes: refused at compose time."""
    with pytest.raises(OptionError) as caught:
        validate_options("tiktok", {"allow_comments": True})
    assert "privacy_level" in caught.value.errors

    assert (
        validate_options("tiktok", {"privacy_level": "SELF_ONLY"})["privacy_level"] == "SELF_ONLY"
    )
    assert refusals("tiktok", _payload("tiktok", options={"privacy_level": "SELF_ONLY"})) == []


# -----------------------------------------------------------------------------
# Rule 2: preview and publish send the same bytes
# -----------------------------------------------------------------------------
def test_preview_and_publish_send_the_same_body(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = create_post(
        workspace=paid_workspace, author=user, master_body="Handmade in Lisbon #ceramics"
    )
    schedule_post(
        post=post,
        delivery_mode="AUTO_PUBLISH",
        scheduled_at=timezone.now() + dt.timedelta(minutes=5),
        actor=user,
    )
    preview = auth_client.post(
        "/api/v1/posts/preview/",
        {"master_body": post.master_body, "platforms": ["instagram"]},
        format="json",
    ).json()["payloads"]["instagram"]
    stored = PostTarget.objects.get(post=post).rendered_payload

    def send(payload: dict[str, Any]) -> bytes:
        built = build_request(
            platform="instagram",
            provider_account_id=ACCOUNT,
            payload=payload,
            media_sources=[],
        )
        return json.dumps(built.body, sort_keys=True).encode()

    assert send(preview) == send(stored)
