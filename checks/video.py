"""Reading a video's frames and sound with `ffmpeg` (steps-plan S4-03).

Nothing is sent anywhere: the file is read from storage to a temporary path and
`ffmpeg`/`ffprobe` measure it on this machine. Four things come back — black
stretches, silent stretches, how the first second moves, and the last frame —
and `checks.services` decides what they mean. A file the tools cannot read
raises `VideoUnreadableError`, which the caller reports as *Not checked*.
"""

from __future__ import annotations

import io
import json
import re
import subprocess

from PIL import Image, ImageChops, ImageStat

from checks.ports import VideoFacts

#: A stretch is black when at least this share of its pixels is below
#: `BLACK_PIXEL` and it lasts at least this long (seconds).
BLACK_MIN_SECONDS = 0.4
BLACK_PIXEL = 0.10
#: Quiet is below this level for at least this long (seconds).
SILENCE_DB = -45
SILENCE_MIN_SECONDS = 1.0
TIMEOUT_SECONDS = 60
#: Frames are read at this width: they only feed a brightness mean, a 64-pixel
#: motion comparison and an 80-pixel logo search, so full resolution is waste.
FRAME_WIDTH = 320


class VideoUnreadableError(RuntimeError):
    pass


def _run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(args, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VideoUnreadableError(str(error)) from error


def _intervals(log: str, start: str, end: str) -> list[tuple[float, float]]:
    starts = [float(m) for m in re.findall(rf"{start}:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(rf"{end}:\s*([0-9.]+)", log)]
    # A stretch still open when the file ends has a start and no end.
    return [(a, ends[i] if i < len(ends) else a) for i, a in enumerate(starts)]


def _frame(path: str, at: float | None, *, from_end: float | None = None) -> bytes | None:
    seek = ["-sseof", f"-{from_end}"] if from_end is not None else ["-ss", f"{at or 0}"]
    done = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            *seek,
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_WIDTH}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
    )
    return done.stdout if done.returncode == 0 and done.stdout else None


def _luma(png: bytes) -> float:
    with Image.open(io.BytesIO(png)) as image:
        return float(ImageStat.Stat(image.convert("L")).mean[0]) / 255


def _motion(first: bytes, later: bytes) -> float:
    with Image.open(io.BytesIO(first)) as a, Image.open(io.BytesIO(later)) as b:
        size = (64, 64)
        diff = ImageChops.difference(a.convert("L").resize(size), b.convert("L").resize(size))
        return float(ImageStat.Stat(diff).mean[0]) / 255


class FfmpegVideo:
    def inspect(self, path: str) -> VideoFacts:
        probe = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ]
        )
        if probe.returncode != 0:
            raise VideoUnreadableError("ffprobe could not read the file")
        info = json.loads(probe.stdout or b"{}")
        streams = info.get("streams", [])
        if not any(s.get("codec_type") == "video" for s in streams):
            raise VideoUnreadableError("no video stream")
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(info.get("format", {}).get("duration") or 0.0)

        filters = [
            "-vf",
            f"blackdetect=d={BLACK_MIN_SECONDS}:pix_th={BLACK_PIXEL}",
        ]
        if has_audio:
            filters += ["-af", f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN_SECONDS}"]
        scan = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, *filters, "-f", "null", "-"])
        if scan.returncode != 0:
            raise VideoUnreadableError("ffmpeg could not decode the file")
        log = scan.stderr.decode("utf-8", "replace")

        first = _frame(path, 0)
        later = _frame(path, min(0.9, max(duration - 0.1, 0.0)))
        if first is None:
            raise VideoUnreadableError("no first frame")
        return VideoFacts(
            duration=duration,
            has_audio=has_audio,
            black=_intervals(log, "black_start", "black_end"),
            silent=_intervals(log, "silence_start", "silence_end"),
            first_luma=_luma(first),
            motion=_motion(first, later) if later else 0.0,
            last_frame=_frame(path, None, from_end=0.15),
            provider="ffmpeg",
        )
