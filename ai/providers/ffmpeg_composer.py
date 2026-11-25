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

from ai.providers.base import CaptionCue, EndCard, OverlayLayer, ReelShot, VideoResult
from common.exceptions import ProviderError

FPS = 30
#: How long a crossfade overlaps two shots, and how long a line takes to land.
CROSSFADE = 0.4
TEXT_IN = 0.35
SIZES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
MUSIC_SUFFIXES = (".m4a", ".mp3", ".aac", ".wav")
#: A bed under a clip's own sound (S17-38): low enough that a voice stays on top.
MUSIC_UNDER_VOICE = 0.18
TIMEOUT_SECONDS = 300
#: The platforms' own buttons and names cover the top ~11 %, the bottom ~21 %
#: and the right ~13 % of a 9:16 frame (goose-skills `review-finished-ad`:
#: 220, 400 and 140 px at 1080x1920). Words stay inside what is left: lines wrap
#: to the centre 74 % of the width, a shot's line sits at 56 % of the height and
#: captions at 70 %, both clear of the bottom band.
SAFE_WIDTH = 0.74
OVERLAY_Y = 0.56
CAPTION_Y = 0.70


def _hex(color: str) -> str:
    """`#RRGGBB` → ffmpeg's `0xRRGGBB`; anything else is the dark default."""
    value = color.strip().lstrip("#")
    if len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
        value = "111111"
    return f"0x{value.upper()}"


def _ink(color: str) -> str:
    """Dark or white text, whichever reads on `color` (relative luminance)."""
    value = _hex(color)[2:]
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return "0x111111" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.55 else "white"


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


def _has_audio(path: Path) -> bool:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
        text=True,
    )
    return bool(out.stdout.strip())


def _motion(speed: float, fps: int) -> list[str]:
    """S19-06: a speed-up, then a frame-rate drop held back up to the reel's
    rate (each frame repeated: the stop-motion look). Nothing for neither."""
    filters = [f"setpts=PTS/{speed:g}"] if speed != 1.0 else []
    return [*filters, f"fps={fps}"] if fps else filters


