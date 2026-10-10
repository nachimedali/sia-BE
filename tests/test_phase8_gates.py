"""Phase 8's ship gate, its hard stop, and the acceptance criteria it owns.

**P8-G1 — a sub-threshold benchmark renders "insufficient data", never a number.**
Asserted at every layer that could produce one: the table, the aggregator, the
reader and the API. A gate held only in the service holds in the one place
nobody renders from.

**P8-08 — HARD STOP: if the aggregation job can read raw content at all, the
projection is wrong, not the query.** So this is not tested by checking what the
aggregator *happens* to read. It is tested three ways that fail on capability:
the projection has no column that could hold content or identity, the
aggregator's module imports nothing that could reach tenant tables, and the SQL
it actually issues touches benchmark tables only.

**A-13** revocation stops future contribution and is timestamped.
**A-17** no benchmark can be produced from fabricated rows.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from analytics.models import MetricSource
from benchmarks.models import (
    BenchmarkObservation,
    BenchmarkRun,
    CohortBenchmark,
    ConsentAction,
)
from benchmarks.services import aggregate, consent, projection
from benchmarks.tests.fixtures import NOW, connect, publish_measured

pytestmark = pytest.mark.django_db


def _numbers_in(value: Any) -> list[float]:
    """Every float anywhere inside a JSON-shaped value."""
    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [n for item in value.values() for n in _numbers_in(item)]
    if isinstance(value, list):
        return [n for item in value for n in _numbers_in(item)]
    return []


@pytest.fixture
def brands(
    make_workspace: Any, leaf: Any, policy: Any, cohort_on: None, small_thresholds: Any
) -> list[Any]:
    """Three consenting brands in one cohort, each with three measured posts."""
    result = []
    for index in range(3):
        workspace = make_workspace(category=leaf, market="PT", name=f"Bakery {index}")
        consent.grant(workspace, actor=workspace.organization.owner, policy_version=1, market="PT")
        account = connect(workspace, followers=4000)
        for day in (15, 25, 35):
            publish_measured(workspace, account, days_ago=day, engagement_rate=0.02 + index / 100)
        result.append(workspace)
    return result


def read_as_owner(workspace: Any, api_client_for: Any) -> Any:
    client = api_client_for(workspace.organization.owner, workspace)
    return client.get(reverse("benchmarks"))


class TestG1SubThresholdIsNeverANumber:
    def test_the_table_cannot_hold_a_statistic_for_an_insufficient_cohort(
        self, vertical: Any
    ) -> None:
        run = BenchmarkRun.objects.create(
            window_start="2026-03-01",
            window_end="2026-06-01",
            min_workspaces=8,
            min_posts=200,
            max_posts_per_contributor=50,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            CohortBenchmark.objects.create(
                run=run,
                vertical=vertical,
                market="PT",
                platform="instagram",
                size_band="1000_10000",
                post_format="FEED",
                contributors=3,
                posts=12,
                sufficient=False,
                metrics={"engagement_rate": {"median": 0.04}},
            )

    def test_the_aggregator_writes_counts_and_no_statistics(
        self, observe: Any, vertical: Any
    ) -> None:
        observe(vertical=vertical, contributors=7, posts_each=40)

        run = aggregate.compute(now=NOW)

        assert run is not None
        cohort = run.cohorts.get()
        assert cohort.sufficient is False
        assert _numbers_in(cohort.metrics) == []
        assert _numbers_in(cohort.best_windows) == []

    def test_the_api_renders_insufficient_data_end_to_end(
        self, brands: list[Any], api_client_for: Any, small_thresholds: Any
    ) -> None:
        """Real posts, real projection, real aggregation — one brand short."""
        small_thresholds.min_workspaces = 4
        small_thresholds.min_posts = 4
        small_thresholds.save()
        projection.project_all(now=NOW)
        aggregate.compute(now=NOW)

        response = read_as_owner(brands[0], api_client_for)

        assert response.status_code == 200
        [cohort] = response.json()["cohorts"]
        assert cohort["status"] == "insufficient_cohort_data"
        assert cohort["shortfall"] == {"workspaces": 1, "posts": 0}
        assert "metrics" not in cohort
        assert _numbers_in(cohort) == [], "no statistic may accompany insufficient data"

    def test_at_the_threshold_the_same_pipeline_does_produce_a_benchmark(
        self, brands: list[Any], api_client_for: Any
    ) -> None:
        """The other side of the boundary, so the test above cannot pass by
        a pipeline that never produces anything."""
        projection.project_all(now=NOW)
        aggregate.compute(now=NOW)

        [cohort] = read_as_owner(brands[0], api_client_for).json()["cohorts"]

        assert cohort["status"] == "ok"
        assert cohort["metrics"]["engagement_rate"]["median"] == pytest.approx(0.03)


class TestP808TheAggregatorCannotReadContent:
    #: The only columns the projection may carry. Adding one is a deliberate
    #: edit here, reviewed against P8-06 — not a migration that slips through.
    ALLOWED_FIELDS: ClassVar[dict[str, type[models.Field[Any, Any]]]] = {
        "id": models.BigAutoField,
        "contributor": models.CharField,
        "vertical": models.ForeignKey,
        "market": models.CharField,
        "platform": models.CharField,
        "size_band": models.CharField,
        "post_format": models.CharField,
        "posting_window": models.CharField,
        "published_on": models.DateField,
        "engagement_rate": models.FloatField,
        "reach_rate": models.FloatField,
        "comment_rate": models.FloatField,
        "projected_at": models.DateTimeField,
    }

    def test_the_projection_has_no_column_that_could_hold_content_or_identity(self) -> None:
        fields = {field.name: field for field in BenchmarkObservation._meta.get_fields()}

        assert set(fields) == set(self.ALLOWED_FIELDS), (
            "BenchmarkObservation's columns changed. Every column is a thing that "
            "crosses a tenant boundary; review the addition against P8-06."
        )
        for name, field in fields.items():
            assert type(field) is self.ALLOWED_FIELDS[name], name
            if isinstance(field, models.CharField):
                assert field.max_length is not None and field.max_length <= 64, name

    def test_its_only_relation_is_the_shared_category_taxonomy(self) -> None:
        relations = [
            field.related_model._meta.label
            for field in BenchmarkObservation._meta.get_fields()
            if field.is_relation and field.related_model is not None
        ]
        assert relations == ["categories.Category"]

    def test_the_aggregator_imports_nothing_that_reaches_tenant_data(self) -> None:
        source = Path(aggregate.__file__).read_text()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = re.compile(
            r"^(accounts|ai|analytics|billing|channels|collaboration|content|learn|media|"
            r"notifications|planning|products|reminders|scheduling|taste|tools|trends|"
            r"workspaces)(\.|$)"
        )
        reaching = sorted(name for name in imported if forbidden.match(name))
        project = sorted(name for name in imported if name.startswith("benchmarks"))

        assert reaching == [], f"the aggregator imports tenant apps: {reaching}"
        assert project == ["benchmarks.models"]

    def test_the_sql_it_issues_touches_benchmark_tables_only(
        self, observe: Any, vertical: Any
    ) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25)

        with CaptureQueriesContext(connection) as queries:
            aggregate.compute(now=NOW)

        tables = {
            table
            for query in queries.captured_queries
            for table in re.findall(r'(?:FROM|JOIN|INTO|UPDATE)\s+"(\w+)"', query["sql"])
        }
        assert tables, "the capture saw no queries, so it proved nothing"
        assert all(table.startswith("benchmarks_") for table in tables), tables


class TestA13RevocationStopsFutureContribution:
    def test_after_revoking_no_later_run_includes_the_workspace(self, brands: list[Any]) -> None:
        projection.project_all(now=NOW)
        before = aggregate.compute(now=NOW)
        assert before is not None and before.cohorts.get().contributors == 3

        revocation = consent.revoke(brands[0], actor=brands[0].organization.owner)
        projection.project_all(now=NOW + dt.timedelta(days=1))
        after = aggregate.compute(now=NOW + dt.timedelta(days=1))

        assert revocation.action == ConsentAction.REVOKED
        assert revocation.recorded_at is not None
        assert after is not None and after.cohorts.get().contributors == 2
        assert before.cohorts.get().contributors == 3, "history is not recomputed"


class TestA17NothingFromFabricatedRows:
    def test_a_cohort_built_entirely_from_the_fake_adapter_produces_nothing(
        self,
        make_workspace: Any,
        leaf: Any,
        policy: Any,
        cohort_on: None,
        small_thresholds: Any,
    ) -> None:
        for index in range(3):
            workspace = make_workspace(category=leaf, name=f"Fabricated {index}")
            consent.grant(
                workspace, actor=workspace.organization.owner, policy_version=1, market="PT"
            )
            account = connect(workspace)
            for day in (15, 25, 35):
                publish_measured(workspace, account, days_ago=day, source=MetricSource.FAKE)

        assert projection.project_all(now=NOW) == 0
        assert BenchmarkObservation.objects.count() == 0
        assert aggregate.compute(now=NOW) is None
        assert CohortBenchmark.objects.count() == 0
