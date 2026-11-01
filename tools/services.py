"""The quick tools (design.md §8.10, C-11 / P0-04).

Three of them call the language provider; three do not, and that split is the
interesting part. `hashtag` ranks against the trend corpus this workspace's
category already has, `best-time` reads the analytics signals already computed,
and `thread-splitter` is arithmetic over `content.services.rules` — none of
them needs a model, and paying for one would be waste dressed as sophistication.

**Charged only on success.** The debit is the last thing that happens, after
the output exists, for the same reason the generation pipeline debits on a
passed quality gate rather than on an attempt: a user who is charged for an
error has been charged for nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from integrations.ports import CORPUS_WINDOW_DAYS
from tools.models import Tool, ToolConfig, ToolUsage

logger = logging.getLogger(__name__)

#: How many alternatives the writing tools return. Three is enough to choose
#: from and few enough to read; more turns a decision into a chore.
VARIANTS = 3

#: The corpus window `hashtag` ranks within. Matches the trend engine's own
#: 14-day window rather than inventing a second one — a tag that is hot this
#: fortnight is the question either way.
#: The corpus is the seam's now, and so is the window it is read over.
HASHTAG_WINDOW_DAYS = CORPUS_WINDOW_DAYS
HASHTAG_LIMIT = 12

_HASHTAG = re.compile(r"#(\w{2,50})")


class ToolUnavailableError(OCCSError):
    """An operator has taken this tool down. 409 rather than 404: the tool
    exists and is listed; it is simply not accepting work right now."""

    status_code = 409
    default_code = "tool_disabled"


@dataclass(frozen=True)
class ToolResult:
    output: dict[str, Any]
    #: Some tools legitimately answer "not yet" — `best-time` before a
    #: workspace has published anything, `hashtag` before its category has
    #: been harvested. That is not a failure, but it is also not worth a
    #: credit, so it is charged as zero.
    chargeable: bool = True


def run(*, workspace: Any, user: Any, slug: str, payload: dict[str, Any]) -> ToolUsage:
    """Runs one tool and records what it cost. Synchronous by design — see the
    module note on `tools.models`."""
    config = ToolConfig.objects.filter(slug=slug).first()
    if config is None:
        raise OCCSError("No such tool.", code="tool_not_found")
    if not config.is_enabled:
        raise ToolUnavailableError("This tool is temporarily unavailable.")

    entitlements = entitlements_for(workspace)
    entitlements.require_credits(config.credits_cost)

    result = _RUNNERS[slug](workspace, payload)

    charged = config.credits_cost if result.chargeable else 0
    with transaction.atomic():
        if charged:
            ledger.debit_credits(
                workspace,
                charged,
                quota=entitlements.quota("monthly_ai_credits"),
                note=f"tool:{slug}",
            )
        usage = ToolUsage.objects.create(
            workspace=workspace,
            user=user,
            tool=slug,
            payload=payload,
            output=result.output,
            credits_charged=charged,
        )
    logger.info("tool run", extra={"tool": slug, "workspace_id": workspace.pk})
    return usage


# -----------------------------------------------------------------------------
# The three that write
# -----------------------------------------------------------------------------
_SYSTEMS: dict[str, str] = {
    Tool.HEADLINE: (
        "You write short, concrete social headlines. No emoji unless the topic "
        "calls for one. Never invent a fact about the product."
    ),
    Tool.BIO: (
        "You write profile bios of at most 150 characters: who this is, what "
        "they make, and one reason to follow."
    ),
    Tool.HOOK: (
        "You write opening lines that earn the second line. Concrete, never "
        "clickbait, never a question the post does not answer."
    ),
    Tool.CTA: (
        "You write calls to action of at most six words. One clear verb, no "
        "false urgency, never a promise the product cannot keep."
    ),
}

#: L-6: the content languages, and nothing else.
LANGUAGES = {"en": "English", "fr": "French", "it": "Italian"}


def _write(workspace: Any, payload: dict[str, Any], *, tool: str) -> ToolResult:
    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider

    topic = str(payload.get("topic", "")).strip()
    if not topic:
        raise OCCSError("This tool needs a topic.", code="topic_required")

    result = get_text_provider(AITask.TOOLS, workspace=workspace).generate(
        system=_SYSTEMS[tool],
        prompt=f"Brand: {workspace.name}. Topic: {topic}",
        n=VARIANTS,
    )
    # `.body`, not the dataclass: the output column is JSON, and a variant's
    # rationale is prompt-engineering telemetry rather than something the card
    # renders.
    return ToolResult(output={"variants": [variant.body for variant in result.variants]})


def _rewrite(workspace: Any, payload: dict[str, Any]) -> ToolResult:
    """Sized to the platform's rules row, so a rewrite for X fits X."""
    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider
    from content.services.rules import PLATFORM_RULES

    body = _body(payload)
    platform = str(payload.get("platform", "")).strip()
    limit = ""
    if platform:
        if platform not in PLATFORM_RULES:
            raise OCCSError(f"Unknown platform: {platform}.", code="invalid_platform")
        limit = f" Keep it under {PLATFORM_RULES[platform].char_limit} characters for {platform}."

    result = get_text_provider(AITask.TOOLS, workspace=workspace).generate(
        system=(
            "You rewrite social captions: same facts, sharper words, the brand's own "
            "voice. Never add a claim the original does not make." + limit
        ),
        prompt=f"Brand: {workspace.name}. Caption:\n{body}",
        n=VARIANTS,
    )
    return ToolResult(output={"variants": [variant.body for variant in result.variants]})


