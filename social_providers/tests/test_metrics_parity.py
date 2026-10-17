"""The metrics fake and SocialAPI agree on what is unknown (SA-33).

The analytics suite measures through the fake. If the fake reported a number
where SocialAPI sends a placeholder `0`, every null-not-zero test downstream
could pass while production stored zeros. With `mirrors="socialapi"` the fake
answers `0` for exactly the pairs SocialAPI cannot read; both must come out of
normalisation with the same metrics left empty.
"""

from __future__ import annotations

from typing import Any

import pytest

from analytics.models import MetricCapability
from analytics.providers.fake import FakeMetricsProvider
from analytics.services.normalise import normalise
from social_providers.socialapi.capabilities import METRICS
from social_providers.socialapi.metrics import METRIC_FIELDS, SocialAPIMetricsProvider
from social_providers.tests.stub import ReadVendor, metrics_answer, seed_capabilities

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _capabilities() -> None:
    seed_capabilities()


def _vendor_answer(platform: str) -> dict[str, Any]:
    """Every field the vendor has, a figure where it can read and the
    placeholder `0` where it cannot — the shape the docs describe."""
    readable = {
        metric
        for metric in METRIC_FIELDS
        if metric in METRICS[platform] and METRICS[platform][metric]["available"]
    }
    fields: dict[str, Any] = {
        theirs: (5 if ours in readable else 0)
        for ours, theirs in METRIC_FIELDS.items()
        if theirs is not None
    }
    if "reach" in readable:
        fields["interactions"] = 5  # what proves the insights permission
    return metrics_answer(platform=platform, **fields)


def _unknown(values: dict[str, int | None]) -> set[str]:
    return {metric for metric, value in values.items() if value is None}


@pytest.mark.parametrize("platform", sorted(METRICS))
def test_the_fake_leaves_empty_exactly_what_socialapi_cannot_read(platform: str) -> None:
    vendor = ReadVendor()
    vendor.routes["GET /posts/p-1/metrics"] = _vendor_answer(platform)
    real = SocialAPIMetricsProvider(vendor.client())
    fake = FakeMetricsProvider()
    fake.mirrors = "socialapi"

    real_payload = real.fetch(platform=platform, provider_post_id="p-1")
    fake_payload = fake.fetch(platform=platform, provider_post_id="p-1")

    assert real_payload.availability == fake_payload.availability
    real_values = normalise(real_payload, provider=real, followers=None).values
    fake_values = normalise(fake_payload, provider=fake, followers=None).values
    assert _unknown(real_values) == _unknown(fake_values)


def test_the_mirror_sends_zeros_that_never_reach_a_column() -> None:
    fake = FakeMetricsProvider()
    fake.mirrors = "socialapi"

    payload = fake.fetch(platform="facebook", provider_post_id="p-1")

    assert payload.body["saves"] == 0, "the placeholder is sent"
    assert normalise(payload, provider=fake, followers=None).values["saves"] is None


def test_the_mirror_follows_an_admin_flip() -> None:
    MetricCapability.objects.filter(
        provider_key="socialapi", platform="facebook", metric_key="saves"
    ).update(available=True)
    fake = FakeMetricsProvider()
    fake.mirrors = "socialapi"

    payload = fake.fetch(platform="facebook", provider_post_id="p-1")

    assert normalise(payload, provider=fake, followers=None).values["saves"] is not None
