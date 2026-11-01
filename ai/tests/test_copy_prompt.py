"""The caption writer's instructions (`ai/prompts/text_v2.py`).

The rules follow the marketingskills `copywriting` and `social` skills and the
goose-skills `make-static-ads-ad-copy` step: specific over vague, the buyer's
words, one idea, no invented proof, and none of the phrasings that read as
machine-written. v1 stays on disk, unedited: a prompt change is a new file.
"""

from __future__ import annotations

from ai.prompts import text_v1, text_v2
from ai.services import prompting


def test_the_caption_writer_runs_on_v2_and_v1_is_kept() -> None:
    assert prompting.TEXT_SYSTEM_PROMPT == text_v2.SYSTEM_PROMPT
    assert text_v1.SYSTEM_PROMPT != text_v2.SYSTEM_PROMPT


def test_v2_carries_the_copywriting_rules() -> None:
    prompt = text_v2.SYSTEM_PROMPT
    for rule in (
        "Never invent",  # claims must be supportable
        "It's not X, it's Y",  # contrast reveals
        "No X, no Y, no Z",  # negation lists
        "Introducing",  # dead openers
        "one idea",
        "buyer",
    ):
        assert rule in prompt, rule
    # No placeholder the model could echo back into a post.
    assert "[" not in prompt and "{" not in prompt
