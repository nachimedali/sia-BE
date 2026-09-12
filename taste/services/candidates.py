"""The candidate lifecycle, and the one door into `Post` (C-01, L-2, P5-G3).

```
generated
   ├─→ screened_out   (hard-constraint violation; never shown; logged)
   └─→ pending_review
          ├─→ rejected      → Decision → training signal
          ├─→ regenerating  → new candidate, parent FK, carries the reason
          ├─→ expired       (review window passed) → weak signal
          └─→ approved      → materialises a Post → existing pipeline
```

**`materialise` is the only function in the codebase that builds a `Post` from
a candidate, and it refuses anything not `approved`.** The refusal lives here,
at the service boundary, rather than in a view — so a task, a command or a
later endpoint inherits it instead of having to remember it (C-01 asks for
exactly this, "asserted at the service boundary, not by review").
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from common.exceptions import StateConflict
from content.models import Post
from content.services.posts import create_post
from taste.models import (
    REASON_CODES,
    CandidateState,
    ContentCandidate,
    Decision,
    TasteProfile,
    Verdict,
)
from taste.services import profiles as profile_service
from taste.services.prompting import PROMPT_TEMPLATE_VERSION, build_prompt_context
from taste.services.screening import screen
from workspaces.models import Workspace

#: States a candidate can still be acted on from. `screened_out` is absent
#: deliberately: it was never shown to anyone, so there is nothing to approve.
_REVIEWABLE = (CandidateState.PENDING_REVIEW,)


@transaction.atomic
def propose(
    *,
    workspace: Workspace,
    profile: TasteProfile,
    payload: dict[str, Any],
    product: Any = None,
    generation: Any = None,
    campaign: Any = None,
    parent: ContentCandidate | None = None,
) -> ContentCandidate:
    """Create a candidate and screen it **before** anyone can see it.

    Screening happens here rather than at read time so that a violating
    candidate never enters the queue at all (P5-G1) — a filter applied when
    rendering would still have loaded the row, counted it in a total, and
    depended on every future caller remembering the filter.

    Expiry defaults to the campaign boundary (P5-10).
    """
    candidate = ContentCandidate(
        workspace=workspace,
        taste_profile=profile,
        payload=payload,
        product=product,
        generation=generation,
        campaign=campaign,
        parent=parent,
        parent_reason_code=_rejection_reason(parent) if parent else "",
        expires_at=campaign.ends_at if campaign is not None else None,
    )

    result = screen(
        payload.get("master_body", ""), profile_service.constraints_for(workspace, product)
    )
    if result.passed:
        candidate.state = CandidateState.PENDING_REVIEW
    else:
        # Logged, never surfaced (P5-08), so the violation *rate* stays
        # measurable — a rising rate signals prompt or profile drift.
        candidate.state = CandidateState.SCREENED_OUT
        candidate.screened_reason = result.reason

    candidate.save()
    return candidate


def _rejection_reason(candidate: ContentCandidate) -> str:
    latest = candidate.decisions.filter(verdict=Verdict.REJECTED).first()
    return latest.reason_code if latest else ""


def review_queue(workspace: Workspace) -> Any:
    """What a reviewer is actually shown.

    **Filtered in the queryset**, which is what makes P5-G1 a gate rather than
    a convention: a `screened_out` row is never loaded, so no serializer, page
    count or future caller can leak one.
    """
    return (
        ContentCandidate.objects.filter(workspace=workspace, state=CandidateState.PENDING_REVIEW)
        .select_related("taste_profile", "product")
        .order_by("created_at", "id")
    )


def screening_violation_rate(workspace: Workspace) -> float | None:
    """Share of candidates refused by policy — monitored, not merely recorded.

    `None` with nothing to measure, never `0.0`: "no candidates yet" and "no
    violations" are different facts, and the second would be a claim we cannot
    make (Part 7 rule 12's habit, applied to a rate).
    """
    counts = ContentCandidate.objects.filter(workspace=workspace).aggregate(
        total=Count("id"), screened=Count("id", filter=Q(state=CandidateState.SCREENED_OUT))
    )
    if not counts["total"]:
        return None
    return float(counts["screened"]) / float(counts["total"])


def _record(
    candidate: ContentCandidate,
    *,
    verdict: str,
    actor: User | None,
    reason_code: str = "",
    note: str = "",
    payload_as_shown: dict[str, Any] | None = None,
    edit_diff: dict[str, Any] | None = None,
) -> Decision:
    return Decision.objects.create(
        candidate=candidate,
        payload_as_shown=payload_as_shown
        if payload_as_shown is not None
        else dict(candidate.payload),
        taste_profile_version=candidate.taste_profile.version,
        ruleset_version=candidate.ruleset.version if candidate.ruleset else None,
        prompt_context=build_prompt_context(candidate),
        model_identity=_model_identity(candidate),
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        actor=actor,
        verdict=verdict,
        reason_code=reason_code,
        note=note,
        edit_diff=edit_diff,
    )


def _model_identity(candidate: ContentCandidate) -> str:
    """Which model produced this, for attribution (P5-15).

    Blank when there was no provider call behind the candidate — a
    hand-written proposal has no model to name, and inventing one would put a
    false cause in the dataset the taste model learns from.
    """
    generation = candidate.generation
    return generation.model_identity if generation is not None else ""


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """`{field: [before, after]}` for what actually moved — the same shape
    `PostRevision.diff` uses, so both are read the same way."""
    keys = set(before) | set(after)
    return {
        key: [before.get(key), after.get(key)] for key in keys if before.get(key) != after.get(key)
    }


@transaction.atomic
def approve(
    candidate: ContentCandidate,
    *,
    actor: User,
    edited_payload: dict[str, Any] | None = None,
    note: str = "",
) -> ContentCandidate:
    """Say yes, record why, and materialise the post.

    **Idempotent.** A double-click or a retried request must not publish the
    same thing twice, so a candidate that already made a post is returned
    unchanged rather than making a second one.

    An `edited_payload` identical to the original is an acceptance, **not** an
    edit: counting it as one would poison the highest-value signal here
    (P5-13), which is precisely "what did the human change".
    """
    if candidate.post_id:
        return candidate
    if candidate.state not in _REVIEWABLE:
        raise StateConflict(
            f"A {candidate.state} candidate cannot be approved.",
            detail={"candidate": candidate.pk, "state": candidate.state},
        )

    shown = dict(candidate.payload)
    final = dict(edited_payload) if edited_payload is not None else shown
    diff = _diff(shown, final)

    _record(
        candidate,
        verdict=Verdict.ACCEPTED_WITH_EDITS if diff else Verdict.ACCEPTED,
        actor=actor,
        note=note,
        payload_as_shown=shown,
        edit_diff=diff or None,
    )

    candidate.payload = final
    candidate.state = CandidateState.APPROVED
    candidate.save(update_fields=["payload", "state", "updated_at"])
    return materialise(candidate, actor=actor)


@transaction.atomic
def materialise(candidate: ContentCandidate, *, actor: User) -> ContentCandidate:
    """**The only path from a candidate to a `Post`** (P5-G3).

    Refuses anything not `approved`, and an `approved` candidate is only
    reachable through `approve`, which always writes a `Decision` first. A
    caller reaching past `approve` to build the post itself is the bypass L-2
    forbids — so the refusal is here, where every future caller inherits it,
    rather than in whichever view happens to be the entry point today.
    """
    if candidate.state != CandidateState.APPROVED:
        raise StateConflict(
            "Only an approved candidate becomes a post.",
            detail={"candidate": candidate.pk, "state": candidate.state},
        )
    if candidate.post_id:
        return candidate

    if not candidate.decisions.filter(
        verdict__in=(Verdict.ACCEPTED, Verdict.ACCEPTED_WITH_EDITS)
    ).exists():
        # Belt to `approve`'s braces. An `approved` row with no approving
        # decision should be unreachable; if it ever is, this is the invariant
        # that says so out loud rather than quietly publishing.
        raise StateConflict(
            "This candidate is approved with no approving decision behind it.",
            detail={"candidate": candidate.pk},
        )

    payload = candidate.payload
    product = candidate.product
    post: Post = create_post(
        workspace=candidate.workspace,
        author=actor,
        master_body=payload.get("master_body", ""),
        category=product.category if product is not None else None,
    )
    candidate.post = post
    candidate.save(update_fields=["post", "updated_at"])
    return candidate


@transaction.atomic
def reject(
    candidate: ContentCandidate, *, actor: User, reason_code: str, note: str = ""
) -> ContentCandidate:
    """Say no, **with a code from the vocabulary** (P5-12).

    Free text is refused because it cannot be aggregated: "63% of your
    rejections this month were `off_brand_voice`" routes to a specific profile
    revision, and a thousand distinct sentences route nowhere.
    """
    if reason_code not in REASON_CODES:
        raise ValidationError(
            {"reason_code": f"Unknown reason {reason_code!r}.", "allowed": list(REASON_CODES)}
        )
    if candidate.state not in _REVIEWABLE:
        raise StateConflict(
            f"A {candidate.state} candidate cannot be rejected.",
            detail={"candidate": candidate.pk, "state": candidate.state},
        )

    _record(candidate, verdict=Verdict.REJECTED, actor=actor, reason_code=reason_code, note=note)
    candidate.state = CandidateState.REJECTED
    candidate.save(update_fields=["state", "updated_at"])
    return candidate


@transaction.atomic
def regenerate(candidate: ContentCandidate, *, actor: User) -> ContentCandidate:
    """A second attempt that **knows why the first failed** (P5-09, P5-G2).

    Refused on a candidate nobody rejected: with no reason to carry, the
    regeneration would be indistinguishable from a fresh generation, and the
    whole point is that it is not.
    """
    reason = _rejection_reason(candidate)
    if not reason:
        raise StateConflict(
            "Only a rejected candidate can be regenerated — there is no reason to carry.",
            detail={"candidate": candidate.pk, "state": candidate.state},
        )

    candidate.state = CandidateState.REGENERATING
    candidate.save(update_fields=["state", "updated_at"])

    return propose(
        workspace=candidate.workspace,
        profile=candidate.taste_profile,
        payload=dict(candidate.payload),
        product=candidate.product,
        campaign=candidate.campaign,
        parent=candidate,
    )


def expire_due(*, now: Any = None) -> int:
    """Time out candidates nobody reviewed.

    The `Decision` names **nobody**, because nobody made this call — and
    expiry is a *weak* signal, weighted below explicit rejection: it says
    something about the queue's health rather than about the content (P5-10).
    A rising expiry rate is a product-health alert, not a taste input.
    """
    moment = now or timezone.now()
    due = ContentCandidate.objects.filter(
        state=CandidateState.PENDING_REVIEW, expires_at__isnull=False, expires_at__lte=moment
    ).select_related("taste_profile", "ruleset", "generation")

    count = 0
    for candidate in due:
        with transaction.atomic():
            _record(candidate, verdict=Verdict.EXPIRED, actor=None)
            candidate.state = CandidateState.EXPIRED
            candidate.save(update_fields=["state", "updated_at"])
        count += 1
    return count
