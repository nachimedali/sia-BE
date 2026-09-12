"""Trend stage 6 — ranked clusters filtered by topic posture (C-10, P5-14)."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.django_db


class _Cluster:
    """The three fields stage 6 reads. A stand-in rather than a real row: the
    filtering is the subject, and building a corpus would test the fixture."""

    def __init__(self, pk: int, label: str, score: float) -> None:
        self.pk = pk
        self.label = label
        self.composite_score = score
        self.platform = "instagram"


@pytest.fixture
def profile(workspace: Any) -> Any:
    from taste.services.profiles import activate, create_profile

    return activate(create_profile(workspace=workspace))


def _posture(profile: Any, **posture: Any) -> Any:
    profile.topic_posture = posture
    profile.save(update_fields=["topic_posture"])
    return profile


class TestPosture:
    def test_an_avoided_topic_is_removed_not_demoted(self, profile: Any) -> None:
        """A stated refusal is a refusal. Ranking it third is how a workspace
        ends up declining the same suggestion every week."""
        from taste.services.topics import eligible_clusters

        _posture(profile, avoid=["whisky"])
        clusters = [_Cluster(1, "whisky pairings", 0.9), _Cluster(2, "morning rituals", 0.5)]

        assert [c.pk for c in eligible_clusters(profile, clusters)] == [2]

    def test_a_favoured_topic_sorts_first_even_below_rank(self, profile: Any) -> None:
        # Favour is a preference, not a filter — which is why it reorders
        # rather than removing everything else.
        from taste.services.topics import eligible_clusters

        _posture(profile, favour=["ceramics"])
        clusters = [_Cluster(1, "loud trend", 0.95), _Cluster(2, "ceramics at home", 0.2)]

        assert [c.pk for c in eligible_clusters(profile, clusters)] == [2, 1]

    def test_an_unfavoured_topic_is_still_offered(self, profile: Any) -> None:
        from taste.services.topics import eligible_clusters

        _posture(profile, favour=["ceramics"])
        clusters = [_Cluster(1, "something else", 0.4)]

        assert len(eligible_clusters(profile, clusters)) == 1

    def test_rank_orders_what_posture_does_not(self, profile: Any) -> None:
        from taste.services.topics import eligible_clusters

        clusters = [_Cluster(1, "quiet", 0.2), _Cluster(2, "loud", 0.8)]

        assert [c.pk for c in eligible_clusters(profile, clusters)] == [2, 1]

    def test_matching_respects_word_boundaries(self, profile: Any) -> None:
        """A brand avoiding "gin" should not lose every "beginning" — a
        posture that silently drops the wrong clusters is worse than none,
        because the user cannot see what they are missing."""
        from taste.services.topics import eligible_clusters

        _posture(profile, avoid=["gin"])
        clusters = [_Cluster(1, "beginning of spring", 0.5)]

        assert len(eligible_clusters(profile, clusters)) == 1

    def test_the_corpus_is_capped(self, profile: Any) -> None:
        from taste.services.topics import MAX_CONSIDERED, eligible_clusters

        clusters = [_Cluster(index, f"topic {index}", 0.5) for index in range(MAX_CONSIDERED + 10)]

        assert len(eligible_clusters(profile, clusters)) == MAX_CONSIDERED


class TestCandidatesFromTrends:
    def test_clusters_become_candidates_in_the_queue(self, workspace: Any, profile: Any) -> None:
        from taste.models import CandidateState
        from taste.services.topics import candidates_from_trends

        made = candidates_from_trends(
            workspace=workspace,
            profile=profile,
            clusters=[_Cluster(1, "morning rituals", 0.8)],
        )

        assert [c.state for c in made] == [CandidateState.PENDING_REVIEW]

    def test_the_reason_travels_with_the_proposal(self, workspace: Any, profile: Any) -> None:
        # A proposal that cannot explain itself gets rejected on suspicion,
        # which teaches the taste model nothing useful.
        from taste.services.topics import candidates_from_trends

        made = candidates_from_trends(
            workspace=workspace, profile=profile, clusters=[_Cluster(7, "morning rituals", 0.8)]
        )

        assert made[0].payload["trend"]["cluster"] == 7

    def test_a_trend_does_not_excuse_a_policy_violation(self, workspace: Any, profile: Any) -> None:
        """**Screening still applies** (P5-G1). A trend is a reason to write
        something, never a reason to skip the brand's own rules."""
        from taste.models import CandidateState
        from taste.services.topics import candidates_from_trends

        profile.hard_constraints = {"banned_phrases": ["whisky"]}
        profile.save(update_fields=["hard_constraints"])

        made = candidates_from_trends(
            workspace=workspace, profile=profile, clusters=[_Cluster(1, "whisky pairings", 0.9)]
        )

        assert made[0].state == CandidateState.SCREENED_OUT

    def test_the_limit_is_honoured(self, workspace: Any, profile: Any) -> None:
        from taste.services.topics import candidates_from_trends

        clusters = [_Cluster(index, f"topic {index}", 0.5) for index in range(10)]

        assert (
            len(
                candidates_from_trends(
                    workspace=workspace, profile=profile, clusters=clusters, limit=2
                )
            )
            == 2
        )
