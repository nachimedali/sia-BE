"""S17-26 — the finish of a template ad (`studio_content_audit.md` §8, P0).

What a template sends and what comes back, beyond the choices themselves:

* every template names its **capture** (phone, studio, macro, screen), and its
  first frame is asked for in that capture's words — a studio hero is never
  told to look like an unpolished phone video;
* the **negative** is sent — to the image model as a keep-out rule the brief's
  rewrite cannot drop, and at the end of every motion prompt;
* a render carries a **seed**, stored and sent with every shot of it;
* no seeded hook or script puts an invented experience, a dead opener or false
  urgency in an invented person's mouth;
* the product's **identity photos** go first: front, packaging, side;
* a single-clip template gets its hook, body and call to action **burned in
  on their beats**, and a reel closes on a **brand end card** — the kit's
  colours, the real logo, the product.
"""

from __future__ import annotations

import io
import itertools
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from ai.ad_template_seed import CAPTURES, TEMPLATES
from ai.models import Generation, VideoRender
from ai.providers.base import CaptionCue, EndCard, ReelShot
from ai.providers.fake import _fake_video_composer, _fake_video_provider
from ai.services import ad_templates, rephrase, video
from ai.services.pipeline import _reference_image_bytes
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    KEYFRAME,
    RENDERS,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)
from brand.models import BrandCore
from common.exceptions import ProviderError
from products.brief import identity_photos

pytestmark = pytest.mark.django_db

PHONE = "phone_ugc"


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    _fake_video_composer.clear()
    _fake_video_provider.calls.clear()
    _fake_video_provider.requests.clear()
    yield
    _fake_video_composer.clear()


def clip_body(product: Any, template: str, length: str = "clip-10", **extra: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": length,
        "template": template,
        "product": product.pk,
        "language": "en",
        **extra,
    }


