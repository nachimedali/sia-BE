"""Thresholds as admin-editable data (P8-04), and the nightly job that uses them."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from benchmarks import markets
from benchmarks.models import BenchmarkConfig, BenchmarkRun
from benchmarks.services import consent
from benchmarks.tasks import refresh_benchmarks
from benchmarks.tests.fixtures import NOW, connect, publish_measured

pytestmark = pytest.mark.django_db


class TestConfig:
    def test_the_published_bars_are_the_defaults(self) -> None:
        config = BenchmarkConfig.get_solo()

        assert config.min_workspaces == 8
        assert config.min_posts == 200

    def test_a_cohort_of_two_is_not_configurable(self) -> None:
        """Below three, each member of a cohort can subtract itself from the
        aggregate and read the other's numbers. No admin edit may get there."""
        config = BenchmarkConfig.get_solo()
        config.min_workspaces = 2
        with pytest.raises(IntegrityError), transaction.atomic():
            config.save()

    def test_fewer_posts_than_workspaces_is_not_configurable(self) -> None:
        config = BenchmarkConfig.get_solo()
        config.min_workspaces = 10
        config.min_posts = 9
        with pytest.raises(IntegrityError), transaction.atomic():
            config.save()

    @pytest.mark.parametrize("edges", [[], [1000, 1000], [10000, 1000], [0, 10], ["1k"]])
    def test_size_band_edges_must_be_increasing_positive_integers(self, edges: Any) -> None:
        config = BenchmarkConfig.get_solo()
        config.size_band_edges = edges
        with pytest.raises(ValidationError):
            config.full_clean()


class TestMarkets:
    def test_the_list_is_iso_3166_and_carries_names(self) -> None:
        listed = markets.all_markets()

        assert listed["PT"] == "Portugal"
        assert listed["FR"] == "France"
        assert "XX" not in listed
        assert all(len(code) == 2 and code.isupper() for code in listed)


class TestTheNightlyJob:
    def test_it_projects_then_aggregates(
        self,
        make_workspace: Any,
        leaf: Any,
        policy: Any,
        cohort_on: None,
        small_thresholds: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from django.utils import timezone

        monkeypatch.setattr(timezone, "now", lambda: NOW)
        for index in range(3):
            workspace = make_workspace(category=leaf, name=f"Nightly {index}")
            consent.grant(
                workspace, actor=workspace.organization.owner, policy_version=1, market="PT"
            )
            account = connect(workspace)
            for day in (15, 25, 35):
                publish_measured(workspace, account, days_ago=day)

        run_id = refresh_benchmarks()

        run = BenchmarkRun.objects.get(pk=run_id)
        assert run.cohorts.get().sufficient is True

    def test_with_nobody_contributing_it_writes_nothing(self, db: None) -> None:
        assert refresh_benchmarks() is None
        assert BenchmarkRun.objects.count() == 0
