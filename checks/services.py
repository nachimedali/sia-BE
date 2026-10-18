"""Pre-publish checks (steps-plan S4).

A third layer beside artifact quality (`ai.services.quality`) and brand
screening (`taste.screening`) — C-09: separate services, separate tests. Those
two judge a generated picture before anyone sees it; this one judges **a post
as it will go out**: its media against each platform's frames, the words in its
pictures, its caption against platform policy.

```
run_checks(post)          every check → one CheckRun, verdict PASS / FIX / BLOCK
latest(post)              the newest finished run
summary(row, current=…)   what a queue badge shows; `stale` when the post moved on
ensure_not_blocked(post)  the gate: approve and schedule call it; 409 on BLOCK
```

**Fix required warns; Block stops.** Only a policy finding the provider calls a
block stops a post — a picture that will be cropped is something to fix, not a
reason to refuse. **A check that did not run is `UNAVAILABLE`**, counted apart
and never as a pass: no OCR vendor, no brand palette, a video whose frames
nothing here decodes. The gate refuses on what it knows to be wrong, and the
panel says plainly what was not looked at.

**The gate reads the post as it is.** A run whose fingerprint does not match the
current content is out of date, and the gate runs the checks again before
deciding — so a Block cannot be dodged by editing after the last run, and a
fixed post is not held back by a stale one.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import prefetch_related_objects
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from PIL import Image, UnidentifiedImageError

from accounts.models import User
from billing.services.flags import CHECKS_S4, flag_enabled
from checks.logo import find_logo
from checks.models import CheckRun, CheckRunState, CheckStatus, CheckVerdict
from checks.ports import TextBox, VideoFacts, get_ocr, get_policy, get_video
from common.exceptions import StateConflict
from content.models import MediaAsset, MediaKind, Platform, Post, PostFormat
from content.services.rules import FormatRule, format_rule

logger = logging.getLogger(__name__)

#: Text shorter than this fraction of the picture's height is unreadable on a
#: phone held at arm's length — about 2.5% of a 1080 x 1350 feed post, 34 px.
LEGIBLE_MIN_HEIGHT = 0.025
#: How close (Euclidean RGB) an image colour must be to a brand colour to count.
PALETTE_DISTANCE = 80.0

LABELS = {
    "format": "Size and frame",
    "legibility": "Text legibility",
    "safe_zone": "Safe zones",
    "palette": "Brand palette",
    "logo": "Logo",
    "policy": "Platform policy",
    "video": "Video",
}


class ChecksBlockedError(StateConflict):
    default_code = "checks_blocked"
    default_detail = "A pre-publish check blocks this post. Fix it before approving or scheduling."


# -----------------------------------------------------------------------------
# What the checks read
# -----------------------------------------------------------------------------
#: What the checks read off a post. Every loader prefetches these, so a run
#: and its fingerprint read the media and targets once between them.
PREFETCH = ("media_attachments__media_asset", "targets")


def _media(post: Post) -> list[MediaAsset]:
    """The post's media in order — from the prefetch when the caller made one,
    so a queue page fingerprints every row without a query per post."""
    return post.ordered_media()


def _formats(post: Post) -> dict[str, str]:
    """Each planned platform's chosen format (its settings target), else feed."""
    chosen = {
        target.platform: target.post_format or PostFormat.FEED
        for target in post.targets.all()
        if target.social_account_id is None
    }
    platforms = list(dict.fromkeys(post.planned_platforms or list(chosen)))
    return {platform: chosen.get(platform, PostFormat.FEED) for platform in platforms}