def png(size: tuple[int, int], color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def brand_kit(workspace: Any, *colors: tuple[str, str]) -> None:
    BrandCore.objects.create(
        workspace=workspace,
        version=1,
        sections={
            "palette": {
                "value": {"colors": [{"role": r, "hex": h} for r, h in colors], "fonts": {}},
                "origin": "site",
            }
        },
    )


def with_logo(workspace: Any, size: tuple[int, int]) -> None:
    workspace.logo = SimpleUploadedFile("logo.png", png(size), content_type="image/png")
    workspace.save(update_fields=["logo"])


# -----------------------------------------------------------------------------
# The seed: capture, negative, words
# -----------------------------------------------------------------------------
def test_every_template_names_a_capture_and_is_asked_for_in_its_words() -> None:
    for spec in TEMPLATES:
        meta = spec["metadata"]
        capture = meta["capture"]
        assert capture in CAPTURES, spec["key"]
        assert meta["keyframe_prompt"].endswith(CAPTURES[capture]["finish"]), spec["key"]
        assert CAPTURES[capture]["avoid"] in meta["negative"], spec["key"]
        if capture != PHONE:
            assert "unpolished" not in meta["keyframe_prompt"].lower(), spec["key"]


def test_studio_and_macro_templates_are_not_phone_videos() -> None:
    capture = {spec["key"]: spec["metadata"]["capture"] for spec in TEMPLATES}
    assert capture["levitate"] == capture["hero-orbit"] == capture["offer-drop"] == "studio_hero"
    assert capture["macro-sizzle"] == capture["close-detail"] == "macro_tabletop"
    assert capture["tap-through"] == "device_screen"
    assert capture["market-stroll"] == capture["first-look"] == PHONE


def test_an_unknown_capture_is_refused_on_save() -> None:
    meta = dict(TEMPLATES[0]["metadata"])
    ad_templates.validate_metadata(meta)
    ad_templates.validate_metadata({k: v for k, v in meta.items() if k != "capture"})  # optional
    with pytest.raises(DjangoValidationError):
        ad_templates.validate_metadata({**meta, "capture": "drone"})


INVENTED = (
    "saved me",
    "wasn't expecting",
    "did not expect",
    "about to give up",
    "gets me through",
    "used to hate",
    "as a professional",
    "i looked into",
    "what i found",
    "i had to find out",
    "a customer wrote",
    "everywhere i go",
    "never leaving home",
    "find of the week",
    "what i came for",
    "on my counter",
    "how i get through",
)
URGENCY = ("this week only", "today only", "last chance", "limited time", "hurry", "only today")
CLAIMS = ("see the difference", "no filter", "built to last", "made better")


def _lines(spec: dict[str, Any]) -> list[str]:
    meta = spec["metadata"]
    shots = [shot["text"] for shot in meta.get("shots", [])]
    return [*meta["hooks"], *meta["script"].values(), *shots]


def test_no_seeded_line_is_an_invented_testimony_false_urgency_or_an_unbacked_claim() -> None:
    for spec in TEMPLATES:
        for line in _lines(spec):
            low = line.lower()
            for phrase in (*INVENTED, *URGENCY, *CLAIMS):
                assert phrase not in low, (spec["key"], line)


def test_no_seeded_hook_opens_with_a_dead_opener() -> None:
    for spec in TEMPLATES:
        meta = spec["metadata"]
        for hook in (*meta["hooks"], meta["script"]["hook"]):
            low = hook.lower()
            assert not low.startswith(ad_templates.DEAD_OPENERS), (spec["key"], hook)
            assert len(hook.split()) <= ad_templates.HOOK_MAX_WORDS, (spec["key"], hook)


def test_the_script_writer_is_told_the_same_two_rules() -> None:
    system = ad_templates.SCRIPT_SYSTEM.lower()
    assert "personal experience" in system and "urgency" in system


# -----------------------------------------------------------------------------
# What reaches the models
# -----------------------------------------------------------------------------
def test_the_keyframe_carries_the_capture_and_a_keep_out_rule(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    response = auth_client.post(
        KEYFRAME, {"template": "levitate", "product": product.pk}, format="json"
    )
    assert response.status_code in (200, 201, 202), response.json()

    generation = Generation.objects.filter(workspace=growth).latest("id")
    negative = CAPTURES["studio_hero"]["avoid"]
    assert negative in generation.creative["negative"]
    sent = generation.prompt_sent
    assert CAPTURES["studio_hero"]["finish"] in sent
    assert "unpolished" not in sent.lower()
    assert f"Avoid: {generation.creative['negative']}." in sent


def test_the_motion_prompt_ends_on_the_negative_even_after_a_rewrite(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
    monkeypatch: Any,
) -> None:
    from billing.models import FeatureFlag
    from billing.services.flags import STUDIO_REPHRASE

    FeatureFlag.objects.create(organization=None, key=STUDIO_REPHRASE, enabled=True)
    monkeypatch.setattr(
        rephrase,
        "motion_brief",
        lambda **_: rephrase.Rephrased(text="a rewritten motion", by="model"),
    )
    product = make_product(growth, make_png_upload)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, clip_body(product, "levitate")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    sent = _fake_video_provider.calls[-1]
    row = ad_templates.resolve(growth, "levitate", product=product, with_person=False)
    assert sent.startswith("a rewritten motion")
    assert sent.endswith(ad_templates.avoid_line(row, ad_templates.place_forbid(row, "")))
    assert VideoRender.objects.get(pk=render["id"]).prompt_sent == sent


def test_a_render_keeps_one_seed_and_sends_it_with_every_shot(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload, kind="space", photos=3)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, clip_body(product, "space-tour", "clip-15")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    seed = VideoRender.objects.get(pk=render["id"]).seed
    assert isinstance(seed, int) and 0 <= seed < 2**31
    assert [r["seed"] for r in _fake_video_provider.requests] == [seed, seed, seed]


def test_two_renders_get_their_own_seeds(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    for _ in range(2):
        auth_client.post(
            RENDERS, confirmed(auth_client, clip_body(product, "levitate")), format="json"
        )
    first, second = VideoRender.objects.filter(workspace=growth).order_by("id")
    assert first.seed is not None and second.seed is not None and first.seed != second.seed


@pytest.mark.parametrize(
    ("tags", "kind", "expected"),
    [
        ({1: "lifestyle", 3: "front"}, "physical", [3, 0, 2, 1]),
        ({0: "in-use", 2: "packaging", 3: "side-back"}, "physical", [2, 3, 1, 0]),
        ({}, "physical", [0, 1, 2, 3]),  # untagged keeps the upload order
        ({0: "front", 3: "screen"}, "app", [3, 0, 1, 2]),  # an app's identity is its screen
        ({0: "screen", 1: "front"}, "physical", [1, 2, 3, 0]),
    ],
)
def test_identity_photos_come_first(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
    tags: dict[int, str],
    kind: str,
    expected: list[int],
) -> None:
    product = make_product(growth, make_png_upload, kind=kind, photos=4)
    photos = list(product.reference_images.order_by("pk"))
    product.reference_tags = {str(photos[i].pk): tag for i, tag in tags.items()}
    product.save(update_fields=["reference_tags"])

    assert [p.pk for p in identity_photos(product)] == [photos[i].pk for i in expected]


def test_a_capped_reference_set_keeps_the_front_photo(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload, photos=3)
    photos = list(product.reference_images.order_by("pk"))
    product.reference_tags = {str(photos[2].pk): "front"}
    product.save(update_fields=["reference_tags"])
    with photos[2].file.open("rb") as handle:
        front = handle.read()

    assert _reference_image_bytes(product, cap=1) == [front]


# -----------------------------------------------------------------------------
# The finish: beats burned in, the brand end card
# -----------------------------------------------------------------------------
def test_beat_cues_put_the_hook_first_and_the_call_to_action_last() -> None:
    script = {"hook": "Wait for it.", "body": "Unboxed slowly.", "cta": "Order today."}

    cues = video.beat_cues(script, 10.0)

    assert [cue.text for cue in cues] == ["Wait for it.", "Unboxed slowly.", "Order today."]
    assert cues[0].start == 0.0 and cues[-1].end == 10.0
    assert all(a.end == b.start for a, b in itertools.pairwise(cues))
    assert cues[0].end <= 3.0  # the hook is the opening, not a third of a long ad
    assert [cue.text for cue in video.beat_cues({**script, "body": ""}, 5.0)] == [
        "Wait for it.",
        "Order today.",
    ]
    assert video.beat_cues({"hook": "", "body": "", "cta": ""}, 5.0) == []


def test_a_single_clip_template_gets_its_words_burned_in_on_the_beats(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, clip_body(product, "levitate")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    [captioned] = _fake_video_composer.calls
    assert captioned["kind"] == "caption"
    script = VideoRender.objects.get(pk=render["id"]).spec["script"]
    assert [cue[0] for cue in captioned["captions"]] == [
        script["hook"],
        script["body"],
        script["cta"],
    ]
    stored = VideoRender.objects.get(pk=render["id"])
    assert stored.provider == "fake"  # the animation's vendor, not the burn-in's


def test_a_failed_burn_in_still_delivers_the_clip(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
    monkeypatch: Any,
) -> None:
    def broken(**_: Any) -> Any:
        raise ProviderError("ffmpeg fell over")

    monkeypatch.setattr(_fake_video_composer, "caption", broken)
    product = make_product(growth, make_png_upload)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, clip_body(product, "levitate")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render  # the paid animation is not thrown away


def test_the_reel_end_card_wears_the_brand(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    brand_kit(growth, ("Primary", "#1F4E5A"), ("Accent", "#C2A03B"))
    with_logo(growth, (600, 200))
    product = make_product(growth, make_png_upload, kind="space", photos=3)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, clip_body(product, "space-tour", "clip-15")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    [reel] = _fake_video_composer.calls
    card = reel["end_card"]
    assert (card["background"], card["accent"]) == ("#1F4E5A", "#C2A03B")
    assert card["logo"] is True and card["image"] is True


def test_no_kit_and_a_favicon_logo_give_the_plain_card_without_the_logo(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    with_logo(growth, (64, 64))  # favicon-sized: never stretched onto a card
    product = make_product(growth, make_png_upload)

    card = video.brand_end_card(growth, product, title="Oil", call_to_action="Shop")

    assert card.background == video.END_CARD_BACKGROUND
    assert card.logo is None and card.image is not None


def test_a_colour_only_kit_still_gives_a_readable_accent(growth: Any) -> None:  # noqa: F811
    brand_kit(growth, ("Primary", "#FAFAFA"))

    card = video.brand_end_card(growth, None, title="Oil", call_to_action="")

    assert card.background == "#FAFAFA" and card.accent and card.accent != card.background
    assert card.image is None


# -----------------------------------------------------------------------------
# The real composer
# -----------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)


def _probe(path: Path) -> dict[str, Any]:
    import json

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return dict(json.loads(out.stdout))


def _pixel(path: Path, at: float, xy: tuple[int, int], tmp_path: Path) -> tuple[int, int, int]:
    frame = tmp_path / f"frame-{at}.png"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(at),
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )
    with Image.open(frame) as image:
        r, g, b = image.convert("RGB").getpixel(xy)
    return (r, g, b)


@needs_ffmpeg
def test_ffmpeg_draws_the_brand_end_card(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    result = FfmpegVideoComposer().compose(
        shots=[ReelShot(image=png((800, 600), (20, 20, 20)), seconds=2.0, overlay="Hello")],
        aspect="9:16",
        transition="cut",
        music=None,
        captions=[],
        end_card=EndCard(
            title="Atelier 7",
            call_to_action="Book your visit.",
            seconds=2.0,
            background="#1F4E5A",
            accent="#C2A03B",
            logo=png((600, 200), (250, 250, 250)),
            image=png((600, 600), (200, 40, 40)),
        ),
        duration_seconds=4.0,
    )
    path = tmp_path / "reel.mp4"
    path.write_bytes(result.content)

    info = _probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(4.0, abs=0.2)
    corner = _pixel(path, 3.5, (40, 1800), tmp_path)  # the card's background, bottom-left
    assert all(abs(a - b) < 12 for a, b in zip(corner, (0x1F, 0x4E, 0x5A), strict=True)), corner
    middle = _pixel(path, 3.5, (540, 860), tmp_path)  # the product, centred above the title
    assert middle[0] > 150 and middle[1] < 90, middle


@needs_ffmpeg
def test_ffmpeg_burns_captions_into_a_clip_and_keeps_its_length_and_sound(
    tmp_path: Path,
) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=720x1280:rate=24:duration=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )

    result = FfmpegVideoComposer().caption(
        clip=source.read_bytes(),
        captions=[CaptionCue("Wait for it.", 0.0, 1.7), CaptionCue("Order today.", 3.3, 5.0)],
        aspect="9:16",
    )
    path = tmp_path / "out.mp4"
    path.write_bytes(result.content)

    info = _probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(5.0, abs=0.2)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
