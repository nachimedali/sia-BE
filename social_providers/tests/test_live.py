"""The live SocialAPI check — opt-in, read-only, free.

    SOCIALAPI_LIVE_TESTS=1 .venv/bin/python -m pytest -m integration social_providers

Reads the key from the environment (the test settings blank it on purpose, so
the normal suite never touches the network). Two free reads through the real
client: `GET /usage` and `GET /accounts`. Asserts the contract this step relies
on — the envelope, the request id, the rate-limit headers — not account data.
"""

from __future__ import annotations

import os

import pytest

from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.errors import SocialAPIError

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def live() -> SocialAPIClient:
    key = os.environ.get("SOCIALAPI_API_KEY", "")
    if os.environ.get("SOCIALAPI_LIVE_TESTS") != "1" or not key:
        pytest.skip("set SOCIALAPI_LIVE_TESTS=1 with SOCIALAPI_API_KEY to run against SocialAPI")
    return SocialAPIClient(api_key=key)


def test_usage_answers_with_limits_and_rate_headers(
    live: SocialAPIClient,
) -> None:  # pragma: no cover
    result = live.get("/usage")
    assert result.status == 200
    for field in ("posts_used", "posts_limit", "interactions_used", "interactions_limit"):
        assert field in result.data
    assert result.rate.limit and result.rate.limit >= 60
    assert result.rate.remaining is not None


def test_accounts_lists(live: SocialAPIClient) -> None:  # pragma: no cover
    result = live.get("/accounts")
    assert result.status == 200


def test_a_bad_key_is_misconfigured_not_an_outage() -> None:  # pragma: no cover
    if os.environ.get("SOCIALAPI_LIVE_TESTS") != "1":
        pytest.skip("live tests not requested")
    with pytest.raises(SocialAPIError) as caught:
        SocialAPIClient(api_key="sapi_key_" + "0" * 62).get("/usage")
    assert caught.value.kind == "MISCONFIGURED"
    assert caught.value.vendor_code.startswith("auth.")
