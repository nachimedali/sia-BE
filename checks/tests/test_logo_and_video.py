"""S4-01 logo detection and S4-03 video frame checks.

The logo detector and the ffmpeg reader are tested against real pictures and
real files made here — not against stand-ins — because what they claim ("the
logo is on the end card", "there is dead air at 0:02") is only worth anything if
a file actually shows it. The ffmpeg cases skip when `ffmpeg` is not installed;
the rest of the suite does not need it.
"""

from __future__ import annotations

import io
import random
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw

from checks import services
from checks.logo import find_logo
from checks.ports import VideoFacts, set_video_override
from checks.video import FfmpegVideo, VideoUnreadableError
from content.services.media import ingest_media
from content.services.posts import create_post

pytestmark = pytest.mark.django_db

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


# --- pictures ------------------------------------------------------------------------------
def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_logo() -> Image.Image:
    """A disc and two bars, on a transparent ground: a shape with a direction."""
    logo = Image.new("RGBA", (160, 80), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    draw.ellipse((6, 6, 70, 70), fill=(20, 40, 160, 255))
    draw.rectangle((84, 14, 150, 30), fill=(20, 40, 160, 255))
    draw.rectangle((84, 44, 128, 60), fill=(20, 40, 160, 255))
    return logo


def photo(seed: int = 1, size: tuple[int, int] = (1080, 1350)) -> Image.Image:
    """A busy, light-toned picture, so a dark logo has something to stand out from."""
    rng = random.Random(seed)
    image = Image.new("RGB", size, (214, 190, 150))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y, r = rng.randint(0, size[0]), rng.randint(0, size[1]), rng.randint(30, 200)
        draw.ellipse((x, y, x + r, y + r), fill=tuple(rng.randint(120, 255) for _ in range(3)))
    return image


def with_logo(seed: int, box: tuple[int, int], at: tuple[int, int]) -> Image.Image:
    image = photo(seed)
    logo = make_logo().resize(box)
    image.paste(logo, at, logo)
    return image


# --- the logo detector ---------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 4])
def test_a_logo_placed_on_the_picture_is_found_where_it_is(seed: int) -> None:
    match = find_logo(png(with_logo(seed, (260, 130), (780, 60))), png(make_logo()))

    assert match.state == "found"
    assert match.corner == "top right"


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_a_picture_without_the_logo_does_not_find_one(seed: int) -> None:
    assert find_logo(png(photo(seed)), png(make_logo())).state == "not_found"


def test_a_logo_squashed_out_of_its_proportions_is_reported_distorted() -> None:
    match = find_logo(png(with_logo(1, (300, 75), (700, 80))), png(make_logo()))

    assert match.state == "distorted"


def test_a_flat_block_of_colour_cannot_be_searched_for_and_says_so() -> None:
    flat = Image.new("RGBA", (80, 80), (20, 40, 160, 255))

    assert find_logo(png(photo()), png(flat)).state == "unsearchable"


# --- the logo in a post's checks -----------------------------------------------------------
def set_logo(workspace: Any) -> None:
    workspace.logo.save("logo.png", ContentFile(png(make_logo())), save=True)


def post_with_image(workspace: Any, user: Any, image: Image.Image) -> Any:
    asset = ingest_media(
        workspace=workspace,
        upload=SimpleUploadedFile("p.png", png(image), content_type="image/png"),
    )
    return create_post(
        workspace=workspace,
        author=user,
        master_body="Fresh from the kiln.",
        media_assets=[asset],
        planned_platforms=("instagram",),
    )


def statuses(run: Any, key: str) -> list[str]:
    return [r["status"] for r in run.results if r["key"] == key]


def test_a_post_with_the_logo_in_place_passes(workspace: Any, user: Any) -> None:
    set_logo(workspace)

    run = services.run_checks(post_with_image(workspace, user, with_logo(1, (260, 130), (780, 60))))

    assert statuses(run, "logo") == ["PASS"]


def test_a_post_without_the_logo_is_asked_to_look_and_not_told_it_is_missing(
    workspace: Any, user: Any
) -> None:
    set_logo(workspace)

    run = services.run_checks(post_with_image(workspace, user, photo(2)))

    [row] = [r for r in run.results if r["key"] == "logo"]
    assert row["status"] == "FIX"
    assert "could not find" in row["message"] and "not proof" in row["message"]


def test_a_stretched_logo_asks_for_it_to_be_placed_again(workspace: Any, user: Any) -> None:
    set_logo(workspace)

    run = services.run_checks(post_with_image(workspace, user, with_logo(1, (300, 75), (700, 80))))

    [row] = [r for r in run.results if r["key"] == "logo"]
    assert (row["status"], "stretched" in row["message"]) == ("FIX", True)


def test_a_plain_logo_is_reported_not_checked_never_passed(workspace: Any, user: Any) -> None:
    workspace.logo.save(
        "flat.png", ContentFile(png(Image.new("RGBA", (80, 80), (20, 40, 160, 255)))), save=True
    )

    run = services.run_checks(post_with_image(workspace, user, photo()))

    assert statuses(run, "logo") == ["UNAVAILABLE"]


def test_no_logo_on_the_workspace_means_no_logo_result(workspace: Any, user: Any) -> None:
    run = services.run_checks(post_with_image(workspace, user, photo()))

    assert statuses(run, "logo") == []


# --- the video facts: judged in code --------------------------------------------------------
class Scripted:
    def __init__(self, facts: VideoFacts) -> None:
        self.facts = facts

    def inspect(self, path: str) -> VideoFacts:
        return self.facts


def facts(**overrides: Any) -> VideoFacts:
    base: dict[str, Any] = {
        "duration": 8.0,
        "has_audio": True,
        "black": [],
        "silent": [],
        "first_luma": 0.5,
        "motion": 0.2,
        "last_frame": None,
        "provider": "scripted",
    }
    return VideoFacts(**{**base, **overrides})


@pytest.fixture(autouse=True)
def _clean_video() -> Iterator[None]:
    yield
    set_video_override(None)


def clip_post(workspace: Any, user: Any) -> Any:
    clip = ingest_media(
        workspace=workspace,
        upload=SimpleUploadedFile("a.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"),
    )
    return create_post(
        workspace=workspace,
        author=user,
        master_body="Fresh from the kiln.",
        media_assets=[clip],
        planned_platforms=("instagram",),
    )


def video_messages(run: Any) -> list[str]:
    return [r["message"] for r in run.results if r["key"] == "video" and r["status"] == "FIX"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"first_luma": 0.01, "motion": 0.0}, "no hook"),
        ({"motion": 0.002}, "Nothing moves in the first second"),
        ({"black": [(3.0, 5.5)]}, "Black frames from 0:03 to 0:05"),
        ({"silent": [(2.0, 6.0)]}, "Dead air from 0:02 to 0:06"),
    ],
)
def test_each_video_fault_is_named_with_its_time(
    workspace: Any, user: Any, overrides: dict[str, Any], expected: str
) -> None:
    set_video_override(Scripted(facts(**overrides)))

    run = services.run_checks(clip_post(workspace, user))

    assert any(expected in message for message in video_messages(run)), video_messages(run)


