"""Coding an audience comment by what it asks for (S2-02).

Two coders in order, and a third outcome that is not a guess: rules first (the
obvious cases, in the three product languages, free and deterministic), then the
text model for longer comments the rules do not settle. Where neither can tell,
the comment stays **uncoded** — a null intent — rather than being filed under
the nearest label. A wrong "complaint" sends a teammate to the wrong place; an
honest "uncoded" costs one glance.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from ai.providers.llm_text import get_text_provider
from analytics.models import AudienceComment
from common.exceptions import ProviderError
from inbox.models import CodingSource, CommentCoding, Intent

logger = logging.getLogger(__name__)

#: Comments the rules settle without a model call. Order is the priority: a
#: comment that is both a complaint and a question is a complaint first.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        Intent.COMPLAINT,
        re.compile(
            r"\b(broken|arrived (damaged|late)|never arrived|not arrived|refund|scam|rip.?off|"
            r"terrible|worst|disappointed|cass[ée]e?s?|rembours\w*|arnaque|d[ée][çc]u\w*|"
            r"jamais re[çc]u|rott[oa]|rimbors\w*|truffa|deluso\w*|mai arrivat\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.OBJECTION,
        re.compile(
            r"\b(too expensive|overpriced|not worth|why (so|is it so) (much|expensive)|"
            r"cheaper (elsewhere|at)|trop cher|pas donn[ée]|vaut pas|troppo car[oa]|"
            r"non vale|costa troppo)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.PURCHASE_INTENT,
        re.compile(
            r"\b(where (can|do) i (buy|get|order)|how (much|do i order)|take my money|"
            r"i('| wi)ll take (it|one|two)|i want (it|one|this)|shut up and|"
            r"o[uù] (puis-je|acheter|trouver)|combien co[uû]te|je le veux|je veux (le|la|ça)|"
            r"dove (posso )?(compr\w+|trov\w+)|quanto costa|lo voglio|la voglio)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.PRODUCT_REQUEST,
        re.compile(
            r"\b(do you (have|make|sell|ship)|will you (make|add|ship)|please (make|add|bring)|"
            r"(come|available) in (blue|red|black|white|green|large|small)|when (will|is) .* "
            r"(back|available|restock)|avez-vous|vous faites|faites-vous|ce l.?avete|"
            r"fate anche|farete)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.QUESTION,
        re.compile(
            r"(\?|\b(how|what|which|when|where|who|does|is it|can i|comment|quel(le)?s?|"
            r"pourquoi|come|quando|quale|perch[eé]|dove)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.PRAISE,
        re.compile(
            r"\b(love|loved|amazing|beautiful|gorgeous|stunning|perfect|incredible|obsessed|"
            r"adore|magnifique|superbe|splendide|j.?adore|bellissim\w*|stupend\w*|meraviglios\w*|"
            r"adoro)\b",
            re.IGNORECASE,
        ),
    ),
)

#: Above this many words a rules match on a bare "question" or "praise" is
#: weak evidence (a long comment can contain both a "?" and a complaint), so
#: those two defer to the model when it is available.
SHORT_WORDS = 12

_PROMPT = (
    "Label each numbered comment on a brand's social post with exactly one of: "
    "question, objection, complaint, praise, purchase_intent, product_request, unclear. "
    "Use unclear when none clearly fits. Answer with one label per line, in order, "
    "and nothing else."
)
_LABELS = {intent.value for intent in Intent}


def by_rules(body: str) -> str | None:
    text = " ".join(body.split())
    for intent, pattern in _RULES:
        if pattern.search(text):
            return str(intent)
    return None


def by_model(bodies: Sequence[str]) -> list[str | None]:
    """One label or `None` per body. A provider failure or an unreadable line
    is `None` — never a default label."""
    if not bodies:
        return []
    numbered = "\n".join(
        f"{n + 1}. {' '.join(body.split())[:400]}" for n, body in enumerate(bodies)
    )
    try:
        result = get_text_provider().generate(system=_PROMPT, prompt=numbered, n=1)
        lines = [
            line.strip().lower().rstrip(".")
            for line in result.variants[0].body.splitlines()
            if line.strip()
        ]
    except (ProviderError, IndexError):
        logger.warning("comment coding failed; leaving uncoded", exc_info=True)
        return [None] * len(bodies)
    if len(lines) != len(bodies):
        return [None] * len(bodies)
    return [line if line in _LABELS else None for line in lines]


def code_comments(comments: Sequence[AudienceComment]) -> int:
    """Code every comment in `comments` that has no coding row yet; return how
    many rows were written. A comment a teammate already coded, or one this has
    already tried, is left exactly as it is."""
    coded = set(
        CommentCoding.objects.filter(comment__in=comments).values_list("comment_id", flat=True)
    )
    todo = [comment for comment in comments if comment.pk not in coded]
    if not todo:
        return 0

    verdicts: dict[int, tuple[str | None, str]] = {}
    for comment in todo:
        intent = by_rules(comment.body)
        words = len(comment.body.split())
        weak = intent in (Intent.QUESTION, Intent.PRAISE) and words > SHORT_WORDS
        verdicts[comment.pk] = (None if weak else intent, CodingSource.RULES)

    undecided = [c for c in todo if verdicts[c.pk][0] is None and c.body.strip()]
    for comment, label in zip(undecided, by_model([c.body for c in undecided]), strict=True):
        if label is not None:
            verdicts[comment.pk] = (label, CodingSource.MODEL)

    ids = [comment.pk for comment in todo]
    before = CommentCoding.objects.filter(comment_id__in=ids).count()
    # One INSERT; a teammate's row or a racing pass's row wins the conflict.
    CommentCoding.objects.bulk_create(
        [
            CommentCoding(
                comment=comment, intent=verdicts[comment.pk][0], source=verdicts[comment.pk][1]
            )
            for comment in todo
        ],
        ignore_conflicts=True,
    )
    return CommentCoding.objects.filter(comment_id__in=ids).count() - before


def set_intent(comment: AudienceComment, *, intent: str | None, actor: Any) -> CommentCoding:
    """A teammate's call, which the automatic pass never overwrites. `None`
    says "this is not any of them" and stays uncoded on purpose."""
    row, _ = CommentCoding.objects.update_or_create(
        comment=comment,
        defaults={"intent": intent, "source": CodingSource.MANUAL, "coded_by": actor},
    )
    return row
