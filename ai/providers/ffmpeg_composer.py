"""The reel composer, on this machine's `ffmpeg` (S17 T3, closes PR-19).

Implements `ai.providers.base.VideoComposer`. Composition is templated editing,
not generation, so it needs no vendor: every shot is cut to its length (a still
with a slow push-in, or a clip trimmed or held on its last frame), its line
fades and rises in (the kinetic type), the shots are joined by cuts or
crossfades, captions are burned in on their cues, an end card closes it and a
licensed music bed goes under it. Everything it is told is decided in
`ai.services.video`; it decides nothing.

A music bed is a file in `VIDEO_MUSIC_DIR` named after its row's key
(`oud.m4a`). A bed that is not there fails the render rather than shipping a
silent reel someone paid a music line for.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from django.conf import settings

from ai.providers.base import CaptionCue, EndCard, ReelShot, VideoResult
from common.exceptions import ProviderError

FPS = 30
#: How long a crossfade overlaps two shots, and how long a line takes to land.
CROSSFADE = 0.4
TEXT_IN = 0.35
SIZES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
MUSIC_SUFFIXES = (".m4a", ".mp3", ".aac", ".wav")
TIMEOUT_SECONDS = 300


def _run(args: list[str]) -> None:
    try:
        done = subprocess.run(args, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderError(
            "The reel could not be composed.", detail={"ffmpeg": str(error)}
        ) from error
    if done.returncode != 0:
        tail = done.stderr.decode(errors="replace")[-400:]
        raise ProviderError("The reel could not be composed.", detail={"ffmpeg": tail})


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        timeout=60,
        check=False,
        text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


class FfmpegVideoComposer:
    def __init__(self) -> None:
        font = Path(getattr(settings, "VIDEO_FONT_FILE", "") or "")
        self.font = f"fontfile={font}:" if font.is_file() else ""

    # --- text -----------------------------------------------------------------
    def _text(
        self, work: Path, name: str, text: str, *, size: int, y: str, width: int, kinetic: bool
    ) -> str:
        """A drawtext filter. The words go through a file, so no quote or
        colon in a caption can break the filter graph."""
        path = work / f"{name}.txt"
        path.write_text(textwrap.fill(text, width=max(8, int(width // (size * 0.55)))))
        rise = f"+{int(size * 0.6)}*(1-min(1,t/{TEXT_IN}))" if kinetic else ""
        alpha = f":alpha='min(1,t/{TEXT_IN})'" if kinetic else ""
        return (
            f"drawtext={self.font}textfile={path}:fontsize={size}:fontcolor=white"
            f":line_spacing={size // 4}:box=1:boxcolor=black@0.45:boxborderw={size // 3}"
            f":x=(w-text_w)/2:y='{y}{rise}'{alpha}"
        )

    # --- shots ----------------------------------------------------------------
    def _segment(
        self, work: Path, index: int, shot: ReelShot, length: float, size: tuple[int, int]
    ) -> Path:
        width, height = size
        cover = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        filters = [cover]
        if shot.clip is not None:
            source = work / f"shot{index}.mp4"
            source.write_bytes(shot.clip)
            head = ["-i", str(source)]
            # A clip shorter than its shot holds on its last frame.
            filters += [f"fps={FPS}", f"tpad=stop_mode=clone:stop_duration={length}"]
        else:
            source = work / f"shot{index}.img"
            source.write_bytes(shot.image)
            head = ["-loop", "1", "-framerate", str(FPS), "-t", f"{length}", "-i", str(source)]
            frames = max(1, round(length * FPS))
            filters.append(
                f"zoompan=z='1+0.08*on/{frames}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
                f":d=1:s={width}x{height}:fps={FPS}"
            )
        if shot.overlay:
            filters.append(
                self._text(
                    work,
                    f"line{index}",
                    shot.overlay,
                    size=width // 14,
                    y="h*0.68",
                    width=width,
                    kinetic=True,
                )
            )
        out = work / f"seg{index}.mp4"
        _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                *head,
                "-vf",
                ",".join([*filters, "format=yuv420p"]),
                "-t",
                f"{length}",
                "-r",
                str(FPS),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                str(out),
            ]
        )
        return out

    def _end_card(self, work: Path, card: EndCard, size: tuple[int, int]) -> Path:
        width, height = size
        filters = [
            self._text(
                work, "title", card.title, size=width // 11, y="h*0.40", width=width, kinetic=True
            )
        ]
        if card.call_to_action:
            filters.append(
                self._text(
                    work,
                    "cta",
                    card.call_to_action,
                    size=width // 18,
                    y="h*0.56",
                    width=width,
                    kinetic=True,
                )
            )
        out = work / "end.mp4"
        _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x111111:s={width}x{height}:r={FPS}:d={card.seconds}",
                "-vf",
                ",".join([*filters, "format=yuv420p"]),
                "-t",
                f"{card.seconds}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                str(out),
            ]
        )
        return out

    def _music(self, key: str) -> Path:
        folder = Path(getattr(settings, "VIDEO_MUSIC_DIR", "") or "")
        for suffix in MUSIC_SUFFIXES:
            path = folder / f"{key}{suffix}"
            if path.is_file():
                return path
        raise ProviderError("The music bed for this reel is not installed.", detail={"music": key})

    # --- the reel -------------------------------------------------------------
    def compose(
        self,
        *,
        shots: list[ReelShot],
        aspect: str,
        transition: str,
        music: str | None,
        captions: list[CaptionCue],
        end_card: EndCard | None,
        duration_seconds: float,
    ) -> VideoResult:
        started = time.monotonic()
        size = SIZES.get(aspect, SIZES["9:16"])
        music_path = self._music(music) if music else None  # before any encoding
        fade = CROSSFADE if transition == "crossfade" else 0.0
        with tempfile.TemporaryDirectory(prefix="reel-") as tmp:
            work = Path(tmp)
            parts: list[tuple[Path, float]] = []
            last = len(shots) - (0 if end_card else 1)
            for index, shot in enumerate(shots):
                # Every part but the last runs `fade` longer, so the overlap
                # leaves the reel exactly as long as asked.
                length = shot.seconds + (fade if index < last else 0.0)
                parts.append((self._segment(work, index, shot, length, size), length))
            if end_card:
                parts.append((self._end_card(work, end_card, size), end_card.seconds))

            inputs = [arg for path, _ in parts for arg in ("-i", str(path))]
            graph: list[str] = []
            if fade and len(parts) > 1:
                current, running = "[0:v]", parts[0][1]
                for index in range(1, len(parts)):
                    label = f"[x{index}]"
                    graph.append(
                        f"{current}[{index}:v]xfade=transition=fade:duration={fade}"
                        f":offset={running - fade:.3f}{label}"
                    )
                    current, running = label, running + parts[index][1] - fade
            else:
                streams = "".join(f"[{i}:v]" for i in range(len(parts)))
                graph.append(f"{streams}concat=n={len(parts)}:v=1:a=0[x0]")
                current = "[x0]"
            for index, cue in enumerate(captions):
                text = self._text(
                    work,
                    f"cue{index}",
                    cue.text,
                    size=size[0] // 20,
                    y="h*0.82",
                    width=size[0],
                    kinetic=False,
                )
                graph.append(f"{current}{text}:enable='between(t,{cue.start},{cue.end})'[c{index}]")
                current = f"[c{index}]"
            graph.append(f"{current}format=yuv420p[v]")

            audio: list[str] = []
            if music_path is not None:
                inputs += ["-stream_loop", "-1", "-i", str(music_path)]
                fade_out = max(0.0, duration_seconds - 1.0)
                graph.append(f"[{len(parts)}:a]afade=t=out:st={fade_out}:d=1[a]")
                audio = ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
            out = work / "reel.mp4"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    *inputs,
                    "-filter_complex",
                    ";".join(graph),
                    "-map",
                    "[v]",
                    *audio,
                    "-t",
                    f"{duration_seconds}",
                    "-r",
                    str(FPS),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-movflags",
                    "+faststart",
                    str(out),
                ]
            )
            return VideoResult(
                content=out.read_bytes(),
                mime="video/mp4",
                duration_seconds=_duration(out),
                provider="ffmpeg",
                model="ffmpeg-composer-1",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
