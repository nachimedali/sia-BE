"""SA-50/52/53 end to end: the SocialAPI provider behind the real capture paths.

The gate line this file holds (plan §10, SA-90): **no zero from an unavailable
pair reaches a stored capture.** Plus what the daily account pass keeps, and
the insights permission reaching the connections page's reconnect prompt.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import (
    AccountMetricDay,
    AccountSnapshot,
    AudienceComment,
    AudienceDemographic,
    Availability,
    PostMetric,
)
from analytics.providers.base import MetricsProviderRegistry
from analytics.services import account_history, ingest
from analytics.tests.conftest import make_target
from channels.models import SocialAccount
from social_providers.socialapi.metrics import SocialAPIMetricsProvider
from social_providers.tests.stub import ReadVendor, error, metrics_answer, seed_capabilities

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _capabilities() -> None:
    seed_capabilities()


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> ReadVendor:
    """SocialAPI serves measurement; its audience port is not built yet."""
    stub = ReadVendor()

    def registry() -> MetricsProviderRegistry:
        return MetricsProviderRegistry(
            [SocialAPIMetricsProvider(stub.client())], audience_ready=False
        )

    monkeypatch.setattr(ingest, "get_metrics_registry", registry)
    return stub


def ago(hours: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()


def _target(workspace: Any, user: Any, account: Any, *, age_days: int = 3) -> Any:
    return make_target(workspace, user, account, age_days=age_days, provider_post_id="p-1")


def _rung(target: Any) -> dt.datetime:
    rung = ingest.due_rung(target, 90, now=timezone.now())
    assert rung is not None
    return rung


# -----------------------------------------------------------------------------
# Post captures
# -----------------------------------------------------------------------------
def test_no_zero_from_an_unavailable_pair_reaches_a_capture(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    social_account.platform = "facebook"
    social_account.save(update_fields=["platform"])
    target = _target(paid_workspace, user, social_account)
    vendor.routes["GET /posts/p-1/metrics"] = metrics_answer(
        platform="facebook", likes=15, comments=3, shares=7, saves=0, reach=0
    )

    row = ingest.capture_target(target, _rung(target))

    assert row is not None and row.availability == Availability.MEASURED
    assert (row.likes, row.comments, row.shares) == (15, 3, 7)
    assert row.saves is None and row.reach is None and row.impressions is None
    assert row.provider_key == "socialapi"
    assert row.raw["response"]["data"]["targets"][0]["metrics"]["saves"] == 0, "kept for audit"


def test_a_pending_answer_writes_nothing_and_the_rung_stays_owed(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    target = _target(paid_workspace, user, social_account, age_days=0)
    target.published_at = timezone.now() - dt.timedelta(hours=2)
    target.save(update_fields=["published_at"])
    vendor.routes["GET /posts/p-1/metrics"] = metrics_answer(published_at=ago(2), synced=False)
    rung = _rung(target)

    assert ingest.capture_target(target, rung) is None
    assert not PostMetric.objects.filter(post_target=target).exists()
    assert ingest.due_rung(target, 90, now=timezone.now()) == rung


def test_a_post_the_provider_lost_is_recorded_unavailable_without_numbers(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    target = _target(paid_workspace, user, social_account)
    vendor.routes["GET /posts/p-1/metrics"] = error(404, "resource.not_found")

    row = ingest.capture_target(target, _rung(target))

    assert row is not None and row.availability == Availability.UNAVAILABLE
    assert row.likes is None and row.engagement_rate is None


# -----------------------------------------------------------------------------
# SA-53: the insights permission reaches the reconnect prompt
# -----------------------------------------------------------------------------
def test_withheld_insights_flag_the_account_for_a_reconnect(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    from channels.overview import account_cards

    target = _target(paid_workspace, user, social_account)
    vendor.routes["GET /posts/p-1/metrics"] = metrics_answer(
        published_at=ago(72), likes=42, comments=5, shares=0, saves=0
    )

    row = ingest.capture_target(target, _rung(target))

    social_account.refresh_from_db()
    assert social_account.capabilities["insights"] is False
    assert social_account.reconnect_reason == ingest.INSIGHTS_RECONNECT_REASON
    assert row is not None and row.reach is None and row.saves is None
    assert account_cards(paid_workspace)[0]["insights_off"] is True


def test_insights_coming_back_clear_the_prompt(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    social_account.capabilities = {"insights": False}
    social_account.reconnect_reason = ingest.INSIGHTS_RECONNECT_REASON
    social_account.save(update_fields=["capabilities", "reconnect_reason"])
    target = _target(paid_workspace, user, social_account)
    vendor.routes["GET /posts/p-1/metrics"] = metrics_answer(likes=4, reach=90, views=150)

    ingest.capture_target(target, _rung(target))

    social_account.refresh_from_db()
    assert social_account.capabilities["insights"] is True
    assert social_account.reconnect_reason == ""


def test_a_platforms_own_reconnect_reason_is_never_overwritten(social_account: Any) -> None:
    social_account.reconnect_reason = "Your Instagram password changed."
    social_account.save(update_fields=["reconnect_reason"])

    ingest.note_insights_permission(social_account, False)

    social_account.refresh_from_db()
    assert social_account.reconnect_reason == "Your Instagram password changed."
    assert social_account.capabilities["insights"] is False


def test_a_reconnect_forgets_what_the_last_sign_in_lacked(
    paid_workspace: Any, social_account: Any
) -> None:
    from channels.adapters.base import ConnectedAccount
    from channels.services import attach_account

    social_account.capabilities = {"insights": False, "beta": True}
    social_account.save(update_fields=["capabilities"])

    attach_account(
        workspace=paid_workspace,
        platform="instagram",
        connected=ConnectedAccount(
            provider_account_id=social_account.provider_account_id,
            handle="@acme",
            display_name="Acme Studio",
            followers=10,
        ),
    )

    social_account.refresh_from_db()
    assert social_account.capabilities == {"beta": True}


# -----------------------------------------------------------------------------
# The daily account pass (SA-52)
# -----------------------------------------------------------------------------
def _instagram_account_routes(vendor: ReadVendor, account_id: str) -> None:
    vendor.routes[f"GET /platforms/instagram/accounts/{account_id}/summary"] = {
        "follower_count": 1040,
        "following_count": 87,
        "media_count": 12,
    }
    vendor.routes[f"GET /platforms/instagram/accounts/{account_id}/demographics"] = {
        "data": {"entries": [{"value": "FR", "count": 3}, {"value": "IT", "count": 1}]}
    }
    vendor.routes[f"GET /accounts/{account_id}/analytics"] = {
        "data": {
            "series": [
                {
                    "metric": "follower_count",
                    "kind": "gauge",
                    "points": [
                        {"day": "2026-10-01", "value": 1000, "subjects_counted": 2},
                        {
                            "day": "2026-10-04",
                            "value": 1030,
                            "change": 30,
                            "days_spanned": 3,
                            "subjects_counted": 1,
                        },
                        {
                            "day": "2026-10-05",
                            "value": 1040,
                            "change": 10,
                            "days_spanned": 1,
                            "subjects_counted": 2,
                        },
                    ],
                }
            ]
        }
    }


def test_the_daily_pass_keeps_counts_demographics_and_history(
    vendor: ReadVendor, social_account: Any
) -> None:
    _instagram_account_routes(vendor, social_account.provider_account_id)

    assert ingest.snapshot_accounts() == 1

    snapshot = AccountSnapshot.objects.get(social_account=social_account)
    assert (snapshot.followers, snapshot.following, snapshot.total_posts) == (1040, 87, 12)
    social_account.refresh_from_db()
    assert social_account.followers_cached == 1040
    assert social_account.capabilities["insights"] is True
    country = AudienceDemographic.objects.get(social_account=social_account, dimension="COUNTRY")
    assert country.breakdown == {"FR": 0.75, "IT": 0.25}
    assert country.availability == Availability.MEASURED

    days = {row.day.day: row for row in AccountMetricDay.objects.filter(metric="follower_count")}
    assert days[4].per_day == 10.0, "a three-day change is spread, never drawn as a spike"
    assert days[4].partial is True, "one subject of two reported that day"
    assert days[5].partial is False
    assert days[1].per_day is None


def test_net_change_follows_the_gauge_and_says_unknown_without_history(
    vendor: ReadVendor, social_account: Any
) -> None:
    _instagram_account_routes(vendor, social_account.provider_account_id)
    ingest.snapshot_accounts()

    moved = account_history.net_change(
        social_account,
        "follower_count",
        start=dt.date(2026, 10, 1),
        end=dt.date(2026, 10, 5),
    )

    assert moved == 40
    assert (
        account_history.net_change(
            social_account, "reach", start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 5)
        )
        is None
    )


def test_a_later_reading_of_a_day_replaces_the_earlier_one(
    vendor: ReadVendor, social_account: Any
) -> None:
    _instagram_account_routes(vendor, social_account.provider_account_id)
    ingest.snapshot_accounts()
    route = f"GET /accounts/{social_account.provider_account_id}/analytics"
    vendor.routes[route]["data"]["series"][0]["points"][-1]["value"] = 1045
    AccountSnapshot.objects.all().delete()

    ingest.snapshot_accounts()

    assert AccountMetricDay.objects.get(day=dt.date(2026, 10, 5)).value == 1045
    assert AccountMetricDay.objects.filter(metric="follower_count").count() == 3
    windows = [params for _m, path, params in vendor.calls if path == route.split(" ")[1]]
    first, second = (dt.date.fromisoformat(w["from"]) for w in windows)
    assert (dt.date.fromisoformat(windows[0]["to"]) - first).days == 29, "first read backfills"
    assert (dt.date.fromisoformat(windows[1]["to"]) - second).days == 6, "later reads re-read"


def test_a_summary_refused_for_the_permission_still_snapshots_unknown_counts(
    vendor: ReadVendor, social_account: Any
) -> None:
    account_id = social_account.provider_account_id
    vendor.routes[f"GET /platforms/instagram/accounts/{account_id}/summary"] = error(
        403, "platform.instagram.insights_scope_missing"
    )
    vendor.routes[f"GET /platforms/instagram/accounts/{account_id}/demographics"] = error(
        403, "platform.instagram.insights_scope_missing"
    )

    ingest.snapshot_accounts()

    social_account.refresh_from_db()
    assert social_account.capabilities["insights"] is False
    assert AccountSnapshot.objects.get(social_account=social_account).followers is None
    assert set(
        AudienceDemographic.objects.filter(social_account=social_account).values_list(
            "availability", flat=True
        )
    ) == {Availability.UNAVAILABLE}


# -----------------------------------------------------------------------------
# Comments wait for their own port
# -----------------------------------------------------------------------------
def test_comments_wait_for_the_audience_port_instead_of_reading_as_none(
    vendor: ReadVendor, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    target = _target(paid_workspace, user, social_account)

    assert ingest.capture_comments(target) == 0
    assert not AudienceComment.objects.filter(post_target=target).exists()


def test_accounts_of_another_workspace_are_read_with_their_own_ids(
    vendor: ReadVendor, social_account: Any, other_workspace: Any
) -> None:
    _workspace, _user, other = other_workspace
    _instagram_account_routes(vendor, social_account.provider_account_id)
    _instagram_account_routes(vendor, other.provider_account_id)

    assert ingest.snapshot_accounts() == 2
    for account in SocialAccount.objects.all():
        assert AccountSnapshot.objects.filter(social_account=account).count() == 1
        assert AccountMetricDay.objects.filter(social_account=account).count() == 3
