"""Consent (P8-05, P8-07) — opt-in, append-only, and bound to the terms read.

What is being protected: a workspace's data leaves its tenant boundary only as
part of an aggregate, and only while the workspace has agreed to the terms that
are currently published. Every rule below is a way that agreement could be
assumed rather than given.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from benchmarks.models import (
    BenchmarkObservation,
    BenchmarkRun,
    CohortBenchmark,
    ConsentAction,
    ConsentPolicy,
    ConsentRecord,
)
from benchmarks.services import consent, projection
from billing.models import FeatureFlag
from billing.services.flags import COHORT_V8
from common.records import AppendOnlyError
from workspaces.models import Membership, Role, permissions_for
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

GRANT = "benchmark-participation-grant"
REVOKE = "benchmark-participation-revoke"
PARTICIPATION = "benchmark-participation"


@pytest.fixture
def ready(workspace: Any, leaf: Any, policy: ConsentPolicy, cohort_on: None) -> Any:
    """A workspace that could opt in: a category, a published policy, flag on."""
    workspace.category = leaf
    workspace.save(update_fields=["category"])
    return workspace


class TestTheService:
    def test_a_workspace_contributes_only_after_granting(self, ready: Any, user: Any) -> None:
        assert consent.is_contributing(ready) is False

        consent.grant(ready, actor=user, policy_version=1, market="pt")

        ready.refresh_from_db()
        assert consent.is_contributing(ready) is True
        assert ready.market == "PT", "a market code is stored in one canonical form"

    def test_revoking_is_a_new_timestamped_row_not_an_edit(self, ready: Any, user: Any) -> None:
        """A-13. The grant stays exactly as it was written; the revocation is
        its own fact with its own time."""
        granted = consent.grant(ready, actor=user, policy_version=1, market="PT")
        revoked = consent.revoke(ready, actor=user)

        assert revoked.pk != granted.pk
        assert revoked.action == ConsentAction.REVOKED
        assert revoked.recorded_at is not None
        assert revoked.recorded_at >= granted.recorded_at
        granted.refresh_from_db()
        assert granted.action == ConsentAction.GRANTED
        assert consent.is_contributing(ready) is False

    def test_records_cannot_be_edited_or_deleted(self, ready: Any, user: Any) -> None:
        record = consent.grant(ready, actor=user, policy_version=1, market="PT")

        record.action = ConsentAction.REVOKED
        with pytest.raises(AppendOnlyError):
            record.save()
        with pytest.raises(AppendOnlyError):
            record.delete()

    def test_granting_twice_on_the_same_terms_writes_one_row(self, ready: Any, user: Any) -> None:
        consent.grant(ready, actor=user, policy_version=1, market="PT")
        consent.grant(ready, actor=user, policy_version=1, market="PT")

        assert ConsentRecord.objects.filter(workspace=ready).count() == 1

    def test_new_terms_stop_contribution_until_consent_is_given_again(
        self, ready: Any, user: Any, policy: ConsentPolicy
    ) -> None:
        """Agreement to version 1 is not agreement to version 2. Carrying it
        forward would mean contributing under terms nobody at the workspace
        has read."""
        consent.grant(ready, actor=user, policy_version=1, market="PT")
        ConsentPolicy.objects.create(version=2, summary="Revised terms.")

        assert consent.is_contributing(ready) is False
        assert consent.status(ready).requires_reconsent is True

        consent.grant(ready, actor=user, policy_version=2, market="PT")
        assert consent.is_contributing(ready) is True

    def test_revoking_stops_future_contribution_immediately(
        self, ready: Any, user: Any, vertical: Any
    ) -> None:
        """P8-07. The workspace's projected rows go at once — not at the next
        nightly run — so nothing computed after the revocation includes them."""
        consent.grant(ready, actor=user, policy_version=1, market="PT")
        token = projection.contributor_token(ready.pk)
        BenchmarkObservation.objects.create(
            contributor=token,
            vertical=vertical,
            market="PT",
            platform="instagram",
            size_band="1000_10000",
            post_format="FEED",
            posting_window="evening",
            published_on="2026-05-01",
            engagement_rate=0.04,
        )

        consent.revoke(ready, actor=user)

        assert not BenchmarkObservation.objects.filter(contributor=token).exists()

    def test_revoking_does_not_recompute_what_was_already_published(
        self, ready: Any, user: Any, vertical: Any
    ) -> None:
        """P8-07, and the consent copy has to say so: a benchmark computed while
        the workspace contributed stays as it was."""
        consent.grant(ready, actor=user, policy_version=1, market="PT")
        run = BenchmarkRun.objects.create(
            window_start="2026-03-01",
            window_end="2026-06-01",
            min_workspaces=8,
            min_posts=200,
            max_posts_per_contributor=50,
        )
        published = CohortBenchmark.objects.create(
            run=run,
            vertical=vertical,
            market="PT",
            platform="instagram",
            size_band="1000_10000",
            post_format="FEED",
            contributors=9,
            posts=240,
            sufficient=True,
            metrics={"engagement_rate": {"median": 0.041, "p25": 0.02, "p75": 0.06, "posts": 240}},
        )

        consent.revoke(ready, actor=user)

        published.refresh_from_db()
        assert published.metrics["engagement_rate"]["median"] == 0.041
        assert BenchmarkRun.objects.count() == 1


class TestTheApi:
    def test_the_participation_state_is_readable(self, auth_client: Any, ready: Any) -> None:
        response = auth_client.get(reverse(PARTICIPATION))

        assert response.status_code == 200
        body = response.json()
        assert body["contributing"] is False
        assert body["policy"]["version"] == 1
        assert body["vertical"]["name"] == "Food & Drink", "the vertical is the category's root"
        assert {"code": "PT", "name": "Portugal"} in body["markets"]

    def test_an_admin_grants_and_the_state_says_so(self, auth_client: Any, ready: Any) -> None:
        response = auth_client.post(
            reverse(GRANT), {"policy_version": 1, "market": "FR"}, format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["contributing"] is True
        assert body["market"] == "FR"
        assert body["consent"]["policy_version"] == 1
        assert body["history"][0]["action"] == ConsentAction.GRANTED

    def test_an_admin_revokes_and_the_history_keeps_both(
        self, auth_client: Any, ready: Any
    ) -> None:
        auth_client.post(reverse(GRANT), {"policy_version": 1, "market": "FR"}, format="json")

        response = auth_client.post(reverse(REVOKE), {}, format="json")

        assert response.status_code == 200
        body = response.json()
        assert body["contributing"] is False
        assert [row["action"] for row in body["history"]] == [
            ConsentAction.REVOKED,
            ConsentAction.GRANTED,
        ]
        assert all(row["recorded_at"] for row in body["history"])

    def test_no_published_terms_means_no_consent_can_be_given(
        self, auth_client: Any, workspace: Any, leaf: Any, cohort_on: None
    ) -> None:
        """The legal gate, held by the system rather than by a checklist: until
        a policy exists, there is nothing to agree to."""
        workspace.category = leaf
        workspace.save(update_fields=["category"])

        response = auth_client.post(
            reverse(GRANT), {"policy_version": 1, "market": "PT"}, format="json"
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "consent_policy_unavailable"

    def test_agreeing_to_superseded_terms_is_refused(self, auth_client: Any, ready: Any) -> None:
        ConsentPolicy.objects.create(version=2, summary="Revised terms.")

        response = auth_client.post(
            reverse(GRANT), {"policy_version": 1, "market": "PT"}, format="json"
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "consent_policy_outdated"
        assert error["detail"]["current_version"] == 2

    def test_a_workspace_with_no_category_cannot_opt_in(
        self, auth_client: Any, workspace: Any, policy: ConsentPolicy, cohort_on: None
    ) -> None:
        """Without a vertical there is no cohort to join. Accepting the consent
        anyway would record an agreement that can never do anything."""
        response = auth_client.post(
            reverse(GRANT), {"policy_version": 1, "market": "PT"}, format="json"
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "benchmark_profile_incomplete"
        assert error["detail"]["missing"] == ["category"]

    @pytest.mark.parametrize("market", ["", "XX", "Portugal", "PRT", None])
    def test_a_market_must_be_a_real_country_code(
        self, auth_client: Any, ready: Any, market: str | None
    ) -> None:
        response = auth_client.post(
            reverse(GRANT), {"policy_version": 1, "market": market}, format="json"
        )

        assert response.status_code == 400
        assert not ConsentRecord.objects.exists()

    def test_revoking_without_a_grant_is_a_conflict(self, auth_client: Any, ready: Any) -> None:
        response = auth_client.post(reverse(REVOKE), {}, format="json")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "consent_not_granted"

    def test_only_an_admin_may_grant_or_revoke(
        self, ready: Any, other_user: Any, api_client_for: Any
    ) -> None:
        """403: a role failure, and no upgrade fixes it."""
        Membership.objects.create(
            user=other_user,
            workspace=ready,
            role=Role.EDITOR,
            permissions=sorted(permissions_for(Role.EDITOR)),
        )
        client = api_client_for(other_user, ready)

        assert client.get(reverse(PARTICIPATION)).status_code == 200
        grant = client.post(reverse(GRANT), {"policy_version": 1, "market": "PT"}, format="json")
        revoke = client.post(reverse(REVOKE), {}, format="json")

        assert grant.status_code == 403
        assert revoke.status_code == 403

    def test_flag_off_is_the_route_not_existing(
        self, auth_client: Any, workspace: Any, policy: ConsentPolicy
    ) -> None:
        """COHORT_V8 ships off. Off is pre-phase behaviour: 404, not an error."""
        assert auth_client.get(reverse(PARTICIPATION)).status_code == 404
        assert (
            auth_client.post(
                reverse(GRANT), {"policy_version": 1, "market": "PT"}, format="json"
            ).status_code
            == 404
        )

    def test_an_organization_row_can_switch_it_off_for_one_customer(
        self, auth_client: Any, ready: Any
    ) -> None:
        FeatureFlag.objects.create(organization=ready.organization, key=COHORT_V8, enabled=False)

        assert auth_client.get(reverse(PARTICIPATION)).status_code == 404

    def test_another_organizations_workspace_is_404(self, auth_client: Any, ready: Any) -> None:
        stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
        theirs = provision_workspace(stranger, name="Another Company")

        for name in (PARTICIPATION, GRANT, REVOKE):
            method = auth_client.get if name == PARTICIPATION else auth_client.post
            response = method(reverse(name), HTTP_X_WORKSPACE_ID=str(theirs.pk))
            assert response.status_code == 404, name

    def test_another_workspace_in_the_same_organization_is_404(
        self, auth_client: Any, ready: Any
    ) -> None:
        """Same company, a brand this user has no membership in."""
        from workspaces.models import Workspace

        sibling = Workspace.objects.create(
            organization=ready.organization, name="Sibling Brand", slug="sibling-brand"
        )

        response = auth_client.get(reverse(PARTICIPATION), HTTP_X_WORKSPACE_ID=str(sibling.pk))

        assert response.status_code == 404