def _translate(workspace: Any, payload: dict[str, Any]) -> ToolResult:
    """One version, not three: a translation is a rendering, not a choice."""
    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider

    body = _body(payload)
    language = str(payload.get("language", "")).strip().lower()
    if language not in LANGUAGES:
        raise OCCSError(
            "Translate into English, French or Italian.",
            code="invalid_language",
            detail={"language": sorted(LANGUAGES)},
        )

    result = get_text_provider(AITask.TOOLS, workspace=workspace).generate(
        system=(
            f"Translate the caption into {LANGUAGES[language]}. Keep hashtags, "
            "@mentions, links and emoji exactly as they are. Reply with the translation only."
        ),
        prompt=body,
        n=1,
    )
    return ToolResult(output={"variants": [variant.body for variant in result.variants]})


def _alt_text(workspace: Any, payload: dict[str, Any]) -> ToolResult:
    """Reads the picture through the vision call captions already use."""
    from rest_framework.exceptions import NotFound

    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider
    from content.models import MediaAsset, MediaKind

    media_id = payload.get("media_id")
    if not media_id:
        raise OCCSError("This tool needs an image.", code="media_required")
    # Another workspace's image does not exist from here: 404, never 403.
    asset = MediaAsset.objects.filter(
        pk=media_id, workspace=workspace, kind=MediaKind.IMAGE
    ).first()
    if asset is None:
        raise NotFound("No such image.")
    with asset.file.open("rb") as handle:
        image_bytes = handle.read()

    result = get_text_provider(AITask.TOOLS, workspace=workspace).caption(
        system=(
            "You write alt text for screen readers: what is in the picture, in one "
            "sentence of at most 125 characters. No 'image of', no hashtags."
        ),
        prompt="Describe this image.",
        image_bytes=image_bytes,
        n=VARIANTS,
    )
    return ToolResult(output={"variants": [variant.body for variant in result.variants]})


def _body(payload: dict[str, Any]) -> str:
    body = str(payload.get("body", "")).strip()
    if not body:
        raise OCCSError("This tool needs some text.", code="body_required")
    return body


# -----------------------------------------------------------------------------
# The three that read what is already there
# -----------------------------------------------------------------------------
def _hashtags(workspace: Any, _payload: dict[str, Any]) -> ToolResult:
    """Ranked from this workspace's own category corpus.

    Not from a model: a language model asked for hashtags returns plausible
    ones, and plausible is precisely the wrong property here — the value is
    that these are tags actually appearing in what is working in this category
    right now. Frequency over a real corpus is the whole answer.
    """
    import datetime as dt

    from django.utils import timezone

    from integrations.trendfeed import get_trend_feed

    if workspace.category_id is None:
        return ToolResult(output={"hashtags": []}, chargeable=False)

    since = timezone.now() - dt.timedelta(days=HASHTAG_WINDOW_DAYS)
    bodies = get_trend_feed(workspace.organization).corpus_bodies(
        category_id=int(workspace.category_id), since=since, limit=2000
    )

    counts: dict[str, int] = {}
    for body in bodies:
        for tag in _HASHTAG.findall(body or ""):
            key = f"#{tag.lower()}"
            counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:HASHTAG_LIMIT]
    return ToolResult(
        output={"hashtags": [{"tag": tag, "count": count} for tag, count in ranked]},
        chargeable=bool(ranked),
    )


def _thread(workspace: Any, payload: dict[str, Any]) -> ToolResult:
    """Splits text using the same splitter publishing uses.

    Reused rather than reimplemented, because a thread this tool previews and a
    thread the publisher sends must be the same thread — the preview-equals-
    publish rule (Part 7 rule 1) applied one surface wider. Sized against the
    rules row for the only platform that threads, so a limit change there moves
    this too.
    """
    from content.services.adaptation import _split_into_thread
    from content.services.rules import PLATFORM_RULES

    body = str(payload.get("body", "")).strip()
    if not body:
        raise OCCSError("This tool needs some text to split.", code="body_required")

    rule = PLATFORM_RULES["threads"]
    return ToolResult(output={"thread": _split_into_thread(body, rule.char_limit)})


def _best_time(workspace: Any, _payload: dict[str, Any]) -> ToolResult:
    """The workspace's own best slots, from captures already taken.

    Free of a provider call *and* free of a fresh capture: this reads the
    analytics signals, so running it costs nothing at the vendor and cannot
    contend with publishing for provider budget.
    """
    from analytics.services import signals

    horizon = entitlements_for(workspace).analytics_horizon_days()
    rows = signals.performance(workspace.pk, horizon_days=horizon)
    buckets = signals.best_times(rows)
    return ToolResult(output={"best_times": buckets}, chargeable=bool(buckets))


_RUNNERS: dict[str, Any] = {
    Tool.HEADLINE: lambda w, p: _write(w, p, tool=Tool.HEADLINE),
    Tool.BIO: lambda w, p: _write(w, p, tool=Tool.BIO),
    Tool.HOOK: lambda w, p: _write(w, p, tool=Tool.HOOK),
    Tool.HASHTAG: _hashtags,
    Tool.THREAD_SPLITTER: _thread,
    Tool.BEST_TIME: _best_time,
    Tool.CTA: lambda w, p: _write(w, p, tool=Tool.CTA),
    Tool.CAPTION_REWRITE: _rewrite,
    Tool.TRANSLATE: _translate,
    Tool.ALT_TEXT: _alt_text,
}
