"""Audience demographics on the daily account snapshot (P6-01).

Two things are under test and only one of them is the happy path. The other is
the distinction this whole subsystem exists to preserve: an account the vendor
will not report on must produce an `UNAVAILABLE` row, never a breakdown of
zeros. Below 100 followers Zernio declines outright (L-5), and "we cannot see
your audience yet" and "your audience is nobody" are different statements.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine

from analytics.models import AudienceDemographic, Availability, DemographicDimension
from analytics.providers.zernio import _shares
from analytics.services import ingest

pytestmark = pytest.mark.django_db

MOMENT = dt.datetime(2026, 6, 1, 1, 30, tzinfo=dt.UTC)


def test_the_daily_snapshot_captures_a_breakdown_per_dimension(
    social_account: Any, metrics_provider: Any
) -> None:
    """One pass of the *existing* job, not a second ladder."""
    with time_machine.travel(MOMENT, tick=False):
        assert ingest.snapshot_accounts() == 1

    rows = {row.dimension: row for row in AudienceDemographic.objects.all()}
    assert set(rows) == {DemographicDimension.AGE, DemographicDimension.COUNTRY}
    assert rows[DemographicDimension.AGE].availability == Availability.MEASURED
    age = rows[DemographicDimension.AGE].breakdown
    assert age is not None
    assert age["25-34"] == pytest.approx(0.44)
    assert rows[DemographicDimension.AGE].provider_key == "fake"


def test_an_account_the_provider_declines_gets_unavailable_rows_not_zeros(
    social_account: Any, metrics_provider: Any
) -> None:
    """The ≥100-follower threshold, and the whole of Part 7 rule 12."""
    metrics_provider.tiny_accounts.add(social_account.provider_account_id)

    with time_machine.travel(MOMENT, tick=False):
        assert ingest.snapshot_accounts() == 1

    rows = AudienceDemographic.objects.all()
    assert rows.count() == len(DemographicDimension.values)
    assert {row.availability for row in rows} == {Availability.UNAVAILABLE}
    # Null, not `{}` and not a dict of zeros: a surface handed an empty chart
    # would draw one.
    assert {row.breakdown for row in rows} == {None}


def test_a_dimension_is_reported_per_axis_so_absence_is_visible(
    social_account: Any, metrics_provider: Any
) -> None:
    """A row per dimension rather than one row for the account.

    A surface cannot render "unavailable" for an axis it never received, and
    inferring absence from a missing record is how an empty chart gets drawn
    instead.
    """
    metrics_provider.tiny_accounts.add(social_account.provider_account_id)
    with time_machine.travel(MOMENT, tick=False):
        ingest.snapshot_accounts()

    captured = set(AudienceDemographic.objects.values_list("dimension", flat=True))
    assert captured == set(DemographicDimension.values)


def test_a_second_run_in_the_same_hour_is_not_a_second_capture(
    social_account: Any, metrics_provider: Any
) -> None:
    with time_machine.travel(MOMENT, tick=False):
        ingest.snapshot_accounts()
        ingest.snapshot_accounts()

    assert AudienceDemographic.objects.filter(dimension=DemographicDimension.AGE).count() == 1


def test_the_next_day_is_a_new_capture(social_account: Any, metrics_provider: Any) -> None:
    with time_machine.travel(MOMENT, tick=False):
        ingest.snapshot_accounts()
    with time_machine.travel(MOMENT + dt.timedelta(days=1), tick=False):
        ingest.snapshot_accounts()

    assert AudienceDemographic.objects.filter(dimension=DemographicDimension.AGE).count() == 2


def test_a_demographics_failure_does_not_lose_the_follower_count(
    social_account: Any, metrics_provider: Any, monkeypatch: Any
) -> None:
    """Demographics are the softer half of the daily pass.

    Losing them costs a chart until tomorrow; losing the follower count costs
    the denominator every engagement rate divides by.
    """
    from analytics.providers.base import MetricsError

    def explode(**_kwargs: Any) -> Any:
        raise MetricsError("demographics unavailable")

    monkeypatch.setattr(metrics_provider, "fetch_demographics", explode)

    with time_machine.travel(MOMENT, tick=False):
        assert ingest.snapshot_accounts() == 1

    social_account.refresh_from_db()
    assert social_account.followers_cached == 1000
    assert AudienceDemographic.objects.count() == 0


def test_a_provider_with_no_demographics_method_is_skipped_not_crashed(
    social_account: Any, monkeypatch: Any
) -> None:
    """A provider covering metrics but not demographics is a supported state,
    not a broken one — coverage is per-method, exactly as it is per-platform.

    A real provider class without the method rather than a patched-out
    attribute: the module-level fake is a singleton, and monkeypatching a
    method off it leaks into whatever runs next.
    """
    from analytics.providers.base import MetricsProviderRegistry
    from analytics.providers.fake import FakeMetricsProvider

    class MetricsOnly(FakeMetricsProvider):
        fetch_demographics = None  # type: ignore[assignment]

    monkeypatch.setattr(
        ingest, "get_metrics_registry", lambda: MetricsProviderRegistry([MetricsOnly()])
    )

    with time_machine.travel(MOMENT, tick=False):
        assert ingest.snapshot_accounts() == 1

    assert AudienceDemographic.objects.count() == 0


def test_demographics_are_scoped_to_their_account(
    social_account: Any, other_workspace: tuple[Any, Any, Any], metrics_provider: Any
) -> None:
    _workspace, _user, other_account = other_workspace

    with time_machine.travel(MOMENT, tick=False):
        assert ingest.snapshot_accounts() == 2

    mine = AudienceDemographic.objects.filter(social_account=social_account)
    theirs = AudienceDemographic.objects.filter(social_account=other_account)
    assert mine.exists()
    assert theirs.exists()
    assert not mine.filter(social_account__workspace=other_account.workspace).exists()


# -----------------------------------------------------------------------------
# Normalisation at the port
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"PT": 50, "ES": 50}, {"PT": 0.5, "ES": 0.5}),
        ({"PT": 0.6, "ES": 0.4}, {"PT": 0.6, "ES": 0.4}),
        ({"PT": 1, "ES": "bogus"}, {"PT": 1.0}),
    ],
)
def test_counts_and_shares_both_normalise_to_shares(
    raw: dict[str, Any], expected: dict[str, float]
) -> None:
    """The vendor returns counts on some dimensions and percentages on others.

    Normalising at the port is what stops every reader downstream from having
    to guess which one it is holding.
    """
    assert _shares(raw) == expected


@pytest.mark.parametrize("raw", [None, {}, [], {"PT": 0, "ES": 0}, {"PT": "x"}])
def test_an_empty_or_malformed_breakdown_is_none_never_zeros(raw: Any) -> None:
    """An all-zero breakdown is the vendor saying it has nothing. Storing it
    would be the fabricated row Part 7 rule 12 forbids."""
    assert _shares(raw) is None
