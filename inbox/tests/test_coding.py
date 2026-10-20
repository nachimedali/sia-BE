"""Coding an audience comment (S2-02): rules first, the model second, and
"uncoded" — a null — whenever neither can tell."""

from __future__ import annotations

from typing import Any

import pytest

from billing.models import FeatureFlag
from billing.services.flags import INBOX_S2
from inbox import tasks
from inbox.models import CodingSource, CommentCoding
from inbox.services import coding
from inbox.tests.conftest import make_comment

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("body", "intent"),
    [
        ("Where can I buy this?", "purchase_intent"),
        ("Take my money", "purchase_intent"),
        ("Où acheter ce vase ?", "purchase_intent"),
        ("Quanto costa?", "purchase_intent"),
        ("Do you make it in blue?", "product_request"),
        ("Avez-vous une version plus grande", "product_request"),
        ("Ce l'avete in nero?", "product_request"),
        ("My order arrived damaged, I want a refund", "complaint"),
        ("C'est une arnaque", "complaint"),
        ("Questa è una truffa", "complaint"),
        ("Way too expensive for what it is", "objection"),
        ("Trop cher pour ce que c'est", "objection"),
        ("How long does the glaze last?", "question"),
        ("Pourquoi le bleu ?", "question"),
        ("Absolutely gorgeous", "praise"),
        ("J'adore", "praise"),
        ("Bellissimo", "praise"),
    ],
)
def test_the_rules_settle_the_obvious_cases_in_three_languages(body: str, intent: str) -> None:
    assert coding.by_rules(body) == intent


@pytest.mark.parametrize("body", ["", "🔥🔥", "first!", "tagging @sam", "ok"])
def test_what_the_rules_cannot_tell_is_not_guessed(body: str) -> None:
    assert coding.by_rules(body) is None


def test_a_complaint_outranks_the_question_inside_it() -> None:
    assert coding.by_rules("Why did mine arrive broken? I want a refund") == "complaint"


def test_coding_writes_one_row_per_comment_and_stays_idempotent(target: Any) -> None:
    comment = make_comment(target, "Where can I buy this?")

    assert coding.code_comments([comment]) == 1
    assert coding.code_comments([comment]) == 0

    row = CommentCoding.objects.get(comment=comment)
    assert (row.intent, row.source) == ("purchase_intent", CodingSource.RULES)


def test_an_unreadable_comment_stays_uncoded_and_is_not_retried(
    target: Any, text_provider: Any
) -> None:
    """The fake text model answers prose, not a label, so the comment must stay
    null — never "praise" or "neutral" by default."""
    comment = make_comment(target, "the thing with the other thing from last week honestly")

    assert coding.code_comments([comment]) == 1

    row = CommentCoding.objects.get(comment=comment)
    assert row.intent is None
    assert coding.code_comments([comment]) == 0


def test_the_model_codes_what_the_rules_leave(
    target: Any, text_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    comment = make_comment(target, "honestly I would have to see it in my kitchen first though")
    monkeypatch.setattr(coding, "by_model", lambda bodies: ["objection"] * len(bodies))

    coding.code_comments([comment])

    row = CommentCoding.objects.get(comment=comment)
    assert (row.intent, row.source) == ("objection", CodingSource.MODEL)


def _model_says(monkeypatch: pytest.MonkeyPatch, text_provider: Any, body: str) -> None:
    from ai.providers.base import TextGenerationResult, TextVariant

    monkeypatch.setattr(
        type(text_provider),
        "generate",
        lambda self, **kw: TextGenerationResult(
            variants=[TextVariant(body=body)],
            provider="fake",
            model="fake",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        ),
    )


def test_a_model_reply_with_the_wrong_number_of_lines_codes_nothing(
    text_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, text_provider, "question")

    assert coding.by_model(["one", "two"]) == [None, None]


def test_a_label_outside_the_vocabulary_is_not_accepted(
    text_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, text_provider, "spam")

    assert coding.by_model(["buy cheap watches"]) == [None]


def test_a_teammates_call_is_never_overwritten_by_the_automatic_pass(
    target: Any, user: Any
) -> None:
    comment = make_comment(target, "Where can I buy this?")
    coding.set_intent(comment, intent="praise", actor=user)

    assert coding.code_comments([comment]) == 0

    row = CommentCoding.objects.get(comment=comment)
    assert (row.intent, row.source, row.coded_by) == ("praise", CodingSource.MANUAL, user)


def test_a_teammate_can_say_it_is_none_of_them(target: Any, user: Any) -> None:
    comment = make_comment(target, "Where can I buy this?")
    coding.code_comments([comment])

    coding.set_intent(comment, intent=None, actor=user)

    assert CommentCoding.objects.get(comment=comment).intent is None


def test_the_beat_pass_codes_new_comments(target: Any) -> None:
    make_comment(target, "Where can I buy this?")
    make_comment(target, "Love it", external_id="c-2")

    assert tasks.code_new_comments() == 2
    assert tasks.code_new_comments() == 0


def test_the_beat_pass_skips_an_organization_with_the_flag_off(
    target: Any, paid_workspace: Any
) -> None:
    FeatureFlag.objects.create(
        organization=paid_workspace.organization, key=INBOX_S2, enabled=False
    )
    make_comment(target, "Where can I buy this?")

    assert tasks.code_new_comments() == 0
    assert not CommentCoding.objects.exists()
