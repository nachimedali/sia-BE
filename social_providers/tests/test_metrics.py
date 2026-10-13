"""SA-50/52/53: what SocialAPI's measurement answers become.

Every rule of plan §5.3 has a test here, against the vendor's documented
answers (recorded in `stub.metrics_answer`): a pair the capability table marks
unavailable is null whatever arrived; an unsynced target writes no numbers; an
omitted figure is null and an insight `0` counts only once the insights
permission is proven; account history keeps its gauge/flow, gap and coverage
markers; our names map to the vendor's through a table.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from analytics.models import MetricCapability
from analytics.providers.base import METRIC_KEYS, MetricsError, RawMetricPayload
from analytics.services.normalise import normalise
from social_providers.socialapi.metrics import SocialAPIMetricsProvider
from social_providers.tests.stub import ReadVendor, error, metrics_answer, seed_capabilities

pytestmark = pytest.mark.django_db

PID = "p-1"
ROUTE = f"GET /posts/{PID}/metrics"


@pytest.fixture(autouse=True)
def _capabilities() -> None:
    seed_capabilities()


@pytest.fixture
def vendor() -> ReadVendor:
    return ReadVendor()


def provider(vendor: ReadVendor) -> SocialAPIMetricsProvider:
    return SocialAPIMetricsProvider(vendor.client())


def ago(hours: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()


def fetch(vendor: ReadVendor, platform: str = "instagram") -> RawMetricPayload:
    return provider(vendor).fetch(platform=platform, provider_post_id=PID)


def values(vendor: ReadVendor, platform: str = "instagram") -> dict[str, int | None]:
    source = provider(vendor)
    payload = source.fetch(platform=platform, provider_post_id=PID)
    return normalise(payload, provider=source, followers=None).values


# -----------------------------------------------------------------------------
# One call per rung, mapped through a table (rule 5)
# -----------------------------------------------------------------------------
def test_one_read_returns_counts_and_insights_under_our_names(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        likes=42, comments=5, shares=8, saves=31, views=4300, reach=1820, interactions=86
    )

    payload = fetch(vendor)

    assert payload.availability == "MEASURED"
    assert vendor.paths() == [f"/posts/{PID}/metrics"]
    assert values(vendor) == {
        "likes": 42,
        "comments": 5,
        "shares": 8,
        "saves": 31,
        "views": 4300,
        "reach": 1820,
        # Instagram reports views and profile taps, not impressions or link
        # clicks: the capability table says so, and the column stays empty.
        "impressions": None,
        "clicks": None,
    }


def test_the_raw_answer_is_kept_for_a_reprocess(vendor: ReadVendor) -> None:
    answer = metrics_answer(likes=1)
    vendor.routes[ROUTE] = answer

    payload = fetch(vendor)

    assert payload.body["response"] == answer
    assert payload.body["observed"]["platform"] == "instagram"
    assert payload.provider_key == "socialapi"
    assert payload.schema_version == 1


def test_every_one_of_our_metrics_has_a_mapping_row() -> None:
    from social_providers.socialapi.metrics import METRIC_FIELDS

    assert set(METRIC_FIELDS) == set(METRIC_KEYS)


# -----------------------------------------------------------------------------
# Rule 1: an unavailable pair is null whatever arrived
# -----------------------------------------------------------------------------
def test_facebooks_always_zero_saves_are_stored_as_unknown(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        platform="facebook", likes=15, comments=3, shares=7, saves=0
    )

    got = values(vendor, "facebook")

    assert got["saves"] is None
    assert (got["likes"], got["comments"], got["shares"]) == (15, 3, 7)


def test_a_row_flipped_in_admin_takes_effect_on_the_next_capture(vendor: ReadVendor) -> None:
    MetricCapability.objects.filter(
        provider_key="socialapi", platform="facebook", metric_key="saves"
    ).update(available=True)
    vendor.routes[ROUTE] = metrics_answer(platform="facebook", likes=1, saves=4)

    assert values(vendor, "facebook")["saves"] == 4


def test_a_platform_with_nothing_readable_is_never_called(vendor: ReadVendor) -> None:
    payload = fetch(vendor, "tiktok")

    assert payload.availability == "UNAVAILABLE"
    assert vendor.calls == []


# -----------------------------------------------------------------------------
# Rule 2: a target never synced writes no numbers
# -----------------------------------------------------------------------------
def test_unsynced_within_the_grace_is_pending(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        platform="facebook", published_at=ago(2), synced=False, likes=0
    )

    assert fetch(vendor, "facebook").availability == "PENDING"


def test_unsynced_after_the_grace_is_unavailable_never_zero(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        platform="facebook", published_at=ago(30), synced=False, likes=0, comments=0
    )

    payload = fetch(vendor, "facebook")

    assert payload.availability == "UNAVAILABLE"
    assert all(value is None for value in values(vendor, "facebook").values())


def test_instagram_unsynced_waits_out_its_48_hour_delay(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(published_at=ago(30), synced=False)

    assert fetch(vendor).availability == "PENDING"


def test_a_target_the_platform_cannot_measure_is_unavailable(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        platform="facebook",
        error={"code": "resource.not_supported", "message": "No metrics for this post."},
        likes=0,
    )

    assert fetch(vendor, "facebook").availability == "UNAVAILABLE"


def test_a_post_gone_at_the_provider_is_unavailable(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = error(404, "resource.not_found", "No such post.")

    assert fetch(vendor).availability == "UNAVAILABLE"


def test_a_rate_limited_read_is_a_retryable_failure_not_a_row(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = httpx.Response(
        429,
        headers={"Retry-After": "30"},
        json={"error": {"code": "ratelimit.exceeded", "message": "Slow down."}},
    )

    with pytest.raises(MetricsError) as caught:
        fetch(vendor)

    assert caught.value.retryable is True


# -----------------------------------------------------------------------------
# Rule 3: omitted is null; an insight 0 counts only once insights are proven
# -----------------------------------------------------------------------------
def test_an_omitted_figure_is_null(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(likes=10, comments=2, views=500, reach=300)

    got = values(vendor)

    assert got["shares"] is None and got["saves"] is None
    assert got["views"] == 500


def test_insights_withheld_past_the_delay_mean_the_permission_is_missing(
    vendor: ReadVendor,
) -> None:
    vendor.routes[ROUTE] = metrics_answer(
        published_at=ago(72), likes=42, comments=5, shares=0, saves=0
    )

    payload = fetch(vendor)
    got = values(vendor)

    assert payload.insights_permission is False
    assert (got["likes"], got["comments"]) == (42, 5)
    assert got["shares"] is None and got["saves"] is None, "a zero here is the missing permission"


def test_insights_not_yet_reported_prove_nothing(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(published_at=ago(3), likes=4, shares=0, saves=0)

    payload = fetch(vendor)

    assert payload.insights_permission is None
    assert values(vendor)["shares"] is None, "unproven insight zeros are not measurements"


def test_an_insight_figure_proves_the_permission_and_its_zeros_count(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = metrics_answer(likes=4, reach=120, views=200, shares=0, saves=0)

    payload = fetch(vendor)

    assert payload.insights_permission is True
    assert values(vendor)["shares"] == 0


def test_platforms_without_insights_prove_nothing_and_keep_their_counts(
    vendor: ReadVendor,
) -> None:
    vendor.routes[ROUTE] = metrics_answer(platform="facebook", likes=3, shares=0)

    payload = fetch(vendor, "facebook")

    assert payload.insights_permission is None
    assert values(vendor, "facebook")["shares"] == 0


# -----------------------------------------------------------------------------
# Account counts, insights permission, demographics (SA-52, SA-53)
# -----------------------------------------------------------------------------
def test_account_counts_come_from_the_platform_summary(vendor: ReadVendor) -> None:
    vendor.routes["GET /platforms/instagram/accounts/acc_1/summary"] = {
        "follower_count": 12400,
        "following_count": 87,
        "media_count": 340,
        "reach": 22400,
    }

    stats = provider(vendor).fetch_account_stats(provider_account_id="acc_1", platform="instagram")

    assert stats == {
        "followers": 12400,
        "following": 87,
        "total_posts": 340,
        "insights_permission": True,
    }


def test_a_summary_refused_for_the_permission_flags_it(vendor: ReadVendor) -> None:
    vendor.routes["GET /platforms/instagram/accounts/acc_1/summary"] = error(
        403, "platform.instagram.insights_scope_missing", "Reconnect the account."
    )

    stats = provider(vendor).fetch_account_stats(provider_account_id="acc_1", platform="instagram")

    assert stats["insights_permission"] is False
    assert stats["followers"] is None, "unknown, not zero"


def test_a_platform_without_a_summary_reports_its_counts_unknown(vendor: ReadVendor) -> None:
    stats = provider(vendor).fetch_account_stats(provider_account_id="acc_1", platform="facebook")

    assert stats == {"followers": None, "following": None, "total_posts": None}
    assert vendor.calls == []


def test_demographics_become_shares_per_dimension(vendor: ReadVendor) -> None:
    def answer(request: httpx.Request) -> dict[str, Any]:
        breakdown = request.url.params["breakdown"]
        entries = {
            "country": [{"value": "FR", "count": 300}, {"value": "US", "count": 100}],
            "age": [{"value": "25-34", "count": 1}, {"value": "35-44", "count": 1}],
        }.get(breakdown, [])
        return {"data": {"breakdown": breakdown, "entries": entries}}

    vendor.routes["GET /platforms/instagram/accounts/acc_1/demographics"] = answer

    got = provider(vendor).fetch_demographics(platform="instagram", provider_account_id="acc_1")

    assert got == {"COUNTRY": {"FR": 0.75, "US": 0.25}, "AGE": {"25-34": 0.5, "35-44": 0.5}}
    assert sorted(params["breakdown"] for _m, _p, params in vendor.calls) == [
        "age",
        "city",
        "country",
        "gender",
    ]


def test_under_100_followers_there_is_no_breakdown_not_an_empty_one(vendor: ReadVendor) -> None:
    vendor.routes["GET /platforms/instagram/accounts/acc_1/demographics"] = {
        "data": {"entries": []}
    }

    assert (
        provider(vendor).fetch_demographics(platform="instagram", provider_account_id="acc_1")
        is None
    )


def test_platforms_without_demographics_are_not_asked(vendor: ReadVendor) -> None:
    got = provider(vendor).fetch_demographics(platform="youtube", provider_account_id="acc_1")

    assert got is None
    assert vendor.calls == []


# -----------------------------------------------------------------------------
# Account history (rule 4) and Google's performance report
# -----------------------------------------------------------------------------
HISTORY = {
    "data": {
        "from": "2026-10-01",
        "to": "2026-10-05",
        "series": [
            {
                "metric": "follower_count",
                "kind": "gauge",
                "day_tz": "America/Los_Angeles",
                "source": "reported",
                "points": [
                    {"day": "2026-10-01", "value": 1000, "subjects_counted": 1},
                    {
                        "day": "2026-10-02",
                        "value": 1010,
                        "change": 10,
                        "days_spanned": 1,
                        "subjects_counted": 1,
                    },
                    {
                        "day": "2026-10-05",
                        "value": 1040,
                        "change": 30,
                        "days_spanned": 3,
                        "subjects_counted": 1,
                    },
                ],
            },
            {
                "metric": "reach",
                "kind": "flow",
                "day_tz": "UTC",
                "points": [{"day": "2026-10-02", "value": 400, "subjects_counted": 1}],
            },
        ],
    }
}


def test_history_keeps_the_vendors_kind_gap_and_coverage(vendor: ReadVendor) -> None:
    vendor.routes["GET /accounts/acc_1/analytics"] = HISTORY

    days = provider(vendor).fetch_account_history(
        platform="instagram",
        provider_account_id="acc_1",
        start=dt.date(2026, 10, 1),
        end=dt.date(2026, 10, 5),
    )

    gap = next(day for day in days if day.day == dt.date(2026, 10, 5))
    assert (gap.kind, gap.change, gap.days_spanned) == ("gauge", 30, 3)
    first = next(day for day in days if day.day == dt.date(2026, 10, 1))
    assert first.change is None, "the first point has nothing to diff against"
    assert {day.kind for day in days if day.metric == "reach"} == {"flow"}
    assert vendor.calls[0][2] == {"from": "2026-10-01", "to": "2026-10-05"}


def test_google_adds_its_performance_report(vendor: ReadVendor) -> None:
    vendor.routes["GET /accounts/acc_g/analytics"] = {"data": {"series": []}}
    vendor.routes["GET /platforms/google/accounts/acc_g/insights"] = {
        "data": {
            "metrics": [
                {
                    "metric": "CALL_CLICKS",
                    "total": 3,
                    "points": [
                        {"date": "2026-10-01", "value": 1},
                        {"date": "2026-10-02", "value": 2},
                    ],
                }
            ]
        }
    }

    days = provider(vendor).fetch_account_history(
        platform="google_business",
        provider_account_id="acc_g",
        start=dt.date(2026, 10, 1),
        end=dt.date(2026, 10, 2),
    )

    assert [(day.metric, day.day.day, day.value, day.kind) for day in days] == [
        ("CALL_CLICKS", 1, 1, "flow"),
        ("CALL_CLICKS", 2, 2, "flow"),
    ]
