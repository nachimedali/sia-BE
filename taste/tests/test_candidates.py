"""The candidate lifecycle and the decision log (P5-06…P5-13, gates G1-G3).

`ContentCandidate` sits **upstream of and distinct from `Post`**. The existing
`Generation` stays the record of a provider call; this is the reviewable thing
a person says yes or no to, and nothing reaches the publish pipeline without an
`approved` one (C-01, L-2).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from taste.models import CandidateState, ContentCandidate, Decision, TasteProfile, Verdict

pytestmark = pytest.mark.django_db


@pytest.fixture
def profile(workspace: Any) -> TasteProfile:
    from taste.services.profiles import activate, create_profile

    created = create_profile(workspace=workspace, hard_constraints={"banned_phrases": ["cheap"]})
    return activate(created)


def _propose(workspace: Any, profile: TasteProfile, body: str = "A quiet shelf of bowls.") -> Any:
    from taste.services.candidates import propose

    return propose(workspace=workspace, profile=profile, payload={"master_body": body})


class TestScreeningGate:
    """P5-G1 — a hard-constraint violation never appears in the queue."""

    def test_a_clean_candidate_reaches_review(self, workspace: Any, profile: Any) -> None:
        assert _propose(workspace, profile).state == CandidateState.PENDING_REVIEW

    def test_a_violating_candidate_is_screened_out(self, workspace: Any, profile: Any) -> None:
        candidate = _propose(workspace, profile, "Our cheap new glaze.")

        assert candidate.state == CandidateState.SCREENED_OUT

    def test_a_screened_candidate_never_appears_in_the_queue(
        self, workspace: Any, profile: Any
    ) -> None:
        """The gate itself. Asserted on the **queryset the queue is built
        from**, not on rendered output: a serializer that omitted the row still
        loaded it and still counted it in a page total."""
        from taste.services.candidates import review_queue

        _propose(workspace, profile, "Our cheap new glaze.")
        clean = _propose(workspace, profile)

        assert list(review_queue(workspace)) == [clean]

    def test_the_screening_reason_is_recorded_for_measurement(
        self, workspace: Any, profile: Any
    ) -> None:
        """P5-08 — logged, never surfaced, so the violation *rate* is
        measurable. A rising rate signals prompt or profile drift."""
        candidate = _propose(workspace, profile, "Our cheap new glaze.")

        assert "banned_phrases" in candidate.screened_reason

    def test_the_violation_rate_is_queryable(self, workspace: Any, profile: Any) -> None:
        from taste.services.candidates import screening_violation_rate

        _propose(workspace, profile, "Our cheap new glaze.")
        _propose(workspace, profile)
        _propose(workspace, profile)

        assert screening_violation_rate(workspace) == pytest.approx(1 / 3)

    def test_the_rate_is_none_rather_than_zero_with_nothing_to_measure(
        self, workspace: Any, profile: Any
    ) -> None:
        # Part 7 rule 12's habit applied here: "no candidates yet" and "no
        # violations" are different facts, and 0.0 would state the second.
        from taste.services.candidates import screening_violation_rate

        assert screening_violation_rate(workspace) is None


class TestTheProfileTravels:
    def test_a_candidate_records_the_profile_that_produced_it(
        self, workspace: Any, profile: Any
    ) -> None:
        # Non-null from the first migration — there is no unversioned path to
        # skip to under deadline (P5-01's risk row).
        assert _propose(workspace, profile).taste_profile_id == profile.pk

    def test_another_brands_profile_cannot_judge_this_workspace(
        self, workspace: Any, other_user: Any
    ) -> None:
        from django.core.exceptions import ValidationError

        from taste.services.profiles import activate, create_profile
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        their_profile = activate(create_profile(workspace=theirs))

        candidate = ContentCandidate(workspace=workspace, taste_profile=their_profile)
        with pytest.raises(ValidationError):
            candidate.full_clean(exclude=["product", "generation", "campaign", "ruleset", "post"])


class TestDecisions:
    def test_approving_records_a_decision(self, workspace: Any, profile: Any, user: Any) -> None:
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile)

        approve(candidate, actor=user)

        decision = Decision.objects.get(candidate=candidate)
        assert decision.verdict == Verdict.ACCEPTED

    def test_a_decision_carries_the_versions_that_produced_the_candidate(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        """Attribution is impossible without all of them: a change in
        acceptance rate otherwise has five causes and no way to separate
        them (P5-11)."""
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile)

        approve(candidate, actor=user)

        decision = Decision.objects.get(candidate=candidate)
        assert decision.taste_profile_version == profile.version

    def test_a_decision_stores_what_the_reviewer_actually_saw(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        # Stored, not read back off the candidate: an edit afterwards would
        # otherwise rewrite history.
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile, "As shown")
        approve(candidate, actor=user)

        assert Decision.objects.get(candidate=candidate).payload_as_shown["master_body"] == (
            "As shown"
        )

    def test_rejecting_needs_a_reason_from_the_vocabulary(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from rest_framework.exceptions import ValidationError

        from taste.services.candidates import reject

        candidate = _propose(workspace, profile)

        with pytest.raises(ValidationError):
            reject(candidate, actor=user, reason_code="i_just_dont_like_it")

    def test_a_rejection_records_its_code(self, workspace: Any, profile: Any, user: Any) -> None:
        """Structured codes are what make rejections aggregable — *"63% of your
        rejections were off_brand_voice"* routes to a profile revision, and
        free text cannot do that (P5-12)."""
        from taste.services.candidates import reject

        candidate = _propose(workspace, profile)

        reject(candidate, actor=user, reason_code="off_brand_voice")

        assert Decision.objects.get(candidate=candidate).reason_code == "off_brand_voice"
        candidate.refresh_from_db()
        assert candidate.state == CandidateState.REJECTED

    def test_accepting_with_edits_stores_the_diff(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        """**The highest-value signal in the system** (P5-13): what the model
        got wrong where the user cared enough to fix rather than discard."""
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile, "Original body")

        approve(candidate, actor=user, edited_payload={"master_body": "Edited body"})

        decision = Decision.objects.get(candidate=candidate)
        assert decision.verdict == Verdict.ACCEPTED_WITH_EDITS
        assert decision.edit_diff is not None
        assert decision.edit_diff["master_body"] == ["Original body", "Edited body"]

    def test_an_unedited_approval_is_not_recorded_as_edited(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        # Sending the payload back unchanged is an acceptance, not an edit —
        # counting it as one would poison the most valuable signal here.
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile, "Same")
        approve(candidate, actor=user, edited_payload={"master_body": "Same"})

        decision = Decision.objects.get(candidate=candidate)
        assert decision.verdict == Verdict.ACCEPTED
        assert decision.edit_diff is None

    def test_a_decision_cannot_be_edited(self, workspace: Any, profile: Any, user: Any) -> None:
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile)
        approve(candidate, actor=user)
        decision = Decision.objects.get(candidate=candidate)

        decision.reason_code = "rewritten"
        with pytest.raises(Exception):  # noqa: B017 — AppendOnly's own guard
            decision.save(update_fields=["reason_code"])


class TestMaterialisation:
    """P5-G3 — no path constructs a `Post` from a candidate without an
    approved `Decision`."""

    def test_approving_materialises_a_post(self, workspace: Any, profile: Any, user: Any) -> None:
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile, "Ready to go")

        approve(candidate, actor=user)

        candidate.refresh_from_db()
        assert candidate.post is not None
        assert candidate.post.master_body == "Ready to go"

    def test_an_edited_approval_materialises_the_edit_not_the_original(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile, "Original")

        approve(candidate, actor=user, edited_payload={"master_body": "Edited"})

        candidate.refresh_from_db()
        assert candidate.post is not None
        assert candidate.post.master_body == "Edited"

    def test_materialising_a_pending_candidate_directly_is_refused(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        """The boundary, asserted at the **service**, not by review (C-01).

        A caller that reached past `approve` to build the post itself is
        exactly the bypass L-2 forbids, and the refusal lives where any future
        caller inherits it."""
        from common.exceptions import StateConflict
        from taste.services.candidates import materialise

        candidate = _propose(workspace, profile)

        with pytest.raises(StateConflict):
            materialise(candidate, actor=user)

    def test_materialising_a_screened_candidate_is_refused(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from common.exceptions import StateConflict
        from taste.services.candidates import materialise

        candidate = _propose(workspace, profile, "Our cheap new glaze.")

        with pytest.raises(StateConflict):
            materialise(candidate, actor=user)

    def test_materialising_twice_does_not_make_two_posts(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        # A double-click, or a retried task, must not publish the same thing
        # twice — the candidate holds the post it made.
        from content.models import Post
        from taste.services.candidates import approve

        candidate = _propose(workspace, profile)
        approve(candidate, actor=user)
        approve(candidate, actor=user)

        assert Post.objects.filter(workspace=workspace).count() == 1

    def test_every_materialised_post_traces_to_an_approved_decision(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        """The gate stated as the invariant, over the whole corpus."""
        from taste.services.candidates import approve, reject

        approve(_propose(workspace, profile, "One"), actor=user)
        approve(_propose(workspace, profile, "Two"), actor=user)
        reject(_propose(workspace, profile, "Three"), actor=user, reason_code="weak_hook")

        for candidate in ContentCandidate.objects.filter(post__isnull=False):
            assert candidate.state == CandidateState.APPROVED
            assert candidate.decisions.filter(
                verdict__in=(Verdict.ACCEPTED, Verdict.ACCEPTED_WITH_EDITS)
            ).exists(), f"candidate {candidate.pk} made a post with no approving decision"


class TestRegeneration:
    """P5-G2 — the new prompt provably carries the parent's rejection reason."""

    def test_regenerating_carries_the_parent_reason(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from taste.services.candidates import regenerate, reject

        parent = _propose(workspace, profile)
        reject(parent, actor=user, reason_code="weak_hook")

        child = regenerate(parent, actor=user)

        assert child.parent_id == parent.pk
        assert child.parent_reason_code == "weak_hook"

    def test_the_reason_reaches_the_prompt(self, workspace: Any, profile: Any, user: Any) -> None:
        """**An explicit test, not a code comment** (P5-09). A regeneration
        that ignores why the first attempt failed reproduces the failure."""
        from taste.services.candidates import regenerate, reject
        from taste.services.prompting import build_prompt_context

        parent = _propose(workspace, profile)
        reject(parent, actor=user, reason_code="off_brand_voice")
        child = regenerate(parent, actor=user)

        context = build_prompt_context(child)

        assert "off_brand_voice" in str(context)

    def test_the_parent_is_marked_regenerating(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from taste.services.candidates import regenerate, reject

        parent = _propose(workspace, profile)
        reject(parent, actor=user, reason_code="weak_hook")
        regenerate(parent, actor=user)

        parent.refresh_from_db()
        assert parent.state == CandidateState.REGENERATING

    def test_regenerating_something_nobody_rejected_is_refused(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        # Without a rejection there is no reason to carry, which would make
        # the regeneration indistinguishable from a fresh generation.
        from common.exceptions import StateConflict
        from taste.services.candidates import regenerate

        with pytest.raises(StateConflict):
            regenerate(_propose(workspace, profile), actor=user)


class TestExpiry:
    def test_expiry_defaults_to_the_campaign_boundary(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        from planning.models import Campaign
        from taste.services.candidates import propose

        ends = timezone.now() + dt.timedelta(days=10)
        campaign = Campaign.objects.create(
            workspace=workspace, name="Spring", starts_at=timezone.now(), ends_at=ends
        )

        candidate = propose(
            workspace=workspace, profile=profile, payload={"master_body": "Hi"}, campaign=campaign
        )

        assert candidate.expires_at == ends

    def test_expiring_records_a_weak_signal_naming_nobody(
        self, workspace: Any, profile: Any
    ) -> None:
        """Expiry is weighted **below** explicit rejection (P5-10): a candidate
        nobody looked at says something about the queue, not the content — and
        no person made this call, so the decision names none."""
        from taste.services.candidates import expire_due

        candidate = _propose(workspace, profile)
        candidate.expires_at = timezone.now() - dt.timedelta(minutes=1)
        candidate.save(update_fields=["expires_at"])

        assert expire_due() == 1

        candidate.refresh_from_db()
        assert candidate.state == CandidateState.EXPIRED
        decision = Decision.objects.get(candidate=candidate)
        assert decision.verdict == Verdict.EXPIRED
        assert decision.actor is None

    def test_a_candidate_still_in_time_is_left_alone(self, workspace: Any, profile: Any) -> None:
        from taste.services.candidates import expire_due

        candidate = _propose(workspace, profile)
        candidate.expires_at = timezone.now() + dt.timedelta(days=1)
        candidate.save(update_fields=["expires_at"])

        assert expire_due() == 0

    def test_an_approved_candidate_never_expires(
        self, workspace: Any, profile: Any, user: Any
    ) -> None:
        # Only what is still awaiting review can time out.
        from taste.services.candidates import approve, expire_due

        candidate = _propose(workspace, profile)
        approve(candidate, actor=user)
        candidate.refresh_from_db()
        candidate.expires_at = timezone.now() - dt.timedelta(minutes=1)
        candidate.save(update_fields=["expires_at"])

        assert expire_due() == 0
