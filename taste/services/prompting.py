"""Assembling the context a generation runs under (P5-09, P5-15).

**Deterministic and versioned.** `system template + taste profile + active
rules + trend context + few-shot from accepted history`, extending the existing
three-context grounding rather than replacing it. Deterministic because a
prompt nobody can reconstruct makes every decision recorded against it
unattributable.
"""

from __future__ import annotations

from typing import Any

from taste.models import ContentCandidate, Rule

#: Bumped when the assembled shape changes meaning, not when a field is added —
#: the same rule `OPTIONS_SCHEMA_VERSION` follows. Recorded on every `Decision`
#: so a shift in acceptance can be attributed to a prompt change rather than
#: guessed at.
PROMPT_TEMPLATE_VERSION = "p5.1"


def build_prompt_context(candidate: ContentCandidate) -> dict[str, Any]:
    """Everything the generator is told, as data.

    **The parent's rejection reason is carried forward** (P5-09). A
    regeneration that ignores why the first attempt failed reproduces the
    failure — so the reason is not merely stored on the row, it is put in
    front of the model, and `test_the_reason_reaches_the_prompt` is what holds
    that true rather than a comment promising it.
    """
    profile = candidate.taste_profile
    context: dict[str, Any] = {
        "template_version": PROMPT_TEMPLATE_VERSION,
        "taste_profile_version": profile.version,
        "voice": dict(profile.voice),
        "structural": dict(profile.structural),
        "topic_posture": dict(profile.topic_posture),
        # The policy is stated to the generator *and* enforced afterwards by
        # `screening`. Telling it the rules makes violations rarer; checking
        # in code is what makes the guarantee (C-09).
        "hard_constraints": dict(profile.hard_constraints),
    }

    ruleset = candidate.ruleset
    if ruleset is not None:
        context["ruleset_version"] = ruleset.version
        context["rules"] = [
            {"kind": rule.kind, "payload": rule.payload}
            for rule in Rule.objects.filter(ruleset=ruleset, accepted_at__isnull=False)
        ]

    if candidate.parent_reason_code:
        context["retry"] = {
            "rejected_because": candidate.parent_reason_code,
            "instruction": (
                f"The previous attempt was rejected as '{candidate.parent_reason_code}'. "
                "Produce something that does not repeat that fault."
            ),
        }

    return context