def test_short_silences_and_a_black_opening_second_are_not_extra_faults(
    workspace: Any, user: Any
) -> None:
    set_video_override(Scripted(facts(silent=[(2.0, 3.2)], black=[(0.0, 0.6)])))

    run = services.run_checks(clip_post(workspace, user))

    assert video_messages(run) == []


def test_silence_under_a_video_with_no_sound_track_is_not_dead_air(
    workspace: Any, user: Any
) -> None:
    set_video_override(Scripted(facts(has_audio=False, silent=[(0.0, 8.0)])))

    run = services.run_checks(clip_post(workspace, user))

    assert video_messages(run) == []
    assert any("no sound track" in r["message"] for r in run.results if r["key"] == "video")


def test_the_end_card_is_checked_for_the_logo(workspace: Any, user: Any) -> None:
    set_logo(workspace)
    card = png(with_logo(1, (260, 130), (780, 60)))
    set_video_override(Scripted(facts(last_frame=card)))
    assert statuses(services.run_checks(clip_post(workspace, user)), "logo") == ["PASS"]

    set_video_override(Scripted(facts(last_frame=png(photo(2)))))
    assert statuses(services.run_checks(clip_post(workspace, user)), "logo") == ["FIX"]


def test_an_end_card_that_could_not_be_read_is_not_checked_not_passed(
    workspace: Any, user: Any
) -> None:
    set_logo(workspace)
    set_video_override(Scripted(facts(last_frame=None)))

    run = services.run_checks(clip_post(workspace, user))

    assert statuses(run, "logo") == ["UNAVAILABLE"]


def test_with_no_reader_a_video_is_not_checked_never_passed(
    workspace: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "get_video", lambda: None)

    run = services.run_checks(clip_post(workspace, user))

    assert [r["status"] for r in run.results if r["key"] == "video"] == ["UNAVAILABLE"]


