"""The taste and approval-queue endpoints (P5-16, and the gates from outside).

The queue is the daily habit surface — the reason someone opens the app — so
these assert the two things a reviewer depends on: that nothing they should not
see is reachable, and that every verdict leaves a record.
"""

from __future__ import annotations

from typing import Any

import pytest

from taste.models import CandidateState, Decision, TasteProfile, Verdict

pytestmark = pytest.mark.django_db

PROFILES = "/api/v1/taste-profiles/"
CANDIDATES = "/api/v1/candidates/"


@pytest.fixture
def profile(workspace: Any) -> TasteProfile:
    from taste.services.profiles import activate, create_profile

    return activate(
        create_profile(workspace=workspace, hard_constraints={"banned_phrases": ["cheap"]})
    )


def _candidate(workspace: Any, profile: Any, body: str = "A quiet shelf of bowls.") -> Any:
    from taste.services.candidates import propose

    return propose(workspace=workspace, profile=profile, payload={"master_body": body})


class TestProfiles:
    def test_creating_a_profile_mints_the_next_version(
        self, auth_client: Any, workspace: Any
    ) -> None:
        first = auth_client.post(PROFILES, {"voice": {"tone": "warm"}}, format="json")
        second = auth_client.post(PROFILES, {"voice": {"tone": "dry"}}, format="json")

        assert first.json()["version"] == 1
        assert second.json()["version"] == 2

    def test_a_new_profile_is_not_active_until_someone_says_so(
        self, auth_client: Any, workspace: Any
    ) -> None:
        # Creating a profile and having it take effect are two decisions, and
        # conflating them would let a half-written brand start generating.
        response = auth_client.post(PROFILES, {"voice": {"tone": "warm"}}, format="json")

        assert response.json()["is_active"] is False

    def test_activating_deactivates_the_previous_one(
        self, auth_client: Any, workspace: Any
    ) -> None:
        first = auth_client.post(PROFILES, {}, format="json").json()
        second = auth_client.post(PROFILES, {}, format="json").json()

        auth_client.post(f"{PROFILES}{first['id']}/activate/")
        auth_client.post(f"{PROFILES}{second['id']}/activate/")

        assert TasteProfile.objects.filter(workspace=workspace, is_active=True).count() == 1

    def test_the_version_cannot_be_chosen_by_the_client(
        self, auth_client: Any, workspace: Any
    ) -> None:
        # It is the one fact attribution depends on.
        response = auth_client.post(PROFILES, {"version": 99}, format="json")

        assert response.json()["version"] == 1

    def test_an_unknown_constraint_is_a_400(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(PROFILES, {"hard_constraints": {"vibes": True}}, format="json")

        assert response.status_code == 400

    def test_the_active_profile_is_404_before_there_is_one(
        self, auth_client: Any, workspace: Any
    ) -> None:
        """ "This workspace has not described its brand" and "here is a blank
        brand" are different answers, and only the first is true."""
        assert auth_client.get(f"{PROFILES}active/").status_code == 404

    def test_completeness_is_served_so_onboarding_can_show_progress(
        self, auth_client: Any, profile: Any
    ) -> None:
        body = auth_client.get(f"{PROFILES}active/").json()

        assert 0.0 < body["completeness"] <= 1.0

    def test_another_workspaces_profile_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from taste.services.profiles import create_profile
        from workspaces.services.provisioning import provision_workspace

        theirs = create_profile(workspace=provision_workspace(other_user, name="Rival"))

        assert auth_client.get(f"{PROFILES}{theirs.id}/").status_code == 404


class TestTheQueue:
    def test_it_lists_what_is_waiting(self, auth_client: Any, workspace: Any, profile: Any) -> None:
        _candidate(workspace, profile)

        body = auth_client.get(CANDIDATES).json()

        assert len(body["results"]) == 1

    def test_a_screened_candidate_is_unreachable_by_id(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        """P5-G1 from outside. Filtered in the queryset, so `.get()` misses it
        too — a filter that only narrowed list pages would be defeated by
        anyone who knew an id."""
        screened = _candidate(workspace, profile, "Our cheap new glaze.")

        assert screened.state == CandidateState.SCREENED_OUT
        assert auth_client.get(f"{CANDIDATES}{screened.id}/").status_code == 404

    def test_an_unknown_state_filter_is_a_400(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        assert auth_client.get(f"{CANDIDATES}?state=vibes").status_code == 400

    def test_another_workspaces_candidate_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from taste.services.profiles import activate, create_profile
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival")
        their_profile = activate(create_profile(workspace=theirs))
        candidate = _candidate(theirs, their_profile)

        assert auth_client.get(f"{CANDIDATES}{candidate.id}/").status_code == 404


class TestDecidingThroughTheApi:
    def test_approving_materialises_and_records(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile)

        response = auth_client.post(f"{CANDIDATES}{candidate.id}/approve/", {}, format="json")

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == CandidateState.APPROVED
        assert body["post"] is not None
        assert body["decisions"][0]["verdict"] == Verdict.ACCEPTED

    def test_approving_with_edits_is_recorded_as_such(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile, "Original")

        response = auth_client.post(
            f"{CANDIDATES}{candidate.id}/approve/",
            {"edited_payload": {"master_body": "Edited"}},
            format="json",
        )

        assert response.json()["decisions"][0]["verdict"] == Verdict.ACCEPTED_WITH_EDITS

    def test_rejecting_without_a_reason_is_a_400(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile)

        response = auth_client.post(f"{CANDIDATES}{candidate.id}/reject/", {}, format="json")

        assert response.status_code == 400

    def test_rejecting_with_an_invented_reason_is_a_400(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile)

        response = auth_client.post(
            f"{CANDIDATES}{candidate.id}/reject/",
            {"reason_code": "i_just_dont_like_it"},
            format="json",
        )

        assert response.status_code == 400

    def test_rejecting_then_regenerating_carries_the_reason(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile)
        auth_client.post(
            f"{CANDIDATES}{candidate.id}/reject/",
            {"reason_code": "weak_hook"},
            format="json",
        )

        response = auth_client.post(f"{CANDIDATES}{candidate.id}/regenerate/", {}, format="json")

        assert response.status_code == 201
        assert response.json()["parent_reason_code"] == "weak_hook"

    def test_the_edit_diff_is_not_on_the_queue_payload(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        """P5-13 — the most revealing field here does not ride along on every
        render. A field that leaks by being on a list nobody thought about is
        the ordinary way this goes wrong."""
        candidate = _candidate(workspace, profile, "Original")
        auth_client.post(
            f"{CANDIDATES}{candidate.id}/approve/",
            {"edited_payload": {"master_body": "Edited"}},
            format="json",
        )

        body = auth_client.get(CANDIDATES).json()

        assert "edit_diff" not in str(body)

    def test_the_edit_diff_is_readable_by_an_admin(
        self, auth_client: Any, workspace: Any, profile: Any
    ) -> None:
        candidate = _candidate(workspace, profile, "Original")
        auth_client.post(
            f"{CANDIDATES}{candidate.id}/approve/",
            {"edited_payload": {"master_body": "Edited"}},
            format="json",
        )

        response = auth_client.get(f"{CANDIDATES}{candidate.id}/edit-diff/")

        assert response.status_code == 200
        assert response.json()["diffs"][0]["diff"]["master_body"] == ["Original", "Edited"]

    def test_a_viewer_cannot_read_the_edit_diff(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any
    ) -> None:
        from taste.services.profiles import activate, create_profile

        their_profile = activate(create_profile(workspace=advanced_workspace))
        candidate = _candidate(advanced_workspace, their_profile)

        response = client_as(viewer_user).get(f"{CANDIDATES}{candidate.id}/edit-diff/")

        assert response.status_code == 403

    def test_every_decision_records_its_actor(
        self, auth_client: Any, workspace: Any, profile: Any, user: Any
    ) -> None:
        candidate = _candidate(workspace, profile)
        auth_client.post(f"{CANDIDATES}{candidate.id}/approve/", {}, format="json")

        assert Decision.objects.get(candidate=candidate).actor_id == user.pk


class TestTheFlag:
    def test_the_surfaces_are_404_with_the_flag_off(self, auth_client: Any, workspace: Any) -> None:
        """Flag off is pre-phase behaviour: before Phase 5 these routes did not
        exist, so 404 is the honest answer."""
        from billing.models import FeatureFlag
        from billing.services.flags import TASTE_V5

        FeatureFlag.objects.create(organization=workspace.organization, key=TASTE_V5, enabled=False)

        assert auth_client.get(CANDIDATES).status_code == 404
        assert auth_client.get(PROFILES).status_code == 404
