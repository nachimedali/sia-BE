"""The last check before a template's words reach a model (S17-43).

`template_fit` keeps contradicting choices out; this catches the rest — a
template's own data, an admin edit, a layer that leaks — on the exact text
about to be sent. A hit fails the job before the vendor is called, so nothing
is paid; the same function runs over the whole catalogue in CI
(`ai/tests/test_prompt_lint.py`). Deterministic: no model is asked.
"""

from __future__ import annotations

import re

from common.exceptions import OCCSError

#: Words that remove the people a template puts in frame (beta render #7).
_NO_PEOPLE = re.compile(
    r"\bno (?:people|person|one) in (?:the )?frame\b|\bthe product is the only subject\b"
    r"|\bclean still life\b",
    re.I,
)
#: A copywriter's instructions: a picture model told them prints them.
_COPY = re.compile(
    r"(?<![\w&])#(?![0-9a-f]{6}\b)[a-z]\w{1,}|\bhashtags?\b|\bcalls? to action\b"
    r"|\bcaption length\b|\bclose with one of\b",
    re.I,
)
#: A beat that is a label, not an action ("8-10 s: Call to action.").
_LABEL_BEAT = re.compile(
    r"\d+(?:\.\d+)?-\d+(?:\.\d+)? s: (?:call to action|end card|the product|hook|cta)[.;]",
    re.I,
)


#: A spoken line with nothing in it, or speech the same prompt silences (S17-38).
_EMPTY_LINE = re.compile(r'says: "\s*"')
_SAYS = re.compile(r'says: "')
_NO_SPEECH = re.compile(r"\bno speech\b", re.I)


class PromptConflictError(OCCSError):
    """The words about to be sent contradict the template or carry copy."""

    default_code = "prompt_conflict"
    default_detail = "This template's words contradict each other; nothing was sent."


def problems(prompt: str, *, person: bool) -> list[str]:
    """What is wrong with `prompt`, in plain words; [] when it may be sent."""
    found = []
    if person and _NO_PEOPLE.search(prompt):
        found.append("it removes the person the template puts in frame")
    copy = _COPY.search(prompt)
    if copy:
        found.append(f"it carries a caption instruction ({copy.group(0)!r})")
    label = _LABEL_BEAT.search(prompt)
    if label:
        found.append(f"a beat is a label, not an action ({label.group(0)!r})")
    if _EMPTY_LINE.search(prompt):
        found.append("a spoken line is empty")
    if _SAYS.search(prompt) and _NO_SPEECH.search(prompt):
        found.append("it asks for speech and for no speech")
    return found


def require_clean(prompt: str, *, person: bool) -> None:
    found = problems(prompt, person=person)
    if found:
        raise PromptConflictError(
            "This template's words contradict each other: " + "; ".join(found) + ".",
            detail={"problems": found},
        )
