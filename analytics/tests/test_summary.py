"""The analytics page in one read (`GET /analytics/summary/`, SA-70).

What the page promises, asserted: a figure is measured, pending or unavailable
with its reason — never a zero standing in for "not told"; totals sum measured
values only and name what they left out; the change is like for like; a fake
row is never a number; a window longer than the plan keeps is refused, not
clipped; and another workspace's posts never appear.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import Availability, MetricCapability, MetricSource, PostMetric
from analytics.services import summary as summary_service
from analytics.tests.conftest import make_target
from channels.models import SocialAccount, SocialAccountStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/analytics/summary/"


def _caps(platform: str = "instagram", provider: str = "socialapi", **overrides: Any) -> None:
    rows = {
        "reach": (True, 48, ""),
        "views": (True, 48, ""),
        "likes": (True, 0, ""),
        "comments": (True, 0, ""),
        "shares": (True, 0, ""),
        "saves": (False, 0, "Not reported by this platform"),
    }
    rows.update(overrides)
    for metric, (available, delay, note) in rows.items():
        MetricCapability.objects.update_or_create(
            provider_key=provider,
            platform=platform,
            metric_key=metric,
            defaults={
                "available": available,
                "latency_hint": dt.timedelta(hours=delay) if delay else None,
                "note": note,
            },
        )


def _at(target: Any, hours: float) -> dt.datetime:
    published: dt.datetime = target.published_at
    return published + dt.timedelta(hours=hours)


def _measure(target: Any, *, hours: float = 72, **values: int | None) -> PostMetric:
    base = {"reach": 1000, "views": 2000, "likes": 50, "comments": 5, "shares": 3, "saves": None}
    base.update(values)
    return PostMetric.objects.create(
        post_target=target,
        captured_at=_at(target, hours),
        engagement_rate=0.05,
        provider_key="socialapi",
        **base,
    )


@pytest.fixture
def account(social_account: Any) -> Any:
    SocialAccount.objects.filter(pk=social_account.pk).update(provider_key="socialapi")
    social_account.refresh_from_db()
    _caps()
    return social_account


def _post(body: dict[str, Any], target_id: int) -> dict[str, Any]:
    return next(row for row in body["posts"] if row["target_id"] == target_id)


# -----------------------------------------------------------------------------
# Cells
# -----------------------------------------------------------------------------
def test_a_measured_figure_is_a_number_and_a_measured_zero_stays_zero(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=10)
    _measure(target, comments=0)

    row = _post(auth_client.get(URL).json(), target.pk)

    assert row["cells"]["reach"] == {"state": "measured", "value": 1000.0, "why": ""}
    assert row["cells"]["comments"]["state"] == "measured"
    assert row["cells"]["comments"]["value"] == 0


def test_a_metric_the_platform_cannot_report_is_unavailable_with_its_reason(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=10)
    _measure(target, saves=0)  # the vendor's 0 for a pair it cannot read

    cell = _post(auth_client.get(URL).json(), target.pk)["cells"]["saves"]

    assert cell["state"] == "unavailable"
    assert cell["value"] is None
    assert cell["why"] == "Not reported by this platform"


def test_inside_the_reporting_delay_a_metric_is_pending_not_zero(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=1)
    _measure(target, hours=6, reach=0)

    cell = _post(auth_client.get(URL).json(), target.pk)["cells"]["reach"]

    assert cell["state"] == "pending"
    assert cell["value"] is None
    assert "48 h after posting" in cell["why"]


def test_before_the_first_capture_every_figure_is_pending(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=3)

    row = _post(auth_client.get(URL).json(), target.pk)

    assert row["cells"]["likes"]["state"] == "pending"
    assert row["capture"] == "pending"


def test_an_account_without_insights_shows_reach_unavailable_with_the_fix(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    SocialAccount.objects.filter(pk=account.pk).update(capabilities={"insights": False})
    target = make_target(paid_workspace, user, account, age_days=10)
    _measure(target)

    cell = _post(auth_client.get(URL).json(), target.pk)["cells"]["reach"]

    assert cell["state"] == "unavailable"
    assert "Reconnect" in cell["why"]


def test_an_unavailable_answer_is_never_a_number(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=10)
    PostMetric.objects.create(
        post_target=target,
        captured_at=_at(target, 72),
        provider_key="socialapi",
        availability=Availability.UNAVAILABLE,
    )

    row = _post(auth_client.get(URL).json(), target.pk)

    assert row["cells"]["likes"]["state"] == "pending"  # no measured capture yet
    assert row["cells"]["likes"]["value"] is None


def test_a_fake_adapter_row_never_becomes_a_number(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    """Part 7 rule 17: the fake is a test fixture, not a measurement."""
    target = make_target(paid_workspace, user, account, age_days=10)
    PostMetric.objects.create(
        post_target=target,
        captured_at=_at(target, 72),
        likes=999,
        source=MetricSource.FAKE,
        provider_key="fake",
    )

    body = auth_client.get(URL).json()

    assert _post(body, target.pk)["cells"]["likes"]["value"] is None
    assert body["totals"]["comments"]["total"] is None


def test_engagement_rate_is_computed_from_reach_and_needs_it(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    measured = make_target(paid_workspace, user, account, age_days=10)
    _measure(measured, reach=1000, likes=50, comments=5, shares=3)
    early = make_target(paid_workspace, user, account, age_days=1)
    _measure(early, hours=6)

    body = auth_client.get(URL).json()

    assert _post(body, measured.pk)["engagement_rate"]["value"] == 5.8
    assert _post(body, early.pk)["engagement_rate"]["state"] == "pending"


# -----------------------------------------------------------------------------
# Capture state and snapshots
# -----------------------------------------------------------------------------
def test_a_post_whose_first_week_was_captured_is_complete(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=20)
    for hours in (1, 6, 24, 72, 168):
        _measure(target, hours=hours)

    row = _post(auth_client.get(URL).json(), target.pk)

    assert row["capture"] == "complete"
    assert [s["state"] for s in row["snapshots"]] == ["captured"] * 5


def test_a_missed_rung_makes_the_capture_partial(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=20)
    for hours in (1, 6, 72, 168):
        _measure(target, hours=hours)

    row = _post(auth_client.get(URL).json(), target.pk)

    assert row["capture"] == "partial"
    assert [s["state"] for s in row["snapshots"]][2] == "missed"


def test_an_account_needing_reconnect_marks_its_posts_stale(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=20)
    _measure(target)
    SocialAccount.objects.filter(pk=account.pk).update(status=SocialAccountStatus.REAUTH_REQUIRED)

    assert _post(auth_client.get(URL).json(), target.pk)["capture"] == "stale"


# -----------------------------------------------------------------------------
# Totals, daily and the change
# -----------------------------------------------------------------------------
def test_totals_sum_measured_figures_only_and_name_what_they_left_out(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    _caps(platform="tiktok", reach=(False, 0, "TikTok reports views, not reach"))
    tiktok = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="tiktok",
        handle="@acme-tt",
        provider_account_id="tt-1",
        provider_key="socialapi",
    )
    _measure(make_target(paid_workspace, user, account, age_days=10), reach=1000)
    _measure(make_target(paid_workspace, user, account, age_days=12), reach=500)
    _measure(make_target(paid_workspace, user, tiktok, age_days=10), reach=0)

    reach = auth_client.get(URL).json()["totals"]["reach"]

    assert reach["total"] == 1500
    assert reach["accounts_counted"] == 1
    assert reach["excluded"] == [{"account": "@acme-tt", "why": "TikTok reports views, not reach"}]


def test_a_total_with_nothing_measured_is_null_not_zero(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    make_target(paid_workspace, user, account, age_days=3)

    totals = auth_client.get(URL).json()["totals"]

    assert totals["reach"]["total"] is None
    assert totals["reach"]["pending_days"] == 1


def test_daily_bars_are_by_publish_day_and_split_by_platform(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    target = make_target(paid_workspace, user, account, age_days=5)
    _measure(target, views=700)

    body = auth_client.get(URL, {"window": 7}).json()
    day = timezone.localtime(target.published_at).date().isoformat()
    bar = next(row for row in body["daily"]["views"] if row["date"] == day)

    assert len(body["daily"]["views"]) == 7
    assert bar["total"] == 700
    assert bar["by_platform"] == {"instagram": 700}
    assert bar["posts"] == 1
    empty = next(row for row in body["daily"]["views"] if row["posts"] == 0)
    assert empty["total"] is None


def test_the_change_compares_like_for_like(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    newcomer = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="instagram",
        handle="@new",
        provider_account_id="ig-new",
        provider_key="socialapi",
    )
    _measure(make_target(paid_workspace, user, account, age_days=3), views=1500)
    _measure(make_target(paid_workspace, user, account, age_days=10), views=1000)
    # Only in the current window — left out of the comparison.
    _measure(make_target(paid_workspace, user, newcomer, age_days=2), views=9000)

    change = auth_client.get(URL, {"window": 7}).json()["totals"]["views"]["change"]

    assert change == {"previous": 1000, "current": 1500, "percent": 50, "accounts_left_out": 1}


def test_no_comparison_when_the_previous_window_is_outside_the_history(
    auth_client: Any, paid_workspace: Any, user: Any, account: Any
) -> None:
    body = auth_client.get(URL, {"window": 90}).json()

    assert body["comparable"] is False
    assert body["totals"]["reach"]["change"] is None


# -----------------------------------------------------------------------------
# The window, the accounts and the boundary
# -----------------------------------------------------------------------------
def test_windows_beyond_the_plan_are_offered_locked(auth_client: Any, paid_workspace: Any) -> None:
    body = auth_client.get(URL).json()

    assert body["window"] == 28
    assert body["history_days"] == 90
    assert {w["days"]: w["locked"] for w in body["windows"]} == {
        7: False,
        28: False,
        90: False,
        180: True,
        365: True,
    }


def test_a_window_beyond_the_plan_is_refused_not_clipped(
    auth_client: Any, paid_workspace: Any
) -> None:
    response = auth_client.get(URL, {"window": 180})

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "analytics_horizon_exceeded"
    assert error["detail"]["horizon_days"] == 90


def test_an_unknown_window_is_a_400(auth_client: Any, paid_workspace: Any) -> None:
    assert auth_client.get(URL, {"window": 30}).status_code == 400
    assert auth_client.get(URL, {"window": "week"}).status_code == 400


def test_a_short_plan_reads_its_own_short_window(
    auth_client: Any, workspace: Any, plans: dict[str, Any]
) -> None:
    # The legacy free row: seven days of history, and the page itself.
    workspace.organization.plan = plans["free"]
    workspace.organization.save(update_fields=["plan"])

    body = auth_client.get(URL).json()

    assert body["window"] == 7
    assert body["history_days"] == 7


def test_the_account_strip_holds_history_up_to_the_plan(
    auth_client: Any, paid_workspace: Any, account: Any
) -> None:
    SocialAccount.objects.filter(pk=account.pk).update(
        connected_at=timezone.now() - dt.timedelta(days=400)
    )

    strip = auth_client.get(URL).json()["accounts"]

    assert strip[0]["history_days_held"] == 90
    assert strip[0]["followers_change_7d"] is None


def test_another_workspaces_posts_never_appear(
    auth_client: Any, paid_workspace: Any, other_workspace: Any
) -> None:
    workspace, owner, theirs = other_workspace
    _measure(make_target(workspace, owner, theirs, age_days=5))

    body = auth_client.get(URL).json()

    assert body["posts"] == []
    assert body["accounts"] == []


def test_another_workspace_of_the_same_org_is_scoped_by_the_header(
    auth_client: Any, user: Any, paid_workspace: Any, account: Any
) -> None:
    from workspaces.services.provisioning import provision_extra_workspace

    second = provision_extra_workspace(user=user, name="Second Brand")
    _measure(make_target(paid_workspace, user, account, age_days=5))

    body = auth_client.get(URL, HTTP_X_WORKSPACE_ID=str(second.pk)).json()

    assert body["posts"] == []


def test_unauthenticated_is_401() -> None:
    from rest_framework.test import APIClient

    assert APIClient().get(URL).status_code == 401


def test_the_service_resolves_the_default_window_to_the_plan(paid_workspace: Any) -> None:
    assert summary_service.resolve_window(paid_workspace, None) == (28, 90)


@pytest.mark.parametrize(
    ("history", "expected"), [(7, 7), (14, 7), (30, 28), (90, 28), (730, 28), (3, 7), (-1, 28)]
)
def test_the_default_window_is_always_one_the_page_offers(history: int, expected: int) -> None:
    assert summary_service.default_window(history) == expected
