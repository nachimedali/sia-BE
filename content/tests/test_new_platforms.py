"""X, Pinterest and Google Business Profile (P4-01, P4-02, P4-03).

These three exist to **test Phase 1's abstraction**, which is why they are
worth their own file. Each is a row in `rules.py`, a row in the adapter's
provider-field map, and nothing else. `test_phase4_gates.py` is what asserts
that "nothing else" stays true; this file asserts the rows say the right
things.
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import MediaKind, Platform, PostFormat
from content.services.adaptation import adapt_for_platform
from content.services.rules import PLATFORM_RULES, format_rule, options_for

pytestmark = pytest.mark.django_db


class _Asset:
    def __init__(self, asset_id: int, kind: str = MediaKind.IMAGE) -> None:
        self.id = asset_id
        self.kind = kind
        self.file = None


class TestX:
    def test_x_is_declared(self) -> None:
        assert Platform.X in PLATFORM_RULES

    def test_the_caption_limit_is_280(self) -> None:
        assert PLATFORM_RULES[Platform.X].char_limit == 280

    def test_a_feed_post_takes_four_media(self) -> None:
        spec = format_rule(Platform.X, PostFormat.FEED)

        assert spec is not None
        assert spec.max_media == 4

    def test_a_long_body_becomes_a_thread_rather_than_being_truncated(self) -> None:
        """**Reusing the splitter built for Threads** (P4-01), not a second
        one. The engine reaches it through `supports_thread`, so X gets
        threading by declaring `True` — no X-shaped code anywhere."""
        body = "word " * 200

        payload = adapt_for_platform(master_body=body, media_assets=[], platform=Platform.X)

        assert payload.thread, "X declared supports_thread but produced no thread"
        assert not payload.truncated
        assert all(len(chunk) <= 280 for chunk in payload.thread)

    def test_every_thread_chunk_is_numbered(self) -> None:
        payload = adapt_for_platform(
            master_body="word " * 200, media_assets=[], platform=Platform.X
        )

        total = len(payload.thread)
        assert payload.thread[0].endswith(f"(1/{total})")
        assert payload.thread[-1].endswith(f"({total}/{total})")

    def test_a_fifth_image_is_dropped(self) -> None:
        payload = adapt_for_platform(
            master_body="Hi",
            media_assets=[_Asset(index) for index in range(1, 6)],
            platform=Platform.X,
        )

        assert len(payload.media) == 4


class TestPinterest:
    def test_pinterest_is_declared(self) -> None:
        assert Platform.PINTEREST in PLATFORM_RULES

    def test_a_pin_needs_an_image(self) -> None:
        # A pin with no image is not a pin — Pinterest is the one platform
        # where the media is the post and the text is the caption.
        spec = format_rule(Platform.PINTEREST, PostFormat.FEED)

        assert spec is not None
        assert spec.min_media == 1
        assert spec.max_media == 1

    def test_the_board_is_a_required_option(self) -> None:
        board = options_for(Platform.PINTEREST)["board_id"]

        assert board.required

    def test_the_board_is_chosen_from_the_account_not_typed(self) -> None:
        """A free-text board id is a rejected pin. `remote_choice` is what
        tells the composer to fetch the list instead of drawing a text box,
        and `source` says which list (P4-02's "new provider read")."""
        board = options_for(Platform.PINTEREST)["board_id"]

        assert board.kind == "remote_choice"
        assert board.source == "pinterest_boards"

    def test_a_pin_carries_a_destination_link(self) -> None:
        assert options_for(Platform.PINTEREST)["destination_link"].kind == "url"

    def test_a_pin_carries_its_own_title(self) -> None:
        title = options_for(Platform.PINTEREST)["title"]

        assert title.kind == "str"
        assert title.max_length == 100


class TestGoogleBusinessProfile:
    def test_it_is_declared(self) -> None:
        assert Platform.GOOGLE_BUSINESS in PLATFORM_RULES

    def test_the_post_type_is_a_closed_choice(self) -> None:
        """Offers and events are **post types, not formats** (P4-03). The
        format enum describes shape — feed, story, reel — and an offer is a
        feed post with extra fields, so bending `PostFormat` around it would
        give every other platform two values it can never use."""
        post_type = options_for(Platform.GOOGLE_BUSINESS)["post_type"]

        assert post_type.kind == "choice"
        assert set(post_type.choices) == {"STANDARD", "OFFER", "EVENT"}
        assert post_type.default == "STANDARD"

    def test_the_call_to_action_is_a_closed_choice(self) -> None:
        cta = options_for(Platform.GOOGLE_BUSINESS)["cta_type"]

        assert cta.kind == "choice"
        assert "LEARN_MORE" in cta.choices

    def test_an_offer_carries_its_coupon_and_terms(self) -> None:
        declared = options_for(Platform.GOOGLE_BUSINESS)

        assert declared["offer_coupon_code"].kind == "str"
        assert declared["offer_terms"].kind == "str"
        assert declared["offer_redeem_url"].kind == "url"

    def test_an_event_carries_a_window(self) -> None:
        declared = options_for(Platform.GOOGLE_BUSINESS)

        assert declared["event_start"].kind == "str"
        assert declared["event_end"].kind == "str"

    def test_it_is_the_richest_declaration_we_carry(self) -> None:
        """The deliberate stress test (P4-03). If the richest options schema in
        the product still costs one table row, the abstraction held."""
        counts = {name: len(rule.options) for name, rule in PLATFORM_RULES.items()}

        assert counts[Platform.GOOGLE_BUSINESS] == max(counts.values())


class TestTheyRenderLikeEverythingElse:
    @pytest.mark.parametrize("platform", [Platform.X, Platform.PINTEREST, Platform.GOOGLE_BUSINESS])
    def test_a_new_platform_renders_through_the_one_engine(self, platform: str) -> None:
        payload = adapt_for_platform(
            master_body="Hello #launch", media_assets=[_Asset(1)], platform=platform
        )

        assert payload.platform == platform
        assert payload.hashtags == ["launch"]

    @pytest.mark.parametrize("platform", [Platform.X, Platform.PINTEREST, Platform.GOOGLE_BUSINESS])
    def test_a_new_platform_is_served_to_the_composer(
        self, platform: str, auth_client: Any, workspace: Any
    ) -> None:
        body = auth_client.get("/api/v1/platform-rules/").json()

        assert platform in {row["platform"] for row in body["platforms"]}
