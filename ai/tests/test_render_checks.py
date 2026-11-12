"""S17-35 — checks measured on every finished video (`studio_content_audit.md` §6, P18).

What a machine can decide about a clip, on this machine's ffmpeg, free:

* `black` — a black stretch longer than 0.3 s;
* `frozen` — the picture still for more than 2.5 s before the end card;
* `still_opening` — the first frame held for more than 1.5 s (the hook must move);
* `silent_opening` — no sound in the first second, when the clip has sound at all;
* `likeness` — the product on the last frame before the end card, compared with
  its photos (the same perceptual hash the image gate uses).

They are **advisory**: shown at review, never a refusal, because their
thresholds are goose-skills' and have not been tuned on our renders yet. A
file that cannot be read gives no checks rather than a wrong one.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from ai.models import VideoRender
from ai.services import render_checks
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)


def _video(tmp_path: Path, source: list[str], seconds: float, *, sound: bool = True) -> bytes:
    path = tmp_path / f"v{len(list(tmp_path.iterdir()))}.mp4"
    audio = ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"] if sound else []
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *source,
            *audio,
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            *(["-shortest"] if sound else []),
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _png(colour: tuple[int, int, int], pattern: bool = True) -> bytes:
    """A left-to-right gradient, or its mirror: opposite perceptual hashes."""
    image = Image.new("RGB", (360, 640), colour)
    pixels = image.load()
    assert pixels is not None
    for x in range(360):
        level = int(255 * x / 359) if pattern else int(255 * (359 - x) / 359)
        for y in range(640):
            pixels[x, y] = (level, level // 2, colour[2])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def codes(found: list[dict[str, Any]]) -> dict[str, bool]:
    return {check["code"]: check["ok"] for check in found}


@needs_ffmpeg
def test_a_moving_clip_with_sound_passes_every_check(tmp_path: Path) -> None:
    clip = _video(tmp_path, ["-f", "lavfi", "-i", "testsrc=size=360x640:rate=24"], 5)
    found = codes(render_checks.inspect(clip, end_card_seconds=0, product_photos=[]))
    assert found == {"black": True, "frozen": True, "still_opening": True, "silent_opening": True}


@needs_ffmpeg
def test_black_frozen_and_silent_are_found(tmp_path: Path) -> None:
    black = _video(tmp_path, ["-f", "lavfi", "-i", "color=c=black:s=360x640:r=24"], 4, sound=False)
    found = codes(render_checks.inspect(black, end_card_seconds=0, product_photos=[]))
    assert found["black"] is False and found["frozen"] is False and found["still_opening"] is False
    assert "silent_opening" not in found  # no sound track at all: nothing to say about sound

    late_path = tmp_path / "late.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=360x640:rate=24:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=4",
            "-af",
            "adelay=1500|1500",
            "-t",
            "4",
            "-pix_fmt",
            "yuv420p",
            str(late_path),
        ],
        check=True,
    )
    late = late_path.read_bytes()
    assert (
        codes(render_checks.inspect(late, end_card_seconds=0, product_photos=[]))["silent_opening"]
        is False
    )


@needs_ffmpeg
def test_a_held_end_card_is_not_a_freeze(tmp_path: Path) -> None:
    joined = tmp_path / "joined.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=360x640:rate=24:duration=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x1E505A:s=360x640:r=24:d=3",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(joined),
        ],
        check=True,
    )
    content = joined.read_bytes()
    assert codes(render_checks.inspect(content, end_card_seconds=3, product_photos=[]))["frozen"]
    assert not codes(render_checks.inspect(content, end_card_seconds=0, product_photos=[]))[
        "frozen"
    ]


@needs_ffmpeg
def test_the_last_frame_is_compared_with_the_products_photos(tmp_path: Path) -> None:
    product = _png((200, 40, 40))
    other = _png((20, 200, 40), pattern=False)
    still = tmp_path / "product.png"
    still.write_bytes(product)
    clip = _video(tmp_path, ["-loop", "1", "-i", str(still)], 3)

    same = render_checks.inspect(clip, end_card_seconds=0, product_photos=[product])
    likeness = next(check for check in same if check["code"] == "likeness")
    assert likeness["ok"] and likeness["score"] >= 0.8
    moved = render_checks.inspect(clip, end_card_seconds=0, product_photos=[other])
    assert next(check for check in moved if check["code"] == "likeness")["ok"] is False


def test_an_unreadable_file_gives_no_checks() -> None:
    assert render_checks.inspect(b"not a video", end_card_seconds=0, product_photos=[]) == []


def test_a_render_stores_its_checks_and_serves_them(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        render_checks,
        "inspect",
        lambda content, **kwargs: [
            {"code": "frozen", "ok": False, "detail": "3.0 s still at 2.0 s"}
        ],
    )
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "levitate",
        "product": product.pk,
        "language": "en",
    }
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    [check] = render["checks"]
    assert (check["code"], check["ok"], check["detail"]) == (
        "frozen",
        False,
        "3.0 s still at 2.0 s",
    )
    assert VideoRender.objects.get(pk=render["id"]).checks[0]["code"] == "frozen"