def test_a_reader_that_fails_is_reported_and_the_other_checks_still_run(
    workspace: Any, user: Any
) -> None:
    class Broken:
        def inspect(self, path: str) -> VideoFacts:
            raise VideoUnreadableError("no video stream")

    set_video_override(Broken())

    run = services.run_checks(clip_post(workspace, user))

    assert [r["status"] for r in run.results if r["key"] == "video"] == ["UNAVAILABLE"]
    assert statuses(run, "policy") == ["PASS"]


# --- real files through real ffmpeg ---------------------------------------------------------
def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=60)


@pytest.fixture
def clips(tmp_path: Path) -> dict[str, str]:
    """Small real mp4s, each with one thing wrong (or nothing)."""
    out = {name: str(tmp_path / f"{name}.mp4") for name in ("good", "black", "midblack", "deadair")}
    moving = "testsrc2=size=160x120:rate=10"
    tone = "sine=frequency=440:sample_rate=22050"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{moving}:duration=4",
        "-f",
        "lavfi",
        "-i",
        f"{tone}:duration=4",
        "-shortest",
        "-pix_fmt",
        "yuv420p",
        out["good"],
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=160x120:rate=10:duration=4",
        "-f",
        "lavfi",
        "-i",
        f"{tone}:duration=4",
        "-shortest",
        "-pix_fmt",
        "yuv420p",
        out["black"],
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{moving}:duration=2",
        "-f",
        "lavfi",
        "-i",
        "color=black:size=160x120:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        f"{moving}:duration=2",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-pix_fmt",
        "yuv420p",
        out["midblack"],
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{moving}:duration=5",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=22050:cl=mono",
        "-t",
        "5",
        "-shortest",
        "-pix_fmt",
        "yuv420p",
        out["deadair"],
    )
    return out


@needs_ffmpeg
def test_ffmpeg_reads_a_good_clip_as_moving_with_sound_and_no_gaps(clips: dict[str, str]) -> None:
    read = FfmpegVideo().inspect(clips["good"])

    assert read.provider == "ffmpeg"
    assert read.has_audio is True
    assert read.black == [] and read.silent == []
    assert read.motion > 0.015
    assert read.first_luma > 0.1
    assert read.last_frame and read.last_frame.startswith(b"\x89PNG")
    assert 3.5 < read.duration < 4.5


@needs_ffmpeg
def test_ffmpeg_sees_a_black_clip_as_black_with_no_motion(clips: dict[str, str]) -> None:
    read = FfmpegVideo().inspect(clips["black"])

    assert read.first_luma < 0.06
    assert read.motion < 0.015
    assert read.black and read.black[0][0] <= 0.05


@needs_ffmpeg
def test_ffmpeg_finds_black_in_the_middle_with_its_time(clips: dict[str, str]) -> None:
    read = FfmpegVideo().inspect(clips["midblack"])

    [(start, end)] = [pair for pair in read.black if pair[0] > 0.5]
    assert 1.8 <= start <= 2.2 and 3.8 <= end <= 4.2
    assert read.has_audio is False


@needs_ffmpeg
def test_ffmpeg_finds_silence_under_a_sound_track(clips: dict[str, str]) -> None:
    read = FfmpegVideo().inspect(clips["deadair"])

    assert read.has_audio is True
    assert read.silent and read.silent[0][1] - read.silent[0][0] >= 4.0


@needs_ffmpeg
def test_a_file_that_is_not_a_video_is_unreadable_not_a_pass(tmp_path: Path) -> None:
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not a video at all")

    with pytest.raises(VideoUnreadableError):
        FfmpegVideo().inspect(str(bad))


@needs_ffmpeg
def test_a_real_clip_through_a_post_names_the_dead_air(
    workspace: Any, user: Any, clips: dict[str, str]
) -> None:
    """End to end with no scripted reader: the stored file is measured."""
    set_video_override(FfmpegVideo())
    clip = ingest_media(
        workspace=workspace,
        upload=SimpleUploadedFile(
            "d.mp4", Path(clips["deadair"]).read_bytes(), content_type="video/mp4"
        ),
    )
    post = create_post(
        workspace=workspace,
        author=user,
        master_body="Fresh from the kiln.",
        media_assets=[clip],
        planned_platforms=("instagram",),
    )

    run = services.run_checks(post)

    assert any("Dead air" in message for message in video_messages(run)), run.results
