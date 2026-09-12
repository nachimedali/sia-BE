"""P4-G1 — **a new platform added with no new code path.**

Phase 4's whole purpose is to find out whether Phase 1's abstraction held.
Three platforms were added — X, Pinterest and Google Business Profile — and
the claim this file makes falsifiable is that each cost *rows in tables* and
nothing else.

Two halves, because "no new code path" is two different statements:

* **Nothing branches on a platform name.** Asserted by reading the source of
  every module that consumes the rules, so a future `if platform == "x"` fails
  here rather than being noticed in review or not at all.
* **A platform that does not exist still works.** A synthetic row is pushed
  into the tables and driven through the real engine end to end. If adding a
  platform needed code, a platform made of nothing but data could not render.

**What is deliberately *not* claimed.** The connect/selection flow in
`channels/adapters/zernio.py` branches per platform, and that is correct: the
vendor exposes genuinely different endpoints and body shapes for Facebook
pages and LinkedIn organisations, and flattening those into a table would be
building a schema language — the thing `rules.Option` explicitly refuses to
be. The adapter is the layer whose job is absorbing vendor shape. P4-06's
hard stop is about the *adaptation* abstraction, which is what `_RULE_READERS`
below pins.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from content.models import MediaKind, Platform, PostFormat
from content.services import adaptation, options
from content.services import rules as rules_module
from content.services.rules import PLATFORM_RULES, FormatRule, PlatformRule

#: The modules that read the rules table. A platform name appearing in any of
#: them is the hard stop firing.
#:
#: `rules.py` itself is absent on purpose — it is the *declaration*, so every
#: platform name appears there by definition. That file is the one place they
#: are allowed to be.
_RULE_READERS = (adaptation, options)


def test_no_rule_reader_branches_on_a_platform_name() -> None:
    for module in _RULE_READERS:
        source = inspect.getsource(module)
        for platform in Platform.values:
            assert f'"{platform}"' not in source, (
                f"{module.__name__} names {platform!r}. Platforms are declared in rules.py; "
                "branching on one here is the P4-06 hard stop."
            )


def test_the_engine_reads_the_table_rather_than_knowing_the_platforms() -> None:
    """Every platform in the enum renders, with no per-platform arrangement.

    A platform the engine had special knowledge of would be one that worked
    only when set up a particular way — this asks all of them the same
    question and expects an answer from each.
    """
    for platform in Platform.values:
        payload = adaptation.adapt_for_platform(
            master_body="Launch day #news", media_assets=[], platform=platform
        )

        assert payload.platform == platform
        assert payload.hashtags == ["news"]


class _SyntheticAsset:
    def __init__(self, asset_id: int, kind: str = MediaKind.IMAGE) -> None:
        self.id = asset_id
        self.kind = kind
        self.file = None


@pytest.fixture
def invented_platform() -> Any:
    """A seventh platform that exists only as data, for the length of one test.

    The strongest available form of the gate: if adding a platform required
    code, a platform made of **nothing but a table row** could not possibly
    render — and this fixture writes no code at all.
    """
    name = "myspace"
    PLATFORM_RULES[name] = PlatformRule(
        char_limit=42,
        formats={
            PostFormat.FEED: FormatRule(
                max_media=2, allowed_media_kinds=frozenset({MediaKind.IMAGE})
            ),
        },
        hashtag_placement="trailing_block",
        supports_thread=True,
        options=(rules_module.Option("mood", "choice", "Mood", choices=("glum", "chipper")),),
    )
    try:
        yield name
    finally:
        del PLATFORM_RULES[name]


class TestAPlatformMadeOnlyOfData:
    def test_it_renders(self, invented_platform: str) -> None:
        payload = adaptation.adapt_for_platform(
            master_body="Hello there", media_assets=[], platform=invented_platform
        )

        assert payload.platform == invented_platform
        assert payload.body == "Hello there"

    def test_it_honours_its_own_character_limit(self, invented_platform: str) -> None:
        payload = adaptation.adapt_for_platform(
            master_body="word " * 50, media_assets=[], platform=invented_platform
        )

        # Declared `supports_thread`, so it threads rather than truncating —
        # reached by the flag, never by a list of threading platforms.
        assert payload.thread
        assert all(len(chunk) <= 42 for chunk in payload.thread)

    def test_it_honours_its_own_media_cap(self, invented_platform: str) -> None:
        payload = adaptation.adapt_for_platform(
            master_body="Hi",
            media_assets=[_SyntheticAsset(index) for index in range(1, 6)],
            platform=invented_platform,
        )

        assert len(payload.media) == 2

    def test_it_honours_its_own_media_kinds(self, invented_platform: str) -> None:
        payload = adaptation.adapt_for_platform(
            master_body="Hi",
            media_assets=[_SyntheticAsset(1, MediaKind.VIDEO)],
            platform=invented_platform,
        )

        assert payload.media == []

    def test_its_declared_options_validate(self, invented_platform: str) -> None:
        cleaned = options.validate(invented_platform, {"mood": "chipper"})

        assert cleaned["mood"] == "chipper"

    def test_an_undeclared_choice_is_refused(self, invented_platform: str) -> None:
        with pytest.raises(options.OptionError):
            options.validate(invented_platform, {"mood": "indifferent"})

    def test_its_hashtag_placement_is_honoured(self, invented_platform: str) -> None:
        # `trailing_block`, declared — the engine moves the tags to the end
        # because the row says so, not because it recognises the platform.
        payload = adaptation.adapt_for_platform(
            master_body="Hi #launch", media_assets=[], platform=invented_platform
        )

        assert payload.body.endswith("#launch")
        assert payload.hashtags == ["launch"]


class TestTheThreePlatformsCostOnlyRows:
    """P4-01, P4-02, P4-03 — stated as the property, not the changelog."""

    NEW = (Platform.X, Platform.PINTEREST, Platform.GOOGLE_BUSINESS)

    @pytest.mark.parametrize("platform", NEW)
    def test_it_has_a_rules_row(self, platform: str) -> None:
        assert platform in PLATFORM_RULES

    @pytest.mark.parametrize("platform", NEW)
    def test_it_has_a_provider_field_row(self, platform: str) -> None:
        """Its options reach the vendor. A platform whose options were declared
        and never mapped would compose perfectly and publish without them —
        the settings would vanish between the form and the post."""
        from channels.adapters.zernio import PROVIDER_OPTION_FIELDS

        declared = {option.key for option in PLATFORM_RULES[platform].options}
        mapped = set(PROVIDER_OPTION_FIELDS.get(platform, {}))

        assert declared == mapped, (
            f"{platform}: declared-but-unmapped {sorted(declared - mapped)}, "
            f"mapped-but-undeclared {sorted(mapped - declared)}"
        )

    @pytest.mark.parametrize("platform", NEW)
    def test_it_declares_at_least_one_format(self, platform: str) -> None:
        assert PLATFORM_RULES[platform].formats


def test_every_platform_maps_every_option_it_declares() -> None:
    """The same check as above, across **all** platforms rather than the new
    three — this is the invariant, and the parametrised version above is only
    the part of it Phase 4 is answerable for.
    """
    from channels.adapters.zernio import PROVIDER_OPTION_FIELDS

    for platform, rule in PLATFORM_RULES.items():
        declared = {option.key for option in rule.options}
        mapped = set(PROVIDER_OPTION_FIELDS.get(platform, {}))
        assert declared == mapped, f"{platform} declares {declared} and maps {mapped}"
