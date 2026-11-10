"""S17-33 — graphic formats (`studio_content_audit.md` §5.4).

Formats a brand pays for that need no person and no lip sync: an offer ad, a
value prop, a variant swipe, a mosaic reveal, myth vs fact, a chat reveal, the
brand's own shop tour and seasonal offers. Their cards, cutouts, grids and chat
bubbles are drawn in code (`ai.services.graphics`) — never by a model, which
invents letterforms — and cut by the composer like any reel.

An offer needs a real one: its words and the day it ends, and an end in the
past is refused (sales `scarcity-urgency-calibration`: urgency only when real).
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from PIL import Image, ImageDraw

from ai.ad_template_seed import TEMPLATES
from ai.models import CreativeOption, VideoRender
from ai.providers.fake import _fake_video_composer, _fake_video_provider
from ai.services import ad_templates, graphics
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    RENDERS,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    make_product,
)
from brand.models import BrandPlace, PlaceSource
from content.services.media import ingest_media

pytestmark = pytest.mark.django_db

NEW = {
    "offer-ad",
    "value-prop",
    "variant-swipe",
    "mosaic-reveal",
    "myth-fact",
    "chat-reveal",
    "shop-tour",
    "ramadan-offer",
    "eid-offer",
    "back-to-school",
    "summer-sale",
    "black-friday",
}


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    _fake_video_composer.clear()
    _fake_video_provider.calls.clear()
    yield
    _fake_video_composer.clear()


def png(
    size: tuple[int, int] = (400, 400), colour: tuple[int, int, int] = (250, 250, 250)
) -> bytes:
    picture = Image.new("RGB", size, colour)
    ImageDraw.Draw(picture).ellipse(
        (size[0] // 4, size[1] // 5, size[0] * 3 // 4, size[1] * 4 // 5), fill=(180, 30, 30)
    )
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def rgb(content: bytes, xy: tuple[int, int]) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(content)) as image:
        return cast(tuple[int, int, int], image.convert("RGB").getpixel(xy))


def body(product: Any, template: str, length: str = "clip-15", **extra: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": length,
        "template": template,
        "product": product.pk,
        "language": "en",
        **extra,
    }


def offer(days: int = 7) -> dict[str, str]:
    return {
        "text": "-20% on every jar",
        "ends": (timezone.localdate() + dt.timedelta(days=days)).isoformat(),
    }


# -----------------------------------------------------------------------------
# Drawn in code
# -----------------------------------------------------------------------------
def test_a_card_is_the_frame_size_in_the_brand_colour_with_its_words() -> None:
    card = graphics.card("Three things it does for you", background="#1F4E5A", kicker="FACT")
    with Image.open(io.BytesIO(card)) as image:
        assert image.size == (1080, 1920)
    assert rgb(card, (20, 20)) == (0x1F, 0x4E, 0x5A)
    plain = graphics.card("", background="#1F4E5A")
    assert card != plain  # the words are drawn


def test_a_long_card_shrinks_to_fit_and_a_struck_card_differs() -> None:
    long = graphics.card("word " * 60, background="#FFFFFF")
    assert Image.open(io.BytesIO(long)).size == (1080, 1920)
    assert graphics.card("Myth", background="#FFFFFF", strike=True) != graphics.card(
        "Myth", background="#FFFFFF"
    )


def test_a_cutout_field_puts_the_product_on_a_colour_field() -> None:
    field = graphics.cutout_field(png(), background="#F5E6C8")
    assert rgb(field, (20, 20)) == (0xF5, 0xE6, 0xC8)
    assert rgb(field, (540, 900))[0] > 150  # the product, centred


def test_a_mosaic_needs_two_photos_and_fills_the_frame() -> None:
    assert graphics.mosaic([png()], background="#FFFFFF") is None
    grid = graphics.mosaic([png(colour=(10 * i, 200, 200)) for i in range(4)], background="#FFFFFF")
    assert grid is not None and Image.open(io.BytesIO(grid)).size == (1080, 1920)


def test_a_chat_draws_both_sides() -> None:
    one = graphics.chat([("Is it any good?", False)], background="#FFFFFF", accent="#1F4E5A")
    two = graphics.chat(
        [("Is it any good?", False), ("Here's what it does.", True)],
        background="#FFFFFF",
        accent="#1F4E5A",
    )
    assert one != two


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_the_new_formats_are_seeded_as_reels_of_drawn_and_real_shots() -> None:
    keys = {spec["key"] for spec in TEMPLATES}
    assert keys >= NEW
    for spec in TEMPLATES:
        if spec["key"] in NEW:
            meta = spec["metadata"]
            ad_templates.validate_metadata(meta)
            assert meta["shots"] and meta["person"] == "none", spec["key"]


def test_offers_need_an_offer_and_the_others_their_material() -> None:
    needs = {spec["key"]: spec["metadata"].get("needs", "") for spec in TEMPLATES}
    for key in (
        "offer-ad",
        "ramadan-offer",
        "eid-offer",
        "back-to-school",
        "summer-sale",
        "black-friday",
    ):
        assert needs[key] == "offer", key
    assert needs["variant-swipe"] == "variants"
    assert needs["mosaic-reveal"] == "photos"
    assert needs["shop-tour"] == "places"


def test_shot_extras_are_held_to_their_types(catalog: None) -> None:  # noqa: F811
    meta = dict(CreativeOption.objects.get(kind="ad_template", key="myth-fact").metadata)
    for bad in (
        {"shots": [{"use": "card", "text": "x", "strike": "yes"}, {"use": "card", "text": "y"}]},
        {"shots": [{"use": "card", "text": "x", "kicker": 3}, {"use": "card", "text": "y"}]},
        {"transition": "spin"},
        {"needs": "a miracle"},
    ):
        with pytest.raises(DjangoValidationError):
            ad_templates.validate_metadata({**meta, **bad})


# -----------------------------------------------------------------------------
# Fit
# -----------------------------------------------------------------------------
def test_what_each_format_needs_is_said_in_the_gallery(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload, photos=2)
    rows = {
        row["key"]: row
        for row in auth_client.get(f"/api/v1/ai/ad-templates/?product={product.pk}").json()[
            "templates"
        ]
    }
    assert rows["variant-swipe"]["fit"] == "needs_variants"
    assert rows["mosaic-reveal"]["fit"] == "needs_photos"
    assert rows["shop-tour"]["fit"] == "needs_places"
    assert rows["offer-ad"]["fit"] is None  # the offer is asked for at render

    BrandPlace.objects.create(
        workspace=growth,
        label="Shop",
        source=PlaceSource.UPLOAD,
        media=ingest_media(workspace=growth, upload=make_png_upload("s.png", (500, 800))),
    )
    rows = {
        row["key"]: row
        for row in auth_client.get(f"/api/v1/ai/ad-templates/?product={product.pk}").json()[
            "templates"
        ]
    }
    assert rows["shop-tour"]["fit"] is None


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def test_an_offer_ad_needs_a_real_offer_that_has_not_ended(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    missing = auth_client.post(ESTIMATE, body(product, "offer-ad"), format="json")
    assert missing.status_code == 400 and code_of(missing) == "offer_required"
    ended = auth_client.post(
        ESTIMATE, body(product, "offer-ad", offer=offer(days=-1)), format="json"
    )
    assert ended.status_code == 400 and code_of(ended) == "offer_ended"


def test_an_offer_ad_is_cut_from_drawn_cards_and_the_product(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    request = body(product, "offer-ad", offer=offer())
    render = auth_client.post(RENDERS, confirmed(auth_client, request), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    [reel] = _fake_video_composer.calls
    uses = [shot["clip"] for shot in reel["shots"]]
    assert uses == [False, True, False, False]  # card · the product moving · card · card
    assert [shot["overlay"] for shot in reel["shots"]][::2] == ["", ""]  # card words are drawn
    assert VideoRender.objects.get(pk=render["id"]).spec["offer"] == offer()


def test_a_variant_swipe_slides_through_the_cut_out_variants(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload, photos=3)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, body(product, "variant-swipe", "clip-10")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    [reel] = _fake_video_composer.calls
    assert reel["transition"] == "slide"
    assert all(shot["clip"] is False for shot in reel["shots"])  # drawn stills, no model
    assert not _fake_video_provider.calls


def test_a_shop_tour_moves_through_the_brands_own_place(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    for index in range(2):
        BrandPlace.objects.create(
            workspace=growth,
            label=f"Shop {index}",
            source=PlaceSource.UPLOAD,
            media=ingest_media(
                workspace=growth, upload=make_png_upload(f"s{index}.png", (500, 800 + index))
            ),
        )
    render = auth_client.post(
        RENDERS, confirmed(auth_client, body(product, "shop-tour")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    [reel] = _fake_video_composer.calls
    assert [shot["clip"] for shot in reel["shots"]][:2] == [True, True]  # the places, moving


def test_a_value_prop_brief_asks_for_three_short_benefits(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    from ai.providers.fake import _fake_text_provider

    product = make_product(growth, make_png_upload)
    _fake_text_provider.calls.clear()
    ad_templates.write_script(growth, key="value-prop", product=product, language="en", seconds=15)
    assert "three benefits" in _fake_text_provider.calls[-1]["prompt"].lower()


def test_the_migration_seeds_the_new_formats_once(settings: Any) -> None:
    import importlib

    from django.apps import apps

    settings.SEED_CATALOG_ON_MIGRATE = True
    migration = importlib.import_module("ai.migrations.0042_graphic_templates")
    migration.forwards(apps, None)
    migration.forwards(apps, None)
    rows = CreativeOption.objects.filter(kind="ad_template", key__in=NEW)
    assert {row.key for row in rows} == NEW
    assert all(row.translations["fr"]["label"] for row in rows)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_slides_drawn_cards_into_one_reel(tmp_path: Any) -> None:
    import json
    import subprocess

    from ai.providers.base import ReelShot
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    cards = [graphics.card(text, background="#1F4E5A") for text in ("One", "Two", "Three")]
    result = FfmpegVideoComposer().compose(
        shots=[ReelShot(image=card, seconds=2.0) for card in cards],
        aspect="9:16",
        transition="slide",
        music=None,
        captions=[],
        end_card=None,
        duration_seconds=6.0,
    )
    path = tmp_path / "reel.mp4"
    path.write_bytes(result.content)
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )
    assert float(info["format"]["duration"]) == pytest.approx(6.0, abs=0.2)
