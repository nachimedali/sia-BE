"""Checks measured on every finished video (S17-35, `studio_content_audit.md` §6).

What a machine can decide, on this machine's ffmpeg, for free — the thresholds
are goose-skills `review-finished-ad`'s:

* `black` — a black stretch longer than 0.3 s;
* `frozen` — the picture still for more than 2.5 s before the end card;
* `still_opening` — the first frame held for more than 1.5 s;
* `silent_opening` — no sound in the first second, when the clip has sound;
* `likeness` — the last frame before the end card against the product's photos,
  with the image gate's perceptual hash and threshold.

**Advisory.** They are shown at review and never refuse a render: the
thresholds have not been tuned on our vendors' clips yet. A file that cannot
be read gives no checks at all rather than a wrong one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

BLACK_MAX = 0.3
FREEZE_MAX = 2.5
OPENING_MAX = 1.5
SOUND_BY = 1.0
TIMEOUT_SECONDS = 120

_FLOAT = r"([0-9]+(?:\.[0-9]+)?)"
_BLACK = re.compile(rf"black_start:{_FLOAT}\s+black_end:{_FLOAT}\s+black_duration:{_FLOAT}")
_FREEZE_START = re.compile(rf"freeze_start: {_FLOAT}")
_FREEZE_END = re.compile(rf"freeze_end: {_FLOAT}")
_SILENCE_START = re.compile(rf"silence_start: {_FLOAT}")
_SILENCE_END = re.compile(rf"silence_end: {_FLOAT}")


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _probe(path: Path) -> tuple[float, bool] | None:
    """The file's length and whether it has a sound track; None if unreadable."""
    done = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "default=noprint_wrappers=1",
            str(path),
        ]
    )
    if done is None or done.returncode != 0:
        return None
    duration = re.search(rf"duration={_FLOAT}", done.stdout)
    if duration is None:
        return None
    return float(duration.group(1)), "codec_type=audio" in done.stdout


def _intervals(starts: list[str], ends: list[str], until: float) -> list[tuple[float, float]]:
    """Paired start/end times; an interval still open at the end runs to `until`."""
    out = []
    for index, start in enumerate(starts):
        end = float(ends[index]) if index < len(ends) else until
        out.append((float(start), end))
    return out


def _likeness(path: Path, at: float, photos: list[bytes], threshold: float) -> dict[str, Any]:
    from ai.services.quality import _dhash, _similarity_from_hash

    frame = path.with_suffix(".png")
    done = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{at:.2f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(frame),
        ]
    )
    if done is None or done.returncode != 0 or not frame.exists():
        return {"code": "likeness", "ok": True, "detail": "not measured", "score": None}
    with Image.open(frame) as image:
        frame_hash = _dhash(image)
    scores = []
    for photo in photos:
        try:
            scores.append(_similarity_from_hash(frame_hash, photo))
        except (OSError, ValueError):
            continue
    if not scores:
        return {"code": "likeness", "ok": True, "detail": "not measured", "score": None}
    score = round(max(scores), 3)
    return {
        "code": "likeness",
        "ok": score >= threshold,
        "detail": f"the last frame matches the product's photos at {score:.2f}",
        "score": score,
    }


def inspect(
    content: bytes,
    *,
    end_card_seconds: float,
    product_photos: list[bytes],
    threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """The checks for one finished video, or [] when it cannot be read."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return []
    with tempfile.TemporaryDirectory(prefix="checks-") as tmp:
        path = Path(tmp) / "video.mp4"
        path.write_bytes(content)
        probed = _probe(path)
        if probed is None:
            return []
        duration, has_sound = probed
        body_end = max(0.0, duration - end_card_seconds)
        args = [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(path),
            "-vf",
            f"blackdetect=d={BLACK_MAX}:pix_th=0.10,freezedetect=n=0.003:d=0.5",
        ]
        if has_sound:
            args += ["-af", "silencedetect=n=-45dB:d=0.5"]
        done = _run([*args, "-f", "null", "-"])
        if done is None or done.returncode != 0:
            return []
        log = done.stderr

        checks: list[dict[str, Any]] = []
        blacks = [(float(a), float(c)) for a, _b, c in _BLACK.findall(log)]
        black = [(start, length) for start, length in blacks if start < body_end]
        checks.append(
            {
                "code": "black",
                "ok": not black,
                "detail": (
                    f"{black[0][1]:.1f} s of black at {black[0][0]:.1f} s" if black else "no black"
                ),
            }
        )
        freezes = _intervals(_FREEZE_START.findall(log), _FREEZE_END.findall(log), duration)
        long_freezes = [
            (start, min(end, body_end) - start)
            for start, end in freezes
            if start < body_end and min(end, body_end) - start > FREEZE_MAX
        ]
        checks.append(
            {
                "code": "frozen",
                "ok": not long_freezes,
                "detail": (
                    f"{long_freezes[0][1]:.1f} s still at {long_freezes[0][0]:.1f} s"
                    if long_freezes
                    else "it keeps moving"
                ),
            }
        )
        opening = [end - start for start, end in freezes if start <= 0.05]
        checks.append(
            {
                "code": "still_opening",
                "ok": not (opening and opening[0] > OPENING_MAX),
                "detail": (
                    f"the first frame holds {opening[0]:.1f} s"
                    if opening and opening[0] > OPENING_MAX
                    else "it opens on movement"
                ),
            }
        )
        if has_sound:
            silences = _intervals(_SILENCE_START.findall(log), _SILENCE_END.findall(log), duration)
            quiet = [end - start for start, end in silences if start <= 0.05]
            checks.append(
                {
                    "code": "silent_opening",
                    "ok": not (quiet and quiet[0] > SOUND_BY),
                    "detail": (
                        f"no sound for the first {quiet[0]:.1f} s"
                        if quiet and quiet[0] > SOUND_BY
                        else "sound from the start"
                    ),
                }
            )
        if product_photos:
            checks.append(_likeness(path, max(0.0, body_end - 0.2), product_photos[:3], threshold))
        return checks
