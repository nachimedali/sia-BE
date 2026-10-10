"""Rule proposals (P7-11) — Learn proposes, a human activates.

Part 7 rule 14, and the reason the loop is worth building at all: a system that
applied its own conclusions would be one whose mistakes compound silently and
invisibly. Every rule here is created **unaccepted** — `accepted_at` null — in
a ruleset that is **not active**, and only `accept` moves either.

**Only Strong findings become rules.** An Emerging finding is surfaced as a
suggested *test* and creates no `Rule` row at all. That is not a formality:
Emerging means the effect showed up inside a single campaign, and a rule built
from one campaign encodes that campaign's season, offer and audience as though
they were the brand's permanent physics. The finding itself is the proposal —
it already carries the segment, the grade and the sample size — so a parallel
"test proposal" table would only be a second place for the same row to rot.

**A rejected proposal is a `Decision`, not a delete.** The rejection is
training signal in its own right: a workspace that turns down every timing rule
is telling the system something about itself, and a deleted row says nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from learn.models import Confidence, Digest, Finding
from taste.models import Decision, Rule, RuleKind, RuleSet, Verdict
from workspaces.models import Workspace

#: Which dimension produces which kind of rule. A dimension absent from this
#: map yields **no proposal** — `platform` is the live example: "Instagram
#: outperforms LinkedIn" is a true finding and not a rule, because no rule the
#: generator understands can act on it. Silently inventing a kind for it would
#: put a rule in the set that does nothing and cannot be debugged.
_RULE_KIND_BY_DIMENSION: dict[str, str] = {
    "format": RuleKind.FORMAT,
    "media_kind": RuleKind.FORMAT,
    "posting_hour": RuleKind.TIMING,
    "topic": RuleKind.TOPIC,
    "tone": RuleKind.VOICE,
    "length_band": RuleKind.STRUCTURE,
}


def actor_or_none(actor: Any) -> Any:
    """A `Decision`/`Rule` actor FK is nullable — an unsaved or absent actor
    (`AnonymousUser`, a system-triggered run) must resolve to `None`, never to
    a stray unsaved instance."""
    return actor if getattr(actor, "pk", None) else None


def _next_version(workspace_id: int) -> int:
    # Locked so two concurrent Learn runs cannot mint the same version — the
    # same guard `taste.services.profiles.create_profile` takes before minting
    # a `TasteProfile` version.
    Workspace.objects.select_for_update().get(pk=workspace_id)
    latest = (
        RuleSet.objects.filter(workspace_id=workspace_id)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    return (latest or 0) + 1


@transaction.atomic
def propose_from(digest: Digest, *, findings: Iterable[Finding] | None = None) -> RuleSet | None:
    """Build an inactive ruleset from this digest's Strong findings.

    `findings` lets a caller that just wrote them (`run_learn`) pass the
    `bulk_create` result straight through instead of paying for a `SELECT` of
    rows created moments ago in the same transaction.

    Returns `None` when nothing qualified, which is the common early case and
    not a failure — an empty ruleset would be a version number burnt on
    nothing, and a proposal list a user has to open to discover is empty.
    """
    candidates = digest.findings.all() if findings is None else findings
    qualifying = [
        finding
        for finding in candidates
        if finding.confidence == Confidence.STRONG
        and not finding.excluded_reason
        and finding.segment.get("dimension") in _RULE_KIND_BY_DIMENSION
    ]
    if not qualifying:
        return None

    ruleset = RuleSet.objects.create(
        workspace_id=digest.workspace_id,
        version=_next_version(digest.workspace_id),
        # Not active, and not activated by anything in this module.
        is_active=False,
        derived_from=digest,
    )

    for finding in qualifying:
        dimension = finding.segment["dimension"]
        Rule.objects.create(
            ruleset=ruleset,
            kind=_RULE_KIND_BY_DIMENSION[dimension],
            payload={
                "dimension": dimension,
                "prefer": finding.segment.get("value"),
                # The evidence, inline as well as by FK: a reader of the rule
                # should not have to join to find out how much it rests on.
                "sample_size": finding.sample_size,
                "led_in": finding.comparison.get("led_in"),
                "campaigns_observed": finding.campaigns_observed,
            },
            provenance=finding,
        )

    return ruleset


def suggested_tests(digest: Digest) -> list[Finding]:
    """Emerging findings, which are proposals to *test* and never rules."""
    return [
        finding
        for finding in digest.findings.all()
        if finding.confidence == Confidence.EMERGING and not finding.excluded_reason
    ]


def _record(
    rule: Rule, *, actor: Any, verdict: str, reason_code: str = "", note: str = ""
) -> Decision:
    return Decision.objects.create(
        rule=rule,
        candidate=None,
        ruleset_version=rule.ruleset.version,
        actor=actor_or_none(actor),
        verdict=verdict,
        reason_code=reason_code,
        note=note,
        payload_as_shown=dict(rule.payload),
    )


@transaction.atomic
def accept(rule: Rule, *, actor: Any, note: str = "") -> Decision:
    """A human activates one proposed rule, and the act is recorded.

    Accepting a rule does **not** activate its ruleset. A set becomes active
    through the existing taste-profile path, deliberately: a half-accepted set
    going live would mean generating under rules nobody finished reading.
    """
    if rule.accepted_at is None:
        rule.accepted_by = actor_or_none(actor)
        rule.accepted_at = timezone.now()
        rule.save(update_fields=["accepted_by", "accepted_at"])

    return _record(rule, actor=actor, verdict=Verdict.ACCEPTED, note=note)


@transaction.atomic
def reject(rule: Rule, *, actor: Any, reason_code: str = "other", note: str = "") -> Decision:
    """A human turns down a proposal — recorded, not deleted (P7-11).

    The rule row stays, unaccepted, with its provenance intact. "We proposed
    this and they said no, for this reason" is a fact about the workspace's
    taste; a deleted row is the same silence as never having proposed it.
    """
    return _record(rule, actor=actor, verdict=Verdict.REJECTED, reason_code=reason_code, note=note)
