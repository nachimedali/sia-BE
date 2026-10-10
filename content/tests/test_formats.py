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
from content.services.rules import PLATFORM_RULES, format_rule, formats_for

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
    def test_a_reel_keeps_one_video_where_the_carousel_would_keep_ten(self) -> None:
        videos = [_Asset(index, MediaKind.VIDEO) for index in range(1, 4)]

        reel = adapt_for_platform(
            master_body="Hello",
            media_assets=videos,
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.REEL,
        )

        assert len(reel.media) == 1
        assert any("at most 1" in warning for warning in reel.warnings)

    def test_a_carousel_keeps_the_carousels_cap(self) -> None:
        images = [_Asset(index) for index in range(1, 12)]

        carousel = adapt_for_platform(
            master_body="Hello",
            media_assets=images,
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.CAROUSEL,
        )

        assert len(carousel.media) == 10

    def test_a_reel_drops_an_image_because_its_format_takes_video(self) -> None:
        # Platform-level, Instagram allows images. Format-level, a reel does
        # not — which is the whole point of moving the constraint (P4-05).
        payload = adapt_for_platform(
            master_body="Hello",
            media_assets=[_Asset(1, MediaKind.IMAGE)],
            platform=Platform.INSTAGRAM,
            post_format=PostFormat.REEL,
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