class FfmpegVideoComposer:
    def __init__(self) -> None:
        font = Path(getattr(settings, "VIDEO_FONT_FILE", "") or "")
        self.font = f"fontfile={font}:" if font.is_file() else ""

    # --- text -----------------------------------------------------------------
    def _text(
        self,
        work: Path,
        name: str,
        text: str,
        *,
        size: int,
        y: str,
        width: int,
        kinetic: bool,
        color: str = "white",
        box: str = "black@0.45",
        x: str = "(w-text_w)/2",
    ) -> str:
        """A drawtext filter. The words go through a file, so no quote or
        colon in a caption can break the filter graph. Lines wrap inside the
        safe width, clear of the platforms' side buttons. `box=""` draws none."""
        path = work / f"{name}.txt"
        usable = width * SAFE_WIDTH
        path.write_text(textwrap.fill(text, width=max(8, int(usable // (size * 0.55)))))
        rise = f"+{int(size * 0.6)}*(1-min(1,t/{TEXT_IN}))" if kinetic else ""
        alpha = f":alpha='min(1,t/{TEXT_IN})'" if kinetic else ""
        backing = f":box=1:boxcolor={box}:boxborderw={size // 3}" if box else ""
        return (
            f"drawtext={self.font}textfile={path}:fontsize={size}:fontcolor={color}"
            f":line_spacing={size // 4}{backing}"
            f":x={x}:y='{y}{rise}'{alpha}"
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
            if shot.right is not None:
                source = self._side_by_side(work, index, source, shot.right, length, size)
            head = ["-i", str(source)]
            # A clip shorter than its shot holds on its last frame.
            filters += [
                f"fps={FPS}",
                f"tpad=stop_mode=clone:stop_duration={length}",
            ]
        else:
            source = work / f"shot{index}.img"
            source.write_bytes(shot.image)
            head = ["-loop", "1", "-framerate", str(FPS), "-t", f"{length}", "-i", str(source)]
            frames = max(1, round(length * FPS))
            filters.append(
                f"zoompan=z='1+0.08*on/{frames}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
                f":d=1:s={width}x{height}:fps={FPS}"
            )
        if shot.badge:
            # A step number, top left inside the safe area (S19-06).
            filters.append(
                self._text(
                    work,
                    f"badge{index}",
                    shot.badge,
                    size=width // 9,
                    y="h*0.12",
                    width=width,
                    kinetic=False,
                    x="w*0.08",
                )
            )
        if shot.overlay:
            filters.append(
                self._text(
                    work,
                    f"line{index}",
                    shot.overlay,
                    size=width // 14,
                    y=f"h*{OVERLAY_Y}",
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

    def _side_by_side(
        self, work: Path, index: int, left: Path, right: bytes, length: float, size: tuple[int, int]
    ) -> Path:
        """Two clips in two halves (S19-06): the left plays for the first half
        of the shot while the right holds its first frame, then the right plays
        while the left holds its last. The left clip's sound is kept."""
        width, height = size
        half = length / 2
        other = work / f"shot{index}-right.mp4"
        other.write_bytes(right)
        cover = (
            f"scale={width // 2}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width // 2}:{height},fps={FPS}"
        )
        graph = (
            f"[0:v]{cover},trim=duration={half},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={half}[l];"
            f"[1:v]{cover},trim=duration={half},setpts=PTS-STARTPTS,"
            f"tpad=start_mode=clone:start_duration={half}[r];"
            "[l][r]hstack=inputs=2,format=yuv420p[v]"
        )
        out = work / f"pair{index}.mp4"
        _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(left),
                "-i",
                str(other),
                "-filter_complex",
                graph,
                "-map",
                "[v]",
                "-t",
                f"{length}",
                "-r",
                str(FPS),
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

    def _audio(
        self, work: Path, shots: list[ReelShot], lengths: list[float], first: int
    ) -> tuple[list[str], list[str]]:
        """One sound track per part (S19-10): a clip's own sound,
        padded or cut to the part's length; silence for a still or the end card.
        `first` is the index the first extra input will have. Returns the extra
        inputs and the filters labelling each part's track `[a<n>]`."""
        inputs: list[str] = []
        graph: list[str] = []
        for index, length in enumerate(lengths):
            shot = shots[index] if index < len(shots) else None
            source = work / f"shot{index}.mp4"
            if shot is not None and shot.clip is not None and _has_audio(source):
                inputs += ["-i", str(source)]
            else:
                inputs += ["-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo"]
            graph.append(
                f"[{first + index}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"apad,atrim=0:{length},asetpts=PTS-STARTPTS[a{index}]"
            )
        return inputs, graph

    def _end_card(self, work: Path, card: EndCard, size: tuple[int, int]) -> Path:
        """The brand card (S17-26): the kit's colour, the real logo file at the
        top, the product's photo in the middle, the name and a call-to-action
        pill below — everything inside the safe area, nothing drawn by a model."""
        width, height = size
        ink = _ink(card.background)
        background = f"color=c={_hex(card.background)}:s={width}x{height}:r={FPS}:d={card.seconds}"
        inputs = ["-f", "lavfi", "-i", background]
        graph: list[str] = []
        current = "[0:v]"
        # (name, file, the box it fits inside, its top edge): the logo under the
        # top band, the product filling the middle above the name.
        pictures = (
            ("logo", card.logo, (int(width * 0.40), int(height * 0.08)), int(height * 0.13)),
            ("image", card.image, (int(width * 0.62), int(width * 0.62)), int(height * 0.24)),
        )
        for name, content, (box_w, box_h), top in pictures:
            if content is None:
                continue
            path = work / f"card-{name}.img"
            path.write_bytes(content)
            index = inputs.count("-i")
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{card.seconds}"]
            inputs += ["-i", str(path)]
            graph.append(
                f"[{index}:v]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease,"
                f"format=rgba[{name}]"
            )
            graph.append(f"{current}[{name}]overlay=x=(main_w-overlay_w)/2:y={top}[with{name}]")
            current = f"[with{name}]"
        texts = [
            self._text(
                work,
                "title",
                card.title,
                size=width // 14,
                y="h*0.63",
                width=width,
                kinetic=True,
                color=ink,
                box="",
            )
        ]
        if card.call_to_action:
            texts.append(
                self._text(
                    work,
                    "cta",
                    card.call_to_action,
                    size=width // 20,
                    y="h*0.72",
                    width=width,
                    kinetic=True,
                    color=_ink(card.accent),
                    box=_hex(card.accent),
                )
            )
        graph.append(f"{current}{','.join([*texts, 'format=yuv420p'])}[card]")
        out = work / "end.mp4"
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
                "[card]",
                "-t",
                f"{card.seconds}",
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
        keep_audio: bool = False,
    ) -> VideoResult:
        started = time.monotonic()
        size = SIZES.get(aspect, SIZES["9:16"])
        music_path = self._music(music) if music else None  # before any encoding
        # How shots join: a crossfade, a slide to the left (S17-33), or a cut.
        effect = {"crossfade": "fade", "slide": "slideleft"}.get(transition, "")
        fade = CROSSFADE if effect else 0.0
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
                        f"{current}[{index}:v]xfade=transition={effect}:duration={fade}"
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
                    y=f"h*{CAPTION_Y}",
                    width=size[0],
                    kinetic=False,
                )
                graph.append(f"{current}{text}:enable='between(t,{cue.start},{cue.end})'[c{index}]")
                current = f"[c{index}]"
            graph.append(f"{current}format=yuv420p[v]")

            audio: list[str] = []
            fade_out = max(0.0, duration_seconds - 1.0)
            if keep_audio:
                extra, tracks = self._audio(
                    work, shots, [length for _, length in parts], inputs.count("-i")
                )
                graph += tracks
                inputs += extra
                if fade and len(parts) > 1:
                    joined = "[a0]"
                    for index in range(1, len(parts)):
                        graph.append(f"{joined}[a{index}]acrossfade=d={fade}[ax{index}]")
                        joined = f"[ax{index}]"
                else:
                    labels = "".join(f"[a{i}]" for i in range(len(parts)))
                    graph.append(f"{labels}concat=n={len(parts)}:v=0:a=1[ajoin]")
                    joined = "[ajoin]"
                if music_path is not None:
                    # The bed ducked under the model's own sound.
                    inputs += ["-stream_loop", "-1", "-i", str(music_path)]
                    bed = inputs.count("-i") - 1
                    graph.append(f"[{bed}:a]volume=0.25[bed]")
                    graph.append(f"{joined}[bed]amix=inputs=2:duration=first:normalize=0[mix]")
                    joined = "[mix]"
                graph.append(f"{joined}afade=t=out:st={fade_out}:d=1[a]")
                audio = ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
            elif music_path is not None:
                inputs += ["-stream_loop", "-1", "-i", str(music_path)]
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

    # --- stills for review (S19-13) ---------------------------------------------
    def overlay(self, *, clip: bytes, layers: list[OverlayLayer], aspect: str) -> VideoResult:
        """S21-02: each drawn layer over the clip on its window. The picture is
        re-encoded at the frame's size; the sound is copied as it is."""
        started = time.monotonic()
        width, height = SIZES.get(aspect, SIZES["9:16"])
        with tempfile.TemporaryDirectory(prefix="layers-") as tmp:
            work = Path(tmp)
            source = work / "clip.mp4"
            source.write_bytes(clip)
            inputs: list[str] = []
            graph = [
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}[v0]"
            ]
            for index, layer in enumerate(layers, start=1):
                picture = work / f"layer{index}.png"
                picture.write_bytes(layer.image)
                inputs += ["-i", str(picture)]
                window = (
                    f"between(t,{layer.start:g},{layer.end:g})"
                    if layer.end is not None
                    else f"gte(t,{layer.start:g})"
                )
                graph.append(
                    f"[v{index - 1}][{index}:v]overlay={layer.x}:{layer.y}:"
                    f"enable='{window}'[v{index}]"
                )
            graph.append(f"[v{len(layers)}]format=yuv420p[out]")
            out = work / "layered.mp4"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    *inputs,
                    "-filter_complex",
                    ";".join(graph),
                    "-map",
                    "[out]",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-c:a",
                    "copy",
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

    def frames(self, *, clip: bytes, at: list[float]) -> list[bytes]:
        with tempfile.TemporaryDirectory(prefix="frames-") as tmp:
            work = Path(tmp)
            source = work / "clip.mp4"
            source.write_bytes(clip)
            out: list[bytes] = []
            for index, seconds in enumerate(at):
                still = work / f"beat{index}.png"
                _run(
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-ss",
                        f"{seconds:.2f}",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=360:-2",
                        str(still),
                    ]
                )
                out.append(still.read_bytes())
            return out

    # --- one clip, words burned in ----------------------------------------------
    def caption(
        self,
        *,
        clip: bytes,
        captions: list[CaptionCue],
        aspect: str,
        speed: float = 1.0,
        fps: int = 0,
        seconds: float | None = None,
        music: str | None = None,
    ) -> VideoResult:
        """The clip at the frame's size with each cue drawn on its window; its
        length and its sound are the clip's own (the sound re-encoded, never
        dropped: a vendor's ambient track is part of what was paid for). A
        `music` bed is mixed low under that sound — a voice stays on top — or
        plays alone when the clip is silent (S17-38)."""
        started = time.monotonic()
        music_path = self._music(music) if music else None  # before any encoding
        width, height = SIZES.get(aspect, SIZES["9:16"])
        with tempfile.TemporaryDirectory(prefix="caption-") as tmp:
            work = Path(tmp)
            source = work / "clip.mp4"
            source.write_bytes(clip)
            filters = [
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                *_motion(speed, fps),
                *([f"fps={FPS}"] if fps else []),
            ]
            for index, cue in enumerate(captions):
                text = self._text(
                    work,
                    f"cue{index}",
                    cue.text,
                    size=width // 16,
                    y=f"h*{CAPTION_Y}",
                    width=width,
                    kinetic=False,
                )
                filters.append(f"{text}:enable='between(t,{cue.start},{cue.end})'")
            out = work / "captioned.mp4"
            tempo = [f"atempo={speed:g}"] if speed != 1.0 else []
            if music_path is None:
                sound = ["-map", "0:a?", *(["-af", tempo[0]] if tempo else [])]
                bed: list[str] = []
            else:
                bed = ["-stream_loop", "-1", "-i", str(music_path)]
                own = ",".join(["anull", *tempo])
                graph = (
                    f"[0:a]{own}[own];[1:a]volume={MUSIC_UNDER_VOICE}[bed];"
                    "[own][bed]amix=inputs=2:duration=first:normalize=0[a]"
                    if _has_audio(source)
                    else f"[1:a]volume={MUSIC_UNDER_VOICE}[a]"
                )
                sound = ["-filter_complex", graph, "-map", "[a]", "-shortest"]
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    *bed,
                    "-vf",
                    ",".join([*filters, "format=yuv420p"]),
                    "-map",
                    "0:v",
                    *sound,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    *(["-t", f"{seconds}"] if seconds else []),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
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
