"""Per-format constraints (P4-04, P4-05).

**Media constraints belong to `(platform, format)`, not to the platform.** A
platform-level `max_media` cannot express the thing that is actually true: a
LinkedIn feed post takes nine images, a LinkedIn document carousel takes one
PDF, and an Instagram reel takes exactly one video where its carousel takes
ten. Modelling the cap on the platform alone forces every format to share the
loosest of them, which means the composer accepts a post the provider will
reject — the failure lands at publish time, on the one path nobody is watching.

Declared as data like everything else in `rules.py`: a format is a row, not a
branch. `test_phase4_gates.py` is what holds that to account.
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import MediaKind, Platform, PostFormat
from content.services.adaptation import adapt_for_platform
from content.services.rules import PLATFORM_RULES, fitting_format, format_rule, formats_for

pytestmark = pytest.mark.django_db


class _Asset:
    """The `MediaLike` shape the engine reads — id, kind, file."""

    def __init__(self, asset_id: int, kind: str = MediaKind.IMAGE) -> None:
        self.id = asset_id
        self.kind = kind
        self.file = None


class TestTheTable:
    def test_every_platform_declares_at_least_one_format(self) -> None:
        for platform, rule in PLATFORM_RULES.items():
            assert rule.formats, f"{platform} declares no format at all"

    def test_every_platform_declares_feed(self) -> None:
        """`FEED` is the format a post has when nobody chose one, so a platform
        without it has no default and every target would need an explicit
        choice the composer has no reason to ask for."""
        for platform, rule in PLATFORM_RULES.items():
            assert PostFormat.FEED in rule.formats, f"{platform} has no FEED format"

    def test_every_declared_format_is_a_real_one(self) -> None:
        for platform, rule in PLATFORM_RULES.items():
            unknown = sorted(set(rule.formats) - set(PostFormat.values))
            assert not unknown, f"{platform} declares unknown format(s) {unknown}"

    def test_every_format_allows_some_media_kind(self) -> None:
        # A format allowing nothing is a format that can never carry a post.
        for platform, rule in PLATFORM_RULES.items():
            for name, spec in rule.formats.items():
                assert spec.allowed_media_kinds, f"{platform}.{name} allows no media kind"

    def test_a_format_requiring_media_allows_at_least_that_many(self) -> None:
        for platform, rule in PLATFORM_RULES.items():
            for name, spec in rule.formats.items():
                assert spec.min_media <= spec.max_media, (
                    f"{platform}.{name} requires {spec.min_media} and permits {spec.max_media}"
                )

    def test_formats_for_lists_what_a_platform_supports(self) -> None:
        assert PostFormat.FEED in formats_for(Platform.INSTAGRAM)

    def test_formats_for_an_unknown_platform_is_empty_not_an_error(self) -> None:
        # The same reasoning `options_for` follows: an unknown platform is
        # refused upstream, and exploding here turns a 400 into a 500.
        assert formats_for("myspace") == {}


class TestResolution:
    def test_a_declared_format_resolves_to_its_own_row(self) -> None:
        reel = format_rule(Platform.INSTAGRAM, PostFormat.REEL)

        assert reel is not None
        assert reel.allowed_media_kinds == frozenset({MediaKind.VIDEO})

    def test_an_undeclared_format_resolves_to_nothing(self) -> None:
        # Instagram has no `SHORT` — that is YouTube's name for the same idea,
        # and silently aliasing them would let a composer offer a format the
        # provider will reject.
        assert format_rule(Platform.INSTAGRAM, PostFormat.SHORT) is None

    def test_a_format_may_narrow_the_character_limit(self) -> None:
        # Declared `None` means "inherit the platform's" rather than "no
        # limit", so a format that does not care says nothing.
        feed = format_rule(Platform.INSTAGRAM, PostFormat.FEED)
        assert feed is not None
        assert feed.char_limit is None


class TestAdaptationUsesTheFormat:
    def test_a_reel_given_three_videos_goes_out_as_a_carousel(self) -> None:
        # The media decides the shape when the chosen one cannot carry it:
        # three videos make a carousel, not a reel with two thrown away.
        videos = [_Asset(index, MediaKind.VIDEO) for index in range(1, 4)]

        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=videos,
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.REEL,
        )

        assert payload.post_format == PostFormat.CAROUSEL
        assert len(payload.media) == 3

    def test_a_carousel_keeps_the_carousels_cap(self) -> None:
        images = [_Asset(index) for index in range(1, 12)]

        carousel = adapt_for_platform(
            master_body="Hello",
            media_assets=images,
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.CAROUSEL,
        )

        assert len(carousel.media) == 10

    def test_a_reel_given_an_image_goes_out_as_a_post(self) -> None:
        # Platform-level, Instagram allows images; format-level, a reel does
        # not (P4-05). The image is kept and the shape follows it.
        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=[_Asset(1, MediaKind.IMAGE)],
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.REEL,
        )

        assert payload.post_format == PostFormat.FEED
        assert [m.id for m in payload.media] == [1]

    def test_media_no_content_type_takes_is_still_dropped_and_said(self) -> None:
        # A pin is an image: a video has no Pinterest shape to move to.
        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=[_Asset(1, MediaKind.VIDEO)],
            platform=Platform.PINTEREST,
        )

        assert payload.media == []
        assert any("does not support that media type" in w for w in payload.warnings)

    def test_the_format_travels_on_the_payload(self) -> None:
        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=[],
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.REEL,
        )

        assert payload.post_format == PostFormat.REEL

    def test_feed_is_the_default_when_no_format_is_named(self) -> None:
        payload = adapt_for_platform(
            master_body="Hello", media_assets=[], platform=Platform.INSTAGRAM
        )

        assert payload.post_format == PostFormat.FEED

    def test_a_format_the_platform_does_not_support_falls_back_to_feed(self) -> None:
        """The engine renders rather than raising.

        A target carrying an unsupported format is a validation failure that
        belongs at the write, and `PostTarget.clean` is where it happens — by
        the time the renderer runs, refusing would mean a preview that 500s
        instead of showing the post. It warns, which is what every other
        can't-honour-this case here does.
        """
        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=[_Asset(1, MediaKind.IMAGE)],
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.SHORT,
        )

        assert payload.post_format == PostFormat.FEED
        assert any("does not support" in warning for warning in payload.warnings)


class TestTargetValidation:
    def test_a_target_accepts_a_format_its_platform_declares(
        self, workspace: Any, user: Any
    ) -> None:
        from content.models import PostTarget
        from content.services.posts import create_post

        post = create_post(workspace=workspace, author=user, master_body="Hello")
        target = PostTarget(post=post, platform=Platform.INSTAGRAM, post_format=PostFormat.REEL)

        target.full_clean(exclude=["social_account", "idempotency_key"])

    def test_a_target_refuses_a_format_its_platform_does_not_declare(
        self, workspace: Any, user: Any
    ) -> None:
        from django.core.exceptions import ValidationError

        from content.models import PostTarget
        from content.services.posts import create_post

        post = create_post(workspace=workspace, author=user, master_body="Hello")
        target = PostTarget(post=post, platform=Platform.INSTAGRAM, post_format=PostFormat.SHORT)

        with pytest.raises(ValidationError):
            target.full_clean(exclude=["social_account", "idempotency_key"])

    def test_a_target_defaults_to_feed(self, workspace: Any, user: Any) -> None:
        from content.models import PostTarget
        from content.services.posts import create_post

        post = create_post(workspace=workspace, author=user, master_body="Hello")

        assert PostTarget(post=post, platform=Platform.INSTAGRAM).post_format == PostFormat.FEED


def test_each_platform_offers_the_content_types_its_provider_publishes() -> None:
    """SocialAPI's publishing pages, read 2026-11-13: what the composer offers
    is what can actually go out, and `FEED` (a plain post) is always there."""
    assert {platform: sorted(formats_for(platform)) for platform in PLATFORM_RULES} == {
        Platform.INSTAGRAM: ["CAROUSEL", "FEED", "REEL", "STORY"],
        Platform.FACEBOOK: ["FEED", "REEL", "STORY"],
        Platform.TIKTOK: ["CAROUSEL", "FEED"],
        Platform.YOUTUBE: ["FEED", "SHORT"],
        Platform.LINKEDIN: ["CAROUSEL", "FEED"],
        Platform.THREADS: ["FEED"],
        Platform.X: ["FEED"],
        Platform.PINTEREST: ["FEED"],
        Platform.GOOGLE_BUSINESS: ["FEED"],
    }


def test_an_instagram_post_is_one_image_and_a_pin_never_a_video() -> None:
    for platform in (Platform.INSTAGRAM, Platform.PINTEREST):
        rule = format_rule(platform, PostFormat.FEED)
        assert rule is not None
        assert rule.allowed_media_kinds == {MediaKind.IMAGE}, platform
    assert format_rule(Platform.INSTAGRAM, PostFormat.FEED).max_media == 1  # type: ignore[union-attr]


class TestTheMediaDecidesTheShape:
    """`fitting_format`: the chosen type while the media fits it, else the
    first declared type it does fit. Read from the table, so one rule serves
    every platform."""

    IMAGE, VIDEO = MediaKind.IMAGE, MediaKind.VIDEO

    @pytest.mark.parametrize(
        ("kinds", "expected"),
        [
            ([IMAGE], PostFormat.FEED),
            ([VIDEO], PostFormat.REEL),
            ([IMAGE, IMAGE], PostFormat.CAROUSEL),
            ([IMAGE, VIDEO, IMAGE], PostFormat.CAROUSEL),
        ],
    )
    def test_instagram_left_on_post_follows_its_media(
        self, kinds: list[str], expected: str
    ) -> None:
        assert fitting_format(Platform.INSTAGRAM, PostFormat.FEED, kinds) == expected

    def test_a_choice_the_media_still_fits_is_kept(self) -> None:
        # A story takes one image or one video: the person's pick stands.
        assert fitting_format(Platform.INSTAGRAM, PostFormat.STORY, [MediaKind.VIDEO]) == (
            PostFormat.STORY
        )
        assert (
            fitting_format(Platform.LINKEDIN, PostFormat.CAROUSEL, [MediaKind.IMAGE] * 3)
            == PostFormat.CAROUSEL
        )

    def test_a_carousel_cut_down_to_one_image_becomes_a_post(self) -> None:
        assert fitting_format(Platform.INSTAGRAM, PostFormat.CAROUSEL, [MediaKind.IMAGE]) == (
            PostFormat.FEED
        )

    def test_the_same_rule_reads_another_platforms_table(self) -> None:
        # TikTok: a video is its plain post, images are its photo post.
        assert fitting_format(Platform.TIKTOK, PostFormat.FEED, [MediaKind.VIDEO]) == (
            PostFormat.FEED
        )
        assert fitting_format(Platform.TIKTOK, PostFormat.FEED, [MediaKind.IMAGE] * 2) == (
            PostFormat.CAROUSEL
        )

    def test_when_nothing_fits_the_choice_stands(self) -> None:
        # No media for Instagram, a video for Pinterest, eleven for a carousel:
        # nothing to move to, so scheduling is what refuses it.
        assert fitting_format(Platform.INSTAGRAM, PostFormat.FEED, []) == PostFormat.FEED
        assert fitting_format(Platform.PINTEREST, PostFormat.FEED, [MediaKind.VIDEO]) == (
            PostFormat.FEED
        )
        assert (
            fitting_format(Platform.INSTAGRAM, PostFormat.CAROUSEL, [MediaKind.IMAGE] * 11)
            == PostFormat.CAROUSEL
        )
