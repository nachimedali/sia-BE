"""S17 T3 — multi-shot templates: a reel of clips under one hold and one debit.

A template whose metadata lists `shots` renders as a reel: each `frame` or
`photo` shot is a clip the video model animates, each `screen` shot is the
product's app screenshot set in as a crisp still (never through the video
model, which would blur it), every shot carries its kinetic line, and the
reel closes on an end card. Gate S17-11: whatever the number of shots, the
render holds once and debits once.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from ai.models import VideoRender, VideoRenderStatus
from ai.providers.base import CaptionCue, EndCard, ReelShot
from ai.providers.fake import _fake_video_composer, _fake_video_provider
from ai.services import ad_templates, video
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    KEYFRAME,
    RENDERS,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    make_product,
)
from billing.models import VideoLedger
from billing.services import ledger
from common.exceptions import ProviderError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    _fake_video_composer.clear()
    _fake_video_provider.calls.clear()
    yield
    _fake_video_composer.clear()


@pytest.fixture
def flat(growth: Any, make_png_upload: Any) -> Any:  # noqa: F811
    return make_product(growth, make_png_upload, kind="space", photos=3)


@pytest.fixture
def app(growth: Any, make_png_upload: Any) -> Any:  # noqa: F811
    product = make_product(growth, make_png_upload, kind="app", photos=2)
    screen = product.reference_images.order_by("id").last()
    product.reference_tags = {str(screen.pk): ad_templates.SCREEN_TAG}
    product.save(update_fields=["reference_tags"])
    return product


def body(product: Any, template: str, length: str = "clip-15") -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": length,
        "template": template,
        "product": product.pk,
        "language": "en",
    }


def test_shots_are_validated_on_save() -> None:
    meta = dict(next(t for t in ad_templates_seed() if t["key"] == "space-tour")["metadata"])
    ad_templates.validate_metadata(meta)
    for bad in ([{"use": "frame"}], [{"use": "drone"}, {"use": "photo"}], "photo"):
        with pytest.raises(DjangoValidationError):
            ad_templates.validate_metadata({**meta, "shots": bad})
    with pytest.raises(DjangoValidationError):  # a screen shot needs the screenshot
        ad_templates.validate_metadata(
            {**meta, "shots": [{"use": "screen"}, {"use": "photo"}], "needs": ""}
        )


def ad_templates_seed() -> list[dict[str, Any]]:
    from ai.ad_template_seed import TEMPLATES

    return TEMPLATES


def test_a_multi_shot_template_holds_once_and_debits_once(auth_client: Any, flat: Any) -> None:
    estimate = auth_client.post(ESTIMATE, body(flat, "space-tour"), format="json").json()
    assert estimate["units"] == 2 and estimate["keyframe_credits"] == 0  # no frame to draw
    before = ledger.video_balance(flat.workspace)

    render = auth_client.post(
        RENDERS, confirmed(auth_client, body(flat, "space-tour")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    assert ledger.video_balance(flat.workspace) == before - 2
    assert VideoLedger.objects.filter(workspace=flat.workspace, delta__lt=0).count() == 1
    [reel] = _fake_video_composer.calls
    assert [shot["clip"] for shot in reel["shots"]] == [True, True, True]  # a reel of clips
    assert len(_fake_video_provider.calls) == 3  # one animation per room
    assert reel["shots"][0]["overlay"] == "Step inside Olive oil 500 ml."  # kinetic line
    assert reel["end_card"] == {
        "title": "Olive oil 500 ml",
        "cta": "Book your visit.",
        "seconds": 2.0,
    }
    assert sum(shot["seconds"] for shot in reel["shots"]) + 2.0 == pytest.approx(15, abs=0.05)


def test_the_app_screen_is_set_in_crisp_never_animated(auth_client: Any, app: Any) -> None:
    render = auth_client.post(
        RENDERS, confirmed(auth_client, body(app, "app-walkthrough")), format="json"
    ).json()

    assert render["status"] == "SUCCEEDED", render
    [reel] = _fake_video_composer.calls
    # The first frame is animated; both screen shots are the tagged photo, as stills.
    assert [shot["clip"] for shot in reel["shots"]] == [True, False, False]
    assert len(_fake_video_provider.calls) == 1
    screen = app.reference_images.order_by("id").last()
    import hashlib

    with screen.file.open("rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()[:16]
    assert reel["shots"][1]["image"] == digest


def test_a_template_with_no_first_frame_refuses_a_keyframe(auth_client: Any, flat: Any) -> None:
    response = auth_client.post(
        KEYFRAME, {"template": "space-tour", "product": flat.pk}, format="json"
    )
    assert response.status_code == 400
    assert code_of(response) == "no_frame"
    gallery = auth_client.get(f"/api/v1/ai/ad-templates/?product={flat.pk}").json()
    tour = next(t for t in gallery["templates"] if t["key"] == "space-tour")
    assert tour["frame"] is False and tour["fit"] is None


def test_one_failed_shot_fails_the_reel_and_takes_nothing(
    auth_client: Any, flat: Any, monkeypatch: Any
) -> None:
    calls = {"n": 0}
    original = _fake_video_provider.animate

    def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProviderError("down")
        return original(**kwargs)

    monkeypatch.setattr(_fake_video_provider, "animate", flaky)
    before = ledger.video_balance(flat.workspace)
    render = auth_client.post(
        RENDERS, confirmed(auth_client, body(flat, "space-tour")), format="json"
    ).json()

    assert render["status"] == "FAILED" and render["error_code"] == "provider_error"
    assert ledger.video_balance(flat.workspace) == before
    assert video.held_units(flat.workspace) == 0
    assert not _fake_video_composer.calls
    assert VideoRender.objects.get(pk=render["id"]).status == VideoRenderStatus.FAILED


# -----------------------------------------------------------------------------
# The real composer: ffmpeg on this machine
# -----------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)


def _probe(content: bytes, tmp_path: Path) -> dict[str, Any]:
    import json

    path = tmp_path / "out.mp4"
    path.write_bytes(content)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return dict(json.loads(out.stdout))


def _clip(tmp_path: Path, seconds: int = 5) -> bytes:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=720x1280:rate=24:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@needs_ffmpeg
def test_ffmpeg_composes_stills_and_clips_with_kinetic_type_captions_and_an_end_card(
    tmp_path: Path, make_png_upload: Any, settings: Any
) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    settings.VIDEO_MUSIC_DIR = str(tmp_path)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=20",
            str(tmp_path / "oud.m4a"),
        ],
        check=True,
    )
    still = make_png_upload("s.png", (800, 600)).read()
    result = FfmpegVideoComposer().compose(
        shots=[
            ReelShot(image=still, seconds=3.0, overlay="Step inside: l'atelier"),
            ReelShot(image=still, seconds=3.0, clip=_clip(tmp_path)),
            ReelShot(image=still, seconds=3.0, overlay='100% "real"'),
        ],
        aspect="9:16",
        transition="crossfade",
        music="oud",
        captions=[CaptionCue("Hello there", 0.0, 4.5), CaptionCue("Book now", 4.5, 9.0)],
        end_card=EndCard(title="Atelier 7", call_to_action="Book your visit."),
        duration_seconds=11.0,
    )

    info = _probe(result.content, tmp_path)
    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(11.0, abs=0.2)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert result.provider == "ffmpeg" and result.duration_seconds == pytest.approx(11.0, abs=0.2)


@needs_ffmpeg
def test_a_music_bed_that_is_not_installed_fails_rather_than_ships_silent(
    tmp_path: Path, make_png_upload: Any, settings: Any
) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    settings.VIDEO_MUSIC_DIR = str(tmp_path)
    still = make_png_upload("s.png", (400, 400)).read()
    with pytest.raises(ProviderError):
        FfmpegVideoComposer().compose(
            shots=[ReelShot(image=still, seconds=2.0)],
            aspect="9:16",
            transition="cut",
            music="oud",
            captions=[],
            end_card=None,
            duration_seconds=2.0,
        )
    silent = FfmpegVideoComposer().compose(
        shots=[ReelShot(image=still, seconds=2.0)],
        aspect="1:1",
        transition="cut",
        music=None,
        captions=[],
        end_card=None,
        duration_seconds=2.0,
    )
    info = _probe(silent.content, tmp_path)
    assert not any(s["codec_type"] == "audio" for s in info["streams"])
    assert next(s for s in info["streams"] if s["codec_type"] == "video")["width"] == 1080
