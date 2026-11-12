"""S17-31 — motion that lands (`studio_content_audit.md` §4.6, §6).

* **Beats on the clock.** A single-clip template's motion prompt carries its
  beats with their seconds (goose Seedance: one call, internal beats, drifts
  less than many short ones).
* **Landing.** A hero template is filmed *towards* its first frame: the clip
  ends on the approved still, so the last thing the viewer sees is the exact
  product (goose 3D showcase: the shared anchor frame). It renders on the
  `video_end` route.
* **The cutout.** A product photographed on a plain background is cut out of
  it for the end card; one that is not keeps its photo.
"""

from __future__ import annotations

import importlib
import io
from typing import Any, cast

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError as DjangoValidationError
from PIL import Image, ImageDraw

from ai.ad_template_seed import TEMPLATES
from ai.models import CreativeKind, CreativeOption, VideoRender
from ai.providers.fake import _fake_video_provider
from ai.services import ad_templates, graphics
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ai.migrations.0040_landing_templates")
LANDING = ("hero-orbit", "levitate", "liquid-splash")


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    _fake_video_provider.calls.clear()
    _fake_video_provider.requests.clear()
    yield


def _row(key: str) -> CreativeOption:
    return CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key=key)


# -----------------------------------------------------------------------------
# Beats on the clock
# -----------------------------------------------------------------------------
def test_the_motion_names_each_beat_with_its_seconds(catalog: None) -> None:  # noqa: F811
    row = _row("slow-unbox")  # Box on the table · Lid lifts · Product in light · End card

    timed = ad_templates.motion_prompt(row, seconds=9)

    assert "0-3 s: Box on the table" in timed
    assert "3-6 s: Lid lifts" in timed and "6-9 s: Product in light" in timed
    assert "End card" not in timed  # the composer's, never the video model's
    assert ad_templates.motion_prompt(row) == row.metadata["motion_prompt"]  # no length: as it was


def test_a_rendered_template_clip_sends_its_timed_beats(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "slow-unbox",
        "product": product.pk,
        "language": "en",
    }
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    assert "s: Lid lifts" in _fake_video_provider.calls[-1]


# -----------------------------------------------------------------------------
# Landing
# -----------------------------------------------------------------------------
def test_hero_templates_land_and_say_so_in_their_motion() -> None:
    for spec in TEMPLATES:
        meta = spec["metadata"]
        assert meta.get("land", False) is (spec["key"] in LANDING), spec["key"]
        if spec["key"] in LANDING:
            assert "exactly as in this frame" in meta["motion_prompt"], spec["key"]


def test_land_must_be_true_or_false(catalog: None) -> None:  # noqa: F811
    meta = dict(_row("levitate").metadata)
    with pytest.raises(DjangoValidationError):
        ad_templates.validate_metadata({**meta, "land": "yes"})


def test_a_landing_template_renders_on_the_last_frame_route(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-5",
        "template": "levitate",
        "product": product.pk,
        "language": "en",
    }
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    assert _fake_video_provider.requests[-1]["role"] == "end"
    assert VideoRender.objects.get(pk=render["id"]).spec["role"] == "end"


def test_the_migration_moves_untouched_hero_motions_only(settings: Any, catalog: None) -> None:  # noqa: F811
    settings.SEED_CATALOG_ON_MIGRATE = True
    orbit, splash = _row("hero-orbit"), _row("liquid-splash")
    orbit.metadata = {
        **{k: v for k, v in orbit.metadata.items() if k != "land"},
        "motion_prompt": MIGRATION.OLD_MOTION["hero-orbit"],
    }
    orbit.save(update_fields=["metadata"])
    splash.metadata = {
        **{k: v for k, v in splash.metadata.items() if k != "land"},
        "motion_prompt": "our own",
    }
    splash.save(update_fields=["metadata"])

    MIGRATION.forwards(apps, None)

    orbit.refresh_from_db()
    splash.refresh_from_db()
    new = next(t for t in TEMPLATES if t["key"] == "hero-orbit")["metadata"]
    assert orbit.metadata["motion_prompt"] == new["motion_prompt"] and orbit.metadata["land"]
    assert splash.metadata["motion_prompt"] == "our own" and "land" not in splash.metadata


# -----------------------------------------------------------------------------
# The cutout
# -----------------------------------------------------------------------------
def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_product_on_a_plain_background_is_cut_out() -> None:
    picture = Image.new("RGB", (400, 400), (250, 250, 250))
    ImageDraw.Draw(picture).ellipse((120, 80, 280, 330), fill=(180, 30, 30))

    cut = graphics.cutout(_png(picture))

    assert cut is not None
    with Image.open(io.BytesIO(cut)) as result:
        assert result.mode == "RGBA"
        width, height = result.size
        assert abs(width - 161) <= 2 and abs(height - 251) <= 2  # cropped to the product
        centre = cast(tuple[int, int, int, int], result.getpixel((width // 2, height // 2)))
        corner = cast(tuple[int, int, int, int], result.getpixel((0, 0)))
        assert centre[3] == 255  # the product stays
        assert corner[3] == 0  # the background around it is gone


def test_a_busy_photo_keeps_its_background() -> None:
    picture = Image.new("RGB", (300, 300))
    pixels = picture.load()
    assert pixels is not None
    for x in range(300):
        for y in range(300):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 5) % 256)

    assert graphics.cutout(_png(picture)) is None


def test_an_unreadable_picture_is_no_cutout() -> None:
    assert graphics.cutout(b"not an image") is None
