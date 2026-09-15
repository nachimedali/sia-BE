"""The benchmark read (P8-04, P8-05) — access for contributors, numbers only when earned."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from benchmarks.models import BenchmarkConfig, BenchmarkObservation, BenchmarkRun, CohortBenchmark
from benchmarks.services import consent, projection
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

URL = "benchmarks"


@pytest.fixture
def contributing(workspace: Any, user: Any, leaf: Any, policy: Any, cohort_on: None) -> Any:
    workspace.category = leaf
    workspace.save(update_fields=["category"])
    consent.grant(workspace, actor=user, policy_version=1, market="PT")
    workspace.refresh_from_db()
    return workspace


def own_observation(workspace: Any, vertical: Any, **key: Any) -> None:
    BenchmarkObservation.objects.create(
        contributor=projection.contributor_token(workspace.pk),
        vertical=vertical,
        market="PT",
        platform=key.get("platform", "instagram"),
        size_band=key.get("size_band", "1000_10000"),
        post_format=key.get("post_format", "FEED"),
        posting_window="evening",
        published_on="2026-05-01",
        engagement_rate=0.04,
    )


def run_with(vertical: Any, **cohort: Any) -> BenchmarkRun:
    run = BenchmarkRun.objects.create(
        window_start="2026-03-01",
        window_end="2026-06-01",
        min_workspaces=8,
        min_posts=200,
        max_posts_per_contributor=50,
    )
    CohortBenchmark.objects.create(
        run=run,
        vertical=vertical,
        market="PT",
        platform="instagram",
        size_band="1000_10000",
        post_format="FEED",
        **cohort,
    )
    return run


class TestAccess:
    def test_a_non_contributor_is_told_to_opt_in(
        self, auth_client: Any, workspace: Any, policy: Any, cohort_on: None
    ) -> None:
        """Access is the reason to contribute. Reading without contributing
        would make the cohort a public good nobody pays into."""
        response = auth_client.get(reverse(URL))

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "benchmark_consent_required"

    def test_flag_off_is_404(self, auth_client: Any, workspace: Any) -> None:
        assert auth_client.get(reverse(URL)).status_code == 404

    def test_another_organizations_workspace_is_404(
        self, auth_client: Any, contributing: Any
    ) -> None:
        stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
        theirs = provision_workspace(stranger, name="Another Company")

        response = auth_client.get(reverse(URL), HTTP_X_WORKSPACE_ID=str(theirs.pk))

        assert response.status_code == 404

    def test_another_workspace_in_the_same_organization_is_404(
        self, auth_client: Any, contributing: Any
    ) -> None:
        from workspaces.models import Workspace

        sibling = Workspace.objects.create(
            organization=contributing.organization, name="Sibling", slug="sibling-brand"
        )

        response = auth_client.get(reverse(URL), HTTP_X_WORKSPACE_ID=str(sibling.pk))

        assert response.status_code == 404


class TestWhatAContributorReads:
    def test_a_sufficient_cohort_carries_its_distribution(
        self, auth_client: Any, contributing: Any, vertical: Any
    ) -> None:
        own_observation(contributing, vertical)
        run_with(
            vertical,
            contributors=9,
            posts=240,
            sufficient=True,
            metrics={"engagement_rate": {"median": 0.041, "p25": 0.02, "p75": 0.06, "posts": 240}},
            metric_counts={
                "engagement_rate": {"contributors": 9, "posts": 240},
                "reach_rate": {"contributors": 2, "posts": 30},
                "comment_rate": {"contributors": 9, "posts": 240},
            },
            best_windows=[{"window": "evening", "median_engagement_rate": 0.05, "posts": 120}],
        )

        response = auth_client.get(reverse(URL))

        assert response.status_code == 200
        [cohort] = response.json()["cohorts"]
        assert cohort["status"] == "ok"
        assert cohort["contributors"] == 9
        assert cohort["metrics"]["engagement_rate"] == {
            "status": "ok",
            "median": 0.041,
            "p25": 0.02,
            "p75": 0.06,
            "posts": 240,
        }
        assert cohort["metrics"]["reach_rate"] == {
            "status": "insufficient_cohort_data",
            "shortfall": {"workspaces": 6, "posts": 170},
        }
        assert cohort["best_windows"][0]["window"] == "evening"

    def test_a_sub_threshold_cohort_says_so_with_its_shortfall(
        self, auth_client: Any, contributing: Any, vertical: Any
    ) -> None:
        own_observation(contributing, vertical)
        run_with(vertical, contributors=3, posts=41, sufficient=False)

        [cohort] = auth_client.get(reverse(URL)).json()["cohorts"]

        assert cohort == {
            "platform": "instagram",
            "size_band": "1000_10000",
            "post_format": "FEED",
            "status": "insufficient_cohort_data",
            "shortfall": {"workspaces": 5, "posts": 159},
        }

    def test_the_shortfall_uses_the_thresholds_the_run_was_computed_under(
        self, auth_client: Any, contributing: Any, vertical: Any
    ) -> None:
        own_observation(contributing, vertical)
        run_with(vertical, contributors=3, posts=41, sufficient=False)
        config = BenchmarkConfig.get_solo()
        config.min_workspaces = 20
        config.save()

        [cohort] = auth_client.get(reverse(URL)).json()["cohorts"]

        assert cohort["shortfall"]["workspaces"] == 5

    def test_a_cohort_projected_since_the_last_run_is_pending(
        self, auth_client: Any, contributing: Any, vertical: Any
    ) -> None:
        own_observation(contributing, vertical, post_format="REEL")
        run_with(vertical, contributors=9, posts=240, sufficient=True)

        cohorts = auth_client.get(reverse(URL)).json()["cohorts"]

        assert cohorts == [
            {
                "platform": "instagram",
                "size_band": "1000_10000",
                "post_format": "REEL",
                "status": "pending",
            }
        ]

    def test_only_the_workspaces_own_cohorts_are_listed(
        self, auth_client: Any, contributing: Any, vertical: Any
    ) -> None:
        """Other cohorts exist in the run. Listing them would tell a customer
        which verticals and markets the product has other customers in."""
        own_observation(contributing, vertical)
        run = run_with(vertical, contributors=9, posts=240, sufficient=True)
        CohortBenchmark.objects.create(
            run=run,
            vertical=vertical,
            market="FR",
            platform="tiktok",
            size_band="0_1000",
            post_format="SHORT",
            contributors=12,
            posts=300,
            sufficient=True,
        )

        cohorts = auth_client.get(reverse(URL)).json()["cohorts"]

        assert [(c["platform"], c["post_format"]) for c in cohorts] == [("instagram", "FEED")]

    def test_a_contributor_with_nothing_projected_yet_reads_an_empty_list(
        self, auth_client: Any, contributing: Any
    ) -> None:
        response = auth_client.get(reverse(URL))

        assert response.status_code == 200
        assert response.json()["cohorts"] == []
        assert response.json()["run"] is None
