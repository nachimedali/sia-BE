"""C-01 — autopilot never constructs a `Post` (P5-07, gate P5-G3).

Autopilot plans slots and produces **candidates**. A person approves one, and
that approval writes a `Decision` before anything becomes a `Post`. There is no
tier, flag or configuration that skips the person (L-2).

**Asserted at the service boundary, not by review** — C-01 asks for exactly
that, because "we checked the diff" is not a guarantee that survives the next
contributor.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from taste.models import CandidateState, ContentCandidate, Decision, Verdict

pytestmark = pytest.mark.django_db


@pytest.fixture
def autopilot_draft(autopilot_config: Any, user: Any) -> Any:
    """One slot's output: a draft, and the candidate a person will judge.

    Built through `run_config` rather than by hand, so the wiring under test is
    the wiring that runs — a draft constructed directly would prove only that
    the fixture can set a foreign key.
    """
    from products.models import AutopilotDraft
    from products.services.autopilot import run_config
    from taste.services.profiles import activate, create_profile

    workspace = autopilot_config.product.workspace
    activate(create_profile(workspace=workspace))

    run_config(autopilot_config)
    draft = AutopilotDraft.objects.filter(product=autopilot_config.product).first()
    assert draft is not None, "the run produced no draft to review"
    return draft


class TestTheBoundary:
    def test_autopilot_cannot_reach_create_post_at_all(self) -> None:
        """The structural half of the gate.

        Checked on the module's **namespace**, not its source text: a
        substring search would be satisfied by renaming a comment, and would
        break on one that merely mentions the function. What matters is
        whether the name is reachable — an unimported function cannot be
        called.
        """
        from products.services import autopilot

        assert not hasattr(autopilot, "create_post"), (
            "autopilot can reach `create_post` directly. Materialisation belongs to "
            "`taste.services.candidates.materialise`, behind an approved Decision (C-01)."
        )

    def test_the_auto_calendar_branch_is_gone(self) -> None:
        """Retired, not merely flagged off (C-01).

        Phase 2 neutralised this behind `COLLABORATION_V2`; leaving the branch
        in place would mean a flag flip could still put generated content on a
        calendar at 03:00 with nobody having read it.
        """
        from products.services import autopilot

        source = inspect.getsource(autopilot)
        finish = inspect.getsource(autopilot._finish)

        # The *landing* is what selected the branch, so its absence is what
        # says the branch is gone.
        assert "AutopilotLanding.AUTO_CALENDAR" not in source

        # `config.auto_approve` is still *read* — deliberately. A user who
        # switched it on and finds their drafts waiting gets the reason in the
        # run record rather than silence. What must not exist is the run
        # approving on their behalf.
        assert "approve_draft(" not in finish, (
            "a run still approves its own drafts; nothing reaches a calendar without a person (L-2)"
        )


class TestDraftsBecomeCandidates:
    def test_a_generated_draft_carries_a_candidate_awaiting_review(
        self, autopilot_draft: Any
    ) -> None:
        assert autopilot_draft.candidate_id is not None
        assert autopilot_draft.candidate.state == CandidateState.PENDING_REVIEW

    def test_approving_writes_a_decision_before_any_post_exists(
        self, autopilot_draft: Any, user: Any
    ) -> None:
        from products.services.autopilot import approve_draft

        approve_draft(autopilot_draft, actor=user)

        candidate = ContentCandidate.objects.get(pk=autopilot_draft.candidate_id)
        decision = Decision.objects.get(candidate=candidate)
        assert decision.verdict in (Verdict.ACCEPTED, Verdict.ACCEPTED_WITH_EDITS)
        assert decision.actor_id == user.pk
        assert candidate.post_id is not None

    def test_the_approver_is_named_on_the_decision(self, autopilot_draft: Any, user: Any) -> None:
        # An approval with nobody's name on it is what L-2 exists to prevent.
        from products.services.autopilot import approve_draft

        approve_draft(autopilot_draft, actor=user)

        assert Decision.objects.get(candidate_id=autopilot_draft.candidate_id).actor_id == user.pk

    def test_rejecting_records_a_reason_rather_than_deleting_the_evidence(
        self, autopilot_draft: Any, user: Any
    ) -> None:
        from products.services.autopilot import reject_draft

        reject_draft(autopilot_draft, actor=user, reason_code="topic_not_relevant")

        decision = Decision.objects.get(candidate_id=autopilot_draft.candidate_id)
        assert decision.verdict == Verdict.REJECTED
        assert decision.reason_code == "topic_not_relevant"

    def test_the_draft_and_its_candidate_never_disagree(
        self, autopilot_draft: Any, user: Any
    ) -> None:
        """The one risk of keeping both rows: two state machines drifting.

        `approve_draft` writes both in one transaction, and this is what keeps
        that true.
        """
        from products.models import AutopilotDraftStatus
        from products.services.autopilot import approve_draft

        approve_draft(autopilot_draft, actor=user)

        autopilot_draft.refresh_from_db()
        assert autopilot_draft.status == AutopilotDraftStatus.SCHEDULED
        assert autopilot_draft.candidate.state == CandidateState.APPROVED


class TestNoPostWithoutADecision:
    """P5-G3, stated over the corpus rather than per call site."""

    def test_every_autopilot_post_traces_to_an_approving_decision(
        self, autopilot_draft: Any, user: Any
    ) -> None:
        from products.models import AutopilotDraft
        from products.services.autopilot import approve_draft

        approve_draft(autopilot_draft, actor=user)

        for draft in AutopilotDraft.objects.filter(post__isnull=False):
            candidate = draft.candidate
            assert candidate is not None, f"draft {draft.pk} made a post with no candidate"
            assert candidate.decisions.filter(
                verdict__in=(Verdict.ACCEPTED, Verdict.ACCEPTED_WITH_EDITS)
            ).exists(), f"draft {draft.pk} made a post with no approving decision"


class TestTheRunNeedsATasteProfile:
    def test_a_run_without_a_profile_spends_nothing_and_says_why(
        self, autopilot_config: Any
    ) -> None:
        """Refused up front, the same shape as `ensure_generation_ready`.

        Since a draft is reviewed as a candidate and a candidate is always
        judged against a profile, a workspace that has not described its brand
        would get drafts it could never approve — having paid for every one.
        """
        from billing.models import CreditLedger
        from products.models import AutopilotDraft, AutopilotJobStatus
        from products.services.autopilot import run_config
        from taste.models import TasteProfile

        workspace = autopilot_config.product.workspace
        TasteProfile.objects.filter(workspace=workspace).update(is_active=False)
        spent_before = CreditLedger.objects.filter(delta__lt=0).count()

        job = run_config(autopilot_config)

        assert job.status == AutopilotJobStatus.FAILED
        assert job.detail["reason"] == "no_taste_profile"
        assert not AutopilotDraft.objects.exists()
        assert CreditLedger.objects.filter(delta__lt=0).count() == spent_before


class TestGenerationProvenance:
    """P5-15 — every generation records what produced it.

    Without the versions, a shift in acceptance rate has five candidate causes
    — the model, the prompt, the taste profile, the rule set, the content — and
    no way to tell them apart. Attribution is the whole point of the loop.
    """

    def test_a_generation_records_the_taste_version_it_ran_under(
        self, autopilot_draft: Any
    ) -> None:
        from taste.models import TasteProfile

        profile = TasteProfile.objects.get(
            workspace=autopilot_draft.product.workspace, is_active=True
        )

        assert autopilot_draft.generation.taste_profile_version == profile.version

    def test_a_generation_records_the_prompt_template_version(self, autopilot_draft: Any) -> None:
        from taste.services.prompting import PROMPT_TEMPLATE_VERSION

        assert autopilot_draft.generation.prompt_template_version == PROMPT_TEMPLATE_VERSION

    def test_a_generation_records_its_model_and_cost(self, autopilot_draft: Any) -> None:
        # Provider, model and token counts were already recorded; this is what
        # `Decision.model_identity` joins them into.
        generation = autopilot_draft.generation

        assert generation.model_identity
        assert generation.credits_charged > 0

    def test_the_decision_carries_the_model_that_produced_the_candidate(
        self, autopilot_draft: Any, user: Any
    ) -> None:
        from products.services.autopilot import approve_draft
        from taste.models import Decision

        approve_draft(autopilot_draft, actor=user)

        decision = Decision.objects.get(candidate_id=autopilot_draft.candidate_id)
        assert decision.model_identity == autopilot_draft.generation.model_identity
