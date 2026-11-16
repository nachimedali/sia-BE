"""S19-06 and S19-10 on the real ffmpeg composer, with synthetic clips.

The edits the video model is no longer asked for: a speed-up, a frame-rate
drop, two clips side by side, a step number; and the model's own sound kept
under the reel as one mixed stream. ffprobe confirms the length, the frame
rate, the layout and the audio.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from ai.providers.base import CaptionCue, ReelShot

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)


def _clip(tmp_path: Path, name: str, seconds: float, *, colour: str, sound: bool) -> bytes:
    path = tmp_path / f"{name}.mp4"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i"]
    args.append(f"color=c={colour}:size=720x1280:rate=24:duration={seconds}")
    if sound:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    subprocess.run([*args, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path.read_bytes()


def _probe(content: bytes, tmp_path: Path, name: str = "out") -> tuple[Path, dict[str, Any]]:
    path = tmp_path / f"{name}.mp4"
    path.write_bytes(content)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return path, json.loads(out.stdout)


def _pixel(path: Path, at: float, xy: tuple[int, int], tmp_path: Path) -> tuple[int, int, int]:
    frame = tmp_path / f"frame-{at}-{xy[0]}.png"
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


def _distinct_frames(path: Path, tmp_path: Path) -> int:
    """How many different pictures the first second holds (a 12 fps drop
    repeats frames; a smooth 30 fps clip of moving content does not)."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-t",
            "1",
            "-i",
            str(path),
            "-vf",
            "mpdecimate,showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stderr.count("pts_time")


@needs_ffmpeg
def test_a_clip_played_faster_keeps_the_paid_length(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    clip = _clip(tmp_path, "long", 12.5, colour="red", sound=True)
    result = FfmpegVideoComposer().caption(
        clip=clip, captions=[], aspect="9:16", speed=1.25, seconds=10
    )
    _, info = _probe(result.content, tmp_path)

    assert float(info["format"]["duration"]) == pytest.approx(10.0, abs=0.2)
    assert any(s["codec_type"] == "audio" for s in info["streams"])


@needs_ffmpeg
def test_a_frame_rate_drop_repeats_frames(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    source = tmp_path / "moving.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=720x1280:rate=30:duration=3",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    smooth = FfmpegVideoComposer().caption(clip=source.read_bytes(), captions=[], aspect="9:16")
    choppy = FfmpegVideoComposer().caption(
        clip=source.read_bytes(), captions=[], aspect="9:16", fps=12
    )
    smooth_path, smooth_info = _probe(smooth.content, tmp_path, "smooth")
    choppy_path, _ = _probe(choppy.content, tmp_path, "choppy")

    video = next(s for s in smooth_info["streams"] if s["codec_type"] == "video")
    assert video["r_frame_rate"] in ("30/1", "24/1")
    assert _distinct_frames(choppy_path, tmp_path) <= 13
    assert _distinct_frames(smooth_path, tmp_path) > 13


@needs_ffmpeg
def test_two_clips_side_by_side_play_in_turn(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    left = _clip(tmp_path, "left", 5, colour="red", sound=True)
    right = _clip(tmp_path, "right", 5, colour="blue", sound=False)
    blank = Image.new("RGB", (8, 8), "white")
    still = tmp_path / "still.png"
    blank.save(still)
    result = FfmpegVideoComposer().compose(
        shots=[ReelShot(image=still.read_bytes(), seconds=10, clip=left, right=right, badge="1")],
        aspect="9:16",
        transition="cut",
        music=None,
        captions=[],
        end_card=None,
        duration_seconds=10,
        keep_audio=True,
    )
    path, info = _probe(result.content, tmp_path)

    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(10.0, abs=0.2)
    assert sum(s["codec_type"] == "audio" for s in info["streams"]) == 1
    red_left = _pixel(path, 2.0, (270, 960), tmp_path)
    blue_right = _pixel(path, 7.0, (810, 960), tmp_path)
    assert red_left[0] > 150 and red_left[2] < 100
    assert blue_right[2] > 150 and blue_right[0] < 100


@needs_ffmpeg
def test_the_models_sound_runs_under_the_reel_as_one_stream(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    still = tmp_path / "still.png"
    Image.new("RGB", (8, 8), "white").save(still)
    shots = [
        ReelShot(
            image=still.read_bytes(),
            seconds=4,
            clip=_clip(tmp_path, "a", 5, colour="red", sound=True),
        ),
        ReelShot(image=still.read_bytes(), seconds=3),
        ReelShot(
            image=still.read_bytes(),
            seconds=3,
            clip=_clip(tmp_path, "b", 5, colour="green", sound=False),
        ),
    ]
    for transition in ("crossfade", "cut"):
        result = FfmpegVideoComposer().compose(
            shots=shots,
            aspect="9:16",
            transition=transition,
            music=None,
            captions=[CaptionCue("Hook", 0.0, 2.0)],
            end_card=None,
            duration_seconds=10,
            keep_audio=True,
        )
        _, info = _probe(result.content, tmp_path, transition)

        assert float(info["format"]["duration"]) == pytest.approx(10.0, abs=0.25), transition
        assert sum(s["codec_type"] == "audio" for s in info["streams"]) == 1, transition


@needs_ffmpeg
def test_without_keep_audio_a_reel_stays_silent(tmp_path: Path) -> None:
    from ai.providers.ffmpeg_composer import FfmpegVideoComposer

    still = tmp_path / "still.png"
    Image.new("RGB", (8, 8), "white").save(still)
    clip = _clip(tmp_path, "a", 5, colour="red", sound=True)
    result = FfmpegVideoComposer().compose(
        shots=[ReelShot(image=still.read_bytes(), seconds=5, clip=clip)],
        aspect="9:16",
        transition="cut",
        music=None,
        captions=[],
        end_card=None,
        duration_seconds=5,
    )
    _, info = _probe(result.content, tmp_path)

    assert not any(s["codec_type"] == "audio" for s in info["streams"])
