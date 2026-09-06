"""No fabricated row ever reaches a user as insight (P0-37, Part 7 rule 17).

The fake exists so a fresh checkout runs end-to-end with zero third-party
accounts. That is worth having and it is also the most dangerous object in the
codebase, because its output is shaped exactly like real measurement. Two
independent guards, because either one alone fails open:

* **the queryset** — `PostMetric.objects.analysable()` filters `source=FAKE`,
  so a statistic computed from fake rows is not a bug in a caller, it is
  impossible;
* **the settings** — the flag cannot default on in production, so the guard
  above is never load-bearing on a live deployment in the first place.

A-17 is the acceptance criterion; this file is where it is asserted rather than
assumed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import Availability, MetricSource, PostMetric
from analytics.services import ingest, signals
from analytics.tests.conftest import make_target

pytestmark = pytest.mark.django_db


def test_the_fake_stamps_every_row_it_produces(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """Provenance is recorded at write time. A row that did not say where it
    came from could not be excluded later."""
    make_target(paid_workspace, user, social_account, age_days=0)

    assert ingest.capture_due() == 1

    row = PostMetric.objects.get()
    assert row.source == MetricSource.FAKE
    assert row.provider_key == "fake"
    assert row.schema_version >= 1


def test_fake_rows_are_invisible_to_every_statistic(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The queryset guard. `analysable()` is what signals, digests, rule
    proposals and cohort benchmarks all read through."""
    make_target(paid_workspace, user, social_account, age_days=0)
    ingest.capture_due()

    assert PostMetric.objects.count() == 1
    assert PostMetric.objects.analysable().count() == 0

    overview = signals.overview(paid_workspace, horizon_days=90)
    assert overview.posts == 0
    assert overview.impressions == 0


def test_unavailable_rows_are_invisible_too(published_target: Any) -> None:
    """The same guard, for the other reason a row carries no numbers. An
    `UNAVAILABLE` row exists to make a gap visible; counting it as a
    measurement would put the gap into the denominator."""
    PostMetric.objects.create(
        post_target=published_target,
        captured_at=timezone.now(),
        availability=Availability.UNAVAILABLE,
        source=MetricSource.PROVIDER,
        provider_key="zernio",
    )

    assert PostMetric.objects.count() == 1
    assert PostMetric.objects.analysable().count() == 0


def test_a_real_measured_row_is_analysable(published_target: Any) -> None:
    """The control. If `analysable()` filtered everything, the tests above
    would pass while the product showed a user nothing at all."""
    PostMetric.objects.create(
        post_target=published_target,
        captured_at=timezone.now() - dt.timedelta(hours=1),
        availability=Availability.MEASURED,
        source=MetricSource.PROVIDER,
        provider_key="zernio",
        impressions=100,
        likes=10,
        engagement_rate=0.1,
    )

    assert PostMetric.objects.analysable().count() == 1


# The settings half of this guard is asserted where the other prod-settings
# assertions live: `common/tests/test_settings.py::test_prod_cannot_run_on_fakes`
# imports `config.settings.prod` for real rather than grepping it.