def fingerprint(
    post: Post,
    *,
    media: list[MediaAsset] | None = None,
    formats: dict[str, str] | None = None,
) -> str:
    """Everything the checks read, hashed. Two posts with the same fingerprint
    get the same results; a post whose fingerprint moved has unchecked content.
    A caller that already gathered the media and formats passes them in."""
    payload = {
        "body": post.master_body,
        "media": [asset.pk for asset in (_media(post) if media is None else media)],
        "formats": sorted((_formats(post) if formats is None else formats).items()),
        "category": post.category_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _result(
    key: str, status: str, message: str, *, subject: str = "", **extra: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "label": LABELS[key],
        "status": status,
        "subject": subject,
        "message": message,
        **{k: v for k, v in extra.items() if v not in (None, "")},
    }


def _platform_name(platform: str) -> str:
    return Platform(platform).label if platform in Platform.values else platform


def _format_name(platform: str, post_format: str) -> str:
    label = PostFormat(post_format).label if post_format in PostFormat.values else post_format
    return f"{_platform_name(platform)} {label.lower()}"


def _bytes_reader() -> Callable[[MediaAsset], bytes]:
    """Each file read from storage once per run, however many checks look at
    it — the text reader and the palette both want the same image."""
    read: dict[int, bytes] = {}

    def get(asset: MediaAsset) -> bytes:
        if asset.pk not in read:
            with asset.file.open("rb") as handle:
                read[asset.pk] = bytes(handle.read())
        return read[asset.pk]

    return get


# -----------------------------------------------------------------------------
# The checks
# -----------------------------------------------------------------------------
def _check_format(media: list[MediaAsset], formats: dict[str, str]) -> list[dict[str, Any]]:
    if not formats:
        return [
            _result(
                "format",
                CheckStatus.UNAVAILABLE,
                "No platform is chosen yet, so there is no frame to check the media against.",
            )
        ]
    if not media:
        return [_result("format", CheckStatus.PASS, "No media: nothing to fit to a frame.")]
    out: list[dict[str, Any]] = []
    for platform, post_format in formats.items():
        rule = format_rule(platform, post_format) or format_rule(platform, PostFormat.FEED)
        where = _format_name(platform, post_format)
        if rule is None:
            continue
        for asset in media:
            subject = f"A-{asset.pk} · {where}"
            if asset.kind not in rule.allowed_media_kinds:
                out.append(
                    _result(
                        "format",
                        CheckStatus.FIX,
                        f"{where} does not take {asset.kind.lower()} media.",
                        subject=subject,
                        media=asset.pk,
                        platform=platform,
                    )
                )
                continue
            out.extend(_fit(asset, rule, where, subject, platform))
    if not out:
        names = ", ".join(_format_name(p, f) for p, f in formats.items())
        out.append(_result("format", CheckStatus.PASS, f"Every file fits its frame on {names}."))
    return out


def _fit(
    asset: MediaAsset, rule: FormatRule, where: str, subject: str, platform: str
) -> Iterable[dict[str, Any]]:
    if not asset.width or not asset.height:
        return
    ratio = asset.width / asset.height
    if rule.aspect_range is not None:
        low, high = rule.aspect_range
        if not low <= ratio <= high:
            yield _result(
                "format",
                CheckStatus.FIX,
                f"A-{asset.pk} is {asset.width} x {asset.height} ({ratio:.2f}); {where} shows "
                f"{low:.2f} to {high:.2f} without cropping, so part of it will be cut off.",
                subject=subject,
                media=asset.pk,
                platform=platform,
            )
    if rule.min_width is not None and asset.width < rule.min_width:
        yield _result(
            "format",
            CheckStatus.FIX,
            f"A-{asset.pk} is {asset.width} px wide; {where} is shown at {rule.min_width} px, "
            "so it will look soft.",
            subject=subject,
            media=asset.pk,
            platform=platform,
        )


def _check_text(
    images: list[MediaAsset], formats: dict[str, str], read: Callable[[MediaAsset], bytes]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legibility and safe zones — both read the words in the pictures."""
    if not images:
        return [], []
    vertical = {
        platform: rule.safe_zone
        for platform, post_format in formats.items()
        if (rule := format_rule(platform, post_format)) is not None and rule.safe_zone
    }
    ocr = get_ocr()
    if ocr is None:
        unavailable = "No text reader is configured, so the words in the images were not read."
        unread = [_result("safe_zone", CheckStatus.UNAVAILABLE, unavailable)] if vertical else []
        return [_result("legibility", CheckStatus.UNAVAILABLE, unavailable)], unread

    legibility: list[dict[str, Any]] = []
    safe: list[dict[str, Any]] = []
    found = 0
    for asset in images:
        try:
            boxes: list[TextBox] = list(ocr.read(read(asset)).boxes)
        except Exception:  # a reader failure is "not checked", never "passed"
            logger.warning("ocr failed on media %s", asset.pk, exc_info=True)
            legibility.append(
                _result(
                    "legibility",
                    CheckStatus.UNAVAILABLE,
                    "The text reader could not read this image.",
                    subject=f"A-{asset.pk}",
                    media=asset.pk,
                )
            )
            continue
        found += len(boxes)
        for box in boxes:
            if box.height < LEGIBLE_MIN_HEIGHT:
                legibility.append(
                    _result(
                        "legibility",
                        CheckStatus.FIX,
                        f"“{box.text}” is {box.height:.1%} of the image's height; under "
                        f"{LEGIBLE_MIN_HEIGHT:.1%} it cannot be read on a phone.",
                        subject=f"A-{asset.pk}",
                        media=asset.pk,
                    )
                )
            for platform, (top, bottom) in vertical.items():
                if box.y < top or box.y + box.height > 1 - bottom:
                    band = "top" if box.y < top else "bottom"
                    safe.append(
                        _result(
                            "safe_zone",
                            CheckStatus.FIX,
                            f"“{box.text}” sits in the {band} band {_platform_name(platform)} "
                            "covers with its own interface.",
                            subject=f"A-{asset.pk} · {_platform_name(platform)}",
                            media=asset.pk,
                            platform=platform,
                        )
                    )
    if not any(r["status"] != CheckStatus.UNAVAILABLE for r in legibility):
        message = (
            "No text found in the images."
            if found == 0
            else f"All {found} line{'s' if found != 1 else ''} of text are large enough to read."
        )
        legibility.append(_result("legibility", CheckStatus.PASS, message))
    if vertical and not safe:
        safe.append(
            _result(
                "safe_zone",
                CheckStatus.PASS,
                "No text sits under the platform's interface.",
            )
        )
    return legibility, safe


def _hex_rgb(value: str) -> tuple[int, int, int] | None:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except (ValueError, IndexError):
        return None


def _dominant(data: bytes, n: int = 6) -> list[tuple[int, int, int]]:
    """The image's main colours, most used first — only the ones it really has."""
    with Image.open(io.BytesIO(data)) as image:
        reduced = image.convert("RGB").resize((64, 64)).quantize(colors=n).convert("RGB")
        counted = reduced.getcolors(64 * 64) or []
    counted.sort(key=lambda pair: pair[0], reverse=True)
    out: list[tuple[int, int, int]] = []
    for _count, rgb in counted[:n]:
        if isinstance(rgb, tuple) and len(rgb) == 3:
            out.append((int(rgb[0]), int(rgb[1]), int(rgb[2])))
    return out


def _check_palette(
    post: Post, images: list[MediaAsset], read: Callable[[MediaAsset], bytes]
) -> list[dict[str, Any]]:
    if not images:
        return []
    from brand.services.kit import kit_for

    kit = kit_for(post.workspace)
    brand = [
        rgb for _role, hex_ in (kit.colors if kit else ()) if (rgb := _hex_rgb(hex_)) is not None
    ]
    if not brand:
        return [
            _result(
                "palette",
                CheckStatus.UNAVAILABLE,
                "No brand colours are set yet. Add them in Settings → Brand kit.",
            )
        ]
    out: list[dict[str, Any]] = []
    for asset in images:
        try:
            seen = _dominant(read(asset))
        except (UnidentifiedImageError, OSError):
            continue
        near = any(
            sum((a - b) ** 2 for a, b in zip(img, ref, strict=True)) ** 0.5 <= PALETTE_DISTANCE
            for img in seen
            for ref in brand
        )
        if not near:
            out.append(
                _result(
                    "palette",
                    CheckStatus.FIX,
                    f"None of the brand's main colours appear in A-{asset.pk}.",
                    subject=f"A-{asset.pk}",
                    media=asset.pk,
                )
            )
    return out or [
        _result("palette", CheckStatus.PASS, "The brand's colours appear in every image.")
    ]


#: How wide, as a share of the picture, a logo has to be for the detector to
#: find it at all — said in the message, so "not found" is never read as "absent".
LOGO_REACH = "about a seventh of the picture's width"


def _workspace_logo(post: Post) -> bytes | None:
    """The brand's logo file, read once per run; `None` when there is none or it
    cannot be read."""
    if not post.workspace.logo:
        return None
    try:
        with post.workspace.logo.open("rb") as handle:
            return bytes(handle.read())
    except (OSError, ValueError):
        return None


def _check_logo(
    post: Post, images: list[MediaAsset], read: Callable[[MediaAsset], bytes], logo: bytes | None
) -> list[dict[str, Any]]:
    if not images or not post.workspace.logo:
        return []
    if logo is None:
        return [_result("logo", CheckStatus.UNAVAILABLE, "The workspace logo could not be read.")]

    out: list[dict[str, Any]] = []
    for asset in images:
        subject = f"A-{asset.pk}"
        try:
            match = find_logo(read(asset), logo)
        except (UnidentifiedImageError, OSError, ValueError):
            continue
        if match.state == "unsearchable":
            return [
                _result(
                    "logo",
                    CheckStatus.UNAVAILABLE,
                    "The logo is too plain (one flat colour) to be searched for in a picture. "
                    "Check by eye that it is present and undistorted.",
                )
            ]
        if match.state == "distorted":
            out.append(
                _result(
                    "logo",
                    CheckStatus.FIX,
                    f"The logo in {subject} looks stretched out of its proportions"
                    f" ({match.corner}). Place it again at its own shape.",
                    subject=subject,
                    media=asset.pk,
                )
            )
        elif match.state == "not_found":
            out.append(
                _result(
                    "logo",
                    CheckStatus.FIX,
                    f"We could not find the logo in {subject}. It can only be recognised when it"
                    f" is {LOGO_REACH} and clear of what is behind it, so this is not proof it"
                    " is missing: check by eye.",
                    subject=subject,
                    media=asset.pk,
                )
            )
    return out or [_result("logo", CheckStatus.PASS, "The logo is in place in every image.")]


def _check_policy(post: Post, formats: dict[str, str]) -> list[dict[str, Any]]:
    text = post.master_body.strip()
    if not text:
        return [_result("policy", CheckStatus.PASS, "No caption to check.")]
    policy = get_policy()
    if policy is None:
        return [
            _result(
                "policy",
                CheckStatus.UNAVAILABLE,
                "No policy reader is configured, so the caption was not checked against "
                "platform rules.",
            )
        ]
    category = post.category.slug if post.category else ""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for platform in formats or {"": ""}:
        try:
            review = policy.review(text=text, platform=platform, category=category)
        except Exception:
            logger.warning("policy review failed for post %s", post.pk, exc_info=True)
            return [
                _result(
                    "policy",
                    CheckStatus.UNAVAILABLE,
                    "The policy reader did not answer. Run the checks again.",
                )
            ]
        for finding in review.findings:
            key = (finding.title, finding.excerpt)
            if key in found:
                found[key]["platforms"].append(platform)
                continue
            found[key] = _result(
                "policy",
                CheckStatus.BLOCK if finding.status == "BLOCK" else CheckStatus.FIX,
                f"“{finding.excerpt}” — {finding.title.lower()}.",
                subject=finding.title,
                clause=finding.clause,
                source=finding.source,
                excerpt=finding.excerpt,
                rewrite=finding.rewrite,
                provider=review.provider,
            )
            found[key]["platforms"] = [platform] if platform else []
    if found:
        return list(found.values())
    names = ", ".join(_platform_name(p) for p in formats) or "the platforms"
    return [_result("policy", CheckStatus.PASS, f"No policy issue found for {names}.")]


#: Largest video read into memory and measured. Past this the file is not
#: inspected (reported, not skipped silently).
VIDEO_MAX_BYTES = 200 * 1024 * 1024
#: Opening conditions that say "no hook": a black or near-black first frame, or
#: a picture that hardly changes across the first second.
HOOK_BLACK_LUMA = 0.06
HOOK_MIN_MOTION = 0.015
BLACK_STRETCH = 1.0  # seconds of black mid-video that count as a fault
DEAD_AIR = 2.0  # seconds of silence under a soundtrack that count as a fault


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _read_video(asset: MediaAsset) -> VideoFacts | None:
    """Measure one video, or `None` when it cannot be read or is too large."""
    port = get_video()
    if port is None:
        return None
    try:
        if asset.file.size > VIDEO_MAX_BYTES:
            return None
    except (OSError, ValueError):
        return None
    folder = tempfile.mkdtemp(prefix="checks-video-")
    try:
        path = Path(folder) / "clip.mp4"
        with asset.file.open("rb") as source, path.open("wb") as target:
            shutil.copyfileobj(source, target)
        return port.inspect(str(path))
    except Exception:
        logger.warning("video %s could not be inspected", asset.pk, exc_info=True)
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _judge_video(asset: MediaAsset, facts: VideoFacts, logo: bytes | None) -> list[dict[str, Any]]:
    subject = f"A-{asset.pk}"
    found: list[dict[str, Any]] = []

    def fix(key: str, message: str) -> None:
        found.append(_result(key, CheckStatus.FIX, message, subject=subject, media=asset.pk))

    if facts.first_luma < HOOK_BLACK_LUMA and facts.motion < HOOK_MIN_MOTION:
        fix(
            "video",
            "It opens on a black frame and nothing moves in the first second: there is no hook.",
        )
    elif facts.motion < HOOK_MIN_MOTION:
        fix("video", "Nothing moves in the first second, so nothing makes a viewer stay.")
    for start, end in facts.black:
        if start <= 0.05:
            continue  # the opening is judged above
        if end - start >= BLACK_STRETCH:
            fix("video", f"Black frames from {_stamp(start)} to {_stamp(end)}.")
    if facts.has_audio:
        for start, end in facts.silent:
            if end - start >= DEAD_AIR:
                fix(
                    "video", f"Dead air from {_stamp(start)} to {_stamp(end)}: the sound drops out."
                )

    if not found:
        found.append(
            _result(
                "video",
                CheckStatus.PASS,
                "It opens with movement and has no black frames"
                + (" or dead air." if facts.has_audio else "; it has no sound track."),
                subject=subject,
                media=asset.pk,
            )
        )

    if logo is not None:
        if facts.last_frame is None:
            found.append(
                _result(
                    "logo",
                    CheckStatus.UNAVAILABLE,
                    "The end card was not inspected: no last frame could be read.",
                    subject=subject,
                    media=asset.pk,
                )
            )
        else:
            try:
                match = find_logo(facts.last_frame, logo)
            except (UnidentifiedImageError, OSError, ValueError):
                match = None
            if match is not None and match.state in ("not_found", "distorted"):
                found.append(
                    _result(
                        "logo",
                        CheckStatus.FIX,
                        "We could not find the logo on the last frame of the video. It can only be"
                        f" recognised when it is {LOGO_REACH} and clear of what is behind it, so"
                        " check the end card by eye.",
                        subject=subject,
                        media=asset.pk,
                    )
                )
            elif match is not None and match.state == "found":
                found.append(
                    _result(
                        "logo",
                        CheckStatus.PASS,
                        "The logo is on the end card.",
                        subject=subject,
                        media=asset.pk,
                    )
                )
    return found


def _check_video(videos: list[MediaAsset], logo: bytes | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for asset in videos:
        facts = _read_video(asset)
        if facts is None:
            out.append(
                _result(
                    "video",
                    CheckStatus.UNAVAILABLE,
                    "The hook, dead air and black frames were not inspected: the video could not "
                    "be read here.",
                    subject=f"A-{asset.pk}",
                    media=asset.pk,
                )
            )
            continue
        out.extend(_judge_video(asset, facts, logo))
    return out


# -----------------------------------------------------------------------------
# Running and reading
# -----------------------------------------------------------------------------
def _verdict(results: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    counts = dict.fromkeys(CheckStatus.values, 0)
    for result in results:
        counts[result["status"]] += 1
    if counts[CheckStatus.BLOCK]:
        return CheckVerdict.BLOCK, counts
    if counts[CheckStatus.FIX]:
        return CheckVerdict.FIX, counts
    return CheckVerdict.PASS, counts


def _revision(post: Post) -> int | None:
    value = post.revisions.order_by("-sequence").values_list("sequence", flat=True).first()
    return int(value) if value is not None else None


def run_checks(post: Post, *, actor: User | None = None, run: CheckRun | None = None) -> CheckRun:
    """Every check, once, stored as one run. Never raises for a reader that
    failed — that check is reported unavailable and the rest still run."""
    media = _media(post)
    formats = _formats(post)
    images = [asset for asset in media if asset.kind == MediaKind.IMAGE]
    videos = [asset for asset in media if asset.kind == MediaKind.VIDEO]
    read = _bytes_reader()
    legibility, safe = _check_text(images, formats, read)
    palette = _check_palette(post, images, read)
    logo_bytes = _workspace_logo(post) if images or videos else None
    logo = _check_logo(post, images, read, logo_bytes)
    del read  # the image bytes: not held through the policy reader's latency
    results = [
        *_check_format(media, formats),
        *legibility,
        *safe,
        *palette,
        *logo,
        *_check_policy(post, formats),
        *_check_video(videos, logo_bytes),
    ]
    verdict, counts = _verdict(results)
    if run is None:
        run = CheckRun(workspace=post.workspace, post=post, created_by=actor)
    run.fingerprint = fingerprint(post, media=media, formats=formats)
    run.revision = _revision(post)
    run.results = results
    run.verdict = verdict
    run.counts = counts
    run.state = CheckRunState.DONE
    run.finished_at = timezone.now()
    run.save()
    return run


def latest(post: Post) -> CheckRun | None:
    return CheckRun.objects.filter(post=post, state=CheckRunState.DONE).first()


def summary(latest_run: dict[str, Any] | None, *, current: str) -> dict[str, Any]:
    """A badge from `checks.queries.annotate`'s row: the verdict, how many of
    each, and whether the post has changed since. A post never checked is
    `NONE` — said, not hidden; `None` (no badge) is reserved for the step being
    off, which the caller decides."""
    if not latest_run or not latest_run.get("verdict"):
        return {
            "verdict": "NONE",
            "counts": dict.fromkeys(CheckStatus.values, 0),
            "stale": False,
            "run_at": None,
        }
    run_at = latest_run.get("run_at")
    return {
        "verdict": latest_run["verdict"],
        "counts": latest_run.get("counts") or {},
        "stale": latest_run.get("fingerprint") != current,
        "run_at": parse_datetime(run_at) if isinstance(run_at, str) else run_at,
    }


def ensure_not_blocked(post: Post, *, actor: User | None = None) -> CheckRun | None:
    """The gate. 409 `checks_blocked` while a check blocks the post as it is now.

    Flag off is pre-phase behaviour: no gate. An out-of-date or missing run is
    replaced before deciding, so the answer is always about this content.
    """
    if not flag_enabled(post.workspace.organization, CHECKS_S4):
        return None
    prefetch_related_objects([post], *PREFETCH)  # a no-op when the caller did
    run = latest(post)
    if run is None or run.fingerprint != fingerprint(post):
        run = run_checks(post, actor=actor)
    if run.verdict == CheckVerdict.BLOCK:
        blocking = [r for r in run.results if r["status"] == CheckStatus.BLOCK]
        raise ChecksBlockedError(
            detail={
                "run": run.pk,
                "blocking": [
                    {"label": r["label"], "subject": r["subject"], "message": r["message"]}
                    for r in blocking
                ],
            }
        )
    return run


def queue_after_submit(post: Post, *, actor: User | None) -> None:
    """Warm the queue's badge: run the checks once the submit has committed,
    on the checks queue, so the reviewer opens a post already checked."""
    if not flag_enabled(post.workspace.organization, CHECKS_S4):
        return
    from checks.tasks import run_post_checks

    actor_id = actor.pk if actor else None
    transaction.on_commit(lambda: run_post_checks.delay(post.pk, actor_id))
