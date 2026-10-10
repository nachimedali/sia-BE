"""The projection (P8-02, P8-06) — the one place tenant rows become cohort rows.

Everything the aggregator will ever see passes through `project_workspace`, so
every exclusion rule lives here and is tested here: unconsented, unmeasured,
fabricated, too young, too old, and of unknown size are each *absent* — never
bucketed as "unknown", and never a zero.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from analytics.models import AccountSnapshot, Availability, MetricSource
from benchmarks.models import BenchmarkObservation, ConsentPolicy
from benchmarks.services import consent, projection
from benchmarks.tests.fixtures import NOW, connect, publish_measured
from billing.models import FeatureFlag
from billing.services.flags import COHORT_V8

pytestmark = pytest.mark.django_db

EDGES = [1000, 10000, 100000, 1000000]


@pytest.fixture
def contributor(make_workspace: Any, leaf: Any, policy: ConsentPolicy, cohort_on: None) -> Any:
    workspace = make_workspace(category=leaf, market="PT")
    consent.grant(workspace, actor=workspace.organization.owner, policy_version=1, market="PT")
    return workspace


def rows_for(workspace: Any) -> list[BenchmarkObservation]:
    return list(
        BenchmarkObservation.objects.filter(contributor=projection.contributor_token(workspace.pk))
    )


class TestSizeBands:
    @pytest.mark.parametrize(
        ("followers", "band"),
        [
            (0, "0_1000"),
            (999, "0_1000"),
            (1000, "1000_10000"),
            (99_999, "10000_100000"),
            (1_000_000, "1000000_plus"),
        ],
    )
    def test_edges_are_lower_inclusive(self, followers: int, band: str) -> None:
        assert projection.size_band(followers, EDGES) == band


class TestContributorTokens:
    def test_a_token_is_stable_distinct_and_not_the_id(self) -> None:
        first = projection.contributor_token(41)

        assert first == projection.contributor_token(41)
        assert first != projection.contributor_token(42)
        assert "41" not in first
        assert len(first) == 64


class TestWhatBecomesAnObservation:
    def test_a_measured_post_is_projected_with_its_cohort_key_and_rates(
        self, contributor: Any, vertical: Any
    ) -> None:
        account = connect(contributor, followers=5000)
        publish_measured(contributor, account, engagement_rate=0.05, impressions=1000, comments=10)

        assert projection.project_workspace(contributor, now=NOW) == 1

        [row] = rows_for(contributor)
        assert row.vertical_id == vertical.pk, "the vertical is the root of the category tree"
        assert row.market == "PT"
        assert row.platform == "instagram"
        assert row.size_band == "1000_10000"
        assert row.post_format == "FEED"
        assert row.published_on == (NOW - dt.timedelta(days=20)).date()
        assert row.engagement_rate == pytest.approx(0.05)
        assert row.reach_rate == pytest.approx(1000 / 5000)
        assert row.comment_rate == pytest.approx(10 / 1000)

    def test_the_posting_window_is_the_workspaces_local_clock(
        self, make_workspace: Any, leaf: Any, policy: ConsentPolicy, cohort_on: None
    ) -> None:
        """12:00 UTC is evening in Tokyo. A benchmark of "best posting windows"
        bucketed in UTC would be a different part of the day for every market."""
        tokyo = make_workspace(category=leaf, market="JP", timezone="Asia/Tokyo")
        consent.grant(tokyo, actor=tokyo.organization.owner, policy_version=1, market="JP")
        publish_measured(tokyo, connect(tokyo))

        projection.project_workspace(tokyo, now=NOW)

        [row] = rows_for(tokyo)
        assert row.posting_window == "evening"

    def test_an_unconsented_workspace_projects_nothing(
        self, make_workspace: Any, leaf: Any, policy: ConsentPolicy, cohort_on: None
    ) -> None:
        workspace = make_workspace(category=leaf)
        publish_measured(workspace, connect(workspace))

        assert projection.project_workspace(workspace, now=NOW) == 0
        assert rows_for(workspace) == []

    def test_flag_off_projects_nothing(self, contributor: Any) -> None:
        FeatureFlag.objects.create(
            organization=contributor.organization, key=COHORT_V8, enabled=False
        )
        publish_measured(contributor, connect(contributor))

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_a_fabricated_capture_never_becomes_an_observation(self, contributor: Any) -> None:
        """A-17. The fake adapter marks its rows MEASURED; `source` is what keeps
        them out, and it is enforced by the queryset the projection reads."""
        publish_measured(contributor, connect(contributor), source=MetricSource.FAKE)

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_an_unavailable_capture_is_absent_not_zero(self, contributor: Any) -> None:
        publish_measured(
            contributor,
            connect(contributor),
            engagement_rate=None,
            impressions=None,
            comments=None,
            availability=Availability.UNAVAILABLE,
        )

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_a_missing_metric_is_null_and_its_siblings_survive(self, contributor: Any) -> None:
        """A platform that reports engagement but not impressions contributes
        its engagement rate. Its reach and comment rates are null, never 0."""
        publish_measured(contributor, connect(contributor), impressions=None, comments=4)

        projection.project_workspace(contributor, now=NOW)

        [row] = rows_for(contributor)
        assert row.engagement_rate == pytest.approx(0.05)
        assert row.reach_rate is None
        assert row.comment_rate is None

    def test_zero_impressions_is_a_measured_zero_reach_and_no_comment_rate(
        self, contributor: Any
    ) -> None:
        publish_measured(contributor, connect(contributor), impressions=0, comments=0)

        projection.project_workspace(contributor, now=NOW)

        [row] = rows_for(contributor)
        assert row.reach_rate == 0.0
        assert row.comment_rate is None, "a rate over a zero denominator does not exist"

    def test_a_post_still_accruing_is_left_out(self, contributor: Any) -> None:
        """A two-day-old post has not finished collecting engagement. Counting
        it drags every median toward the posts nobody has seen yet."""
        publish_measured(contributor, connect(contributor), days_ago=2)

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_a_post_older_than_the_window_is_left_out(self, contributor: Any) -> None:
        publish_measured(contributor, connect(contributor), days_ago=200)

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_an_account_of_unknown_size_is_left_out(self, contributor: Any) -> None:
        """No band means no comparable cohort. An "unknown" band would pool an
        800-follower page with an 80,000-follower one — the exact error the band
        exists to prevent."""
        publish_measured(contributor, connect(contributor, followers=None))

        assert projection.project_workspace(contributor, now=NOW) == 0

    def test_the_band_is_the_accounts_size_when_it_posted(self, contributor: Any) -> None:
        account = connect(contributor, followers=None)
        AccountSnapshot.objects.create(
            social_account=account, captured_at=NOW - dt.timedelta(days=90), followers=50_000
        )
        AccountSnapshot.objects.create(
            social_account=account, captured_at=NOW - dt.timedelta(days=21), followers=800
        )
        publish_measured(contributor, account, days_ago=20)

        projection.project_workspace(contributor, now=NOW)

        [row] = rows_for(contributor)
        assert row.size_band == "0_1000"


class TestReprojection:
    def test_projecting_twice_leaves_one_row_per_post(self, contributor: Any) -> None:
        account = connect(contributor)
        publish_measured(contributor, account)
        publish_measured(contributor, account, days_ago=30)

        projection.project_workspace(contributor, now=NOW)
        projection.project_workspace(contributor, now=NOW)

        assert len(rows_for(contributor)) == 2

    def test_a_deleted_post_leaves_the_projection_on_the_next_run(self, contributor: Any) -> None:
        account = connect(contributor)
        kept = publish_measured(contributor, account)
        gone = publish_measured(contributor, account, days_ago=30)
        projection.project_workspace(contributor, now=NOW)

        gone.post.delete()
        projection.project_workspace(contributor, now=NOW)

        assert len(rows_for(contributor)) == 1
        assert kept.pk

    def test_project_all_purges_a_workspace_that_stopped_contributing(
        self, contributor: Any
    ) -> None:
        """New terms published: every earlier grant is stale until renewed, and
        the nightly run must drop those rows without being told which ones."""
        publish_measured(contributor, connect(contributor))
        projection.project_all(now=NOW)
        assert rows_for(contributor)

        ConsentPolicy.objects.create(version=2, summary="Revised terms.")
        projection.project_all(now=NOW)

        assert rows_for(contributor) == []
