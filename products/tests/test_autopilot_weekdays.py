"""Autopilot on chosen weekdays at a time window (X-38).

`/app/tools/autopilot` schedules a product the way a person thinks about it —
"Tuesdays and Saturdays, in the evening" — rather than "every three days from
whenever I switched it on". The grid stays a grid: the same inputs give the
same slots whenever the run happens, so the uniqueness constraint still
recognises a slot already drafted. An empty `weekdays` keeps the old
`cadence_days` grid exactly, so every existing config behaves as before.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from products.models import AutopilotTimeWindow
from products.services import autopilot

pytestmark = pytest.mark.django_db


def _weekly(config: Any, *, weekdays: list[int], window: str, tz: str = "UTC") -> Any:
    workspace = config.product.workspace
    workspace.timezone = tz
    workspace.save(update_fields=["timezone"])
    config.weekdays = weekdays
    config.time_window = window
    config.save()
    return config


def test_slots_fall_on_the_chosen_weekdays_at_the_window_in_local_time(
    autopilot_config: Any,
) -> None:
    config = _weekly(
        autopilot_config, weekdays=[1, 5], window=AutopilotTimeWindow.EVENING, tz="Africa/Tunis"
    )
    # Monday 2026-10-12, 10:00 UTC.
    now = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.UTC)

    slots = autopilot.planned_slots(config, now=now, horizon_days=14)

    local = [slot.astimezone(ZoneInfo("Africa/Tunis")) for slot in slots]
    assert [moment.date().isoformat() for moment in local] == [
        "2026-10-13",
        "2026-10-17",
        "2026-10-20",
        "2026-10-24",
    ]
    assert {(moment.hour, moment.minute) for moment in local} == {(19, 30)}
    assert all(slot > now for slot in slots)


def test_a_slot_already_past_today_is_not_planned(autopilot_config: Any) -> None:
    config = _weekly(autopilot_config, weekdays=[0], window=AutopilotTimeWindow.MORNING)
    # Monday 10:00 UTC: this morning's 09:00 has gone.
    now = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.UTC)

    slots = autopilot.planned_slots(config, now=now, horizon_days=7)

    assert slots == [dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.UTC)]


def test_a_dst_change_keeps_the_local_hour(autopilot_config: Any) -> None:
    """Paris leaves summer time on 2026-10-25: the slot stays at 13:00 on the
    wall clock, so it moves an hour in UTC."""
    config = _weekly(
        autopilot_config, weekdays=[3], window=AutopilotTimeWindow.MIDDAY, tz="Europe/Paris"
    )
    now = dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.UTC)

    slots = autopilot.planned_slots(config, now=now, horizon_days=14)

    assert [slot.astimezone(dt.UTC).hour for slot in slots] == [11, 12]
    assert {slot.astimezone(ZoneInfo("Europe/Paris")).hour for slot in slots} == {13}


def test_the_weekday_grid_is_stable_whenever_the_run_happens(autopilot_config: Any) -> None:
    config = _weekly(autopilot_config, weekdays=[0, 2, 4], window=AutopilotTimeWindow.EVENING)
    now = timezone.now()

    on_time = autopilot.planned_slots(config, now=now, horizon_days=10)
    late = autopilot.planned_slots(config, now=now + dt.timedelta(minutes=5), horizon_days=10)

    assert set(on_time) - set(late) <= {
        slot for slot in on_time if slot <= now + dt.timedelta(minutes=5)
    }
    assert set(late) - set(on_time) <= {slot for slot in late if slot > now + dt.timedelta(days=10)}


def test_no_weekdays_keeps_the_cadence_grid(autopilot_config: Any) -> None:
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    now = timezone.now()

    slots = autopilot.planned_slots(autopilot_config, now=now, horizon_days=9)

    assert len(slots) == 3
    assert {b - a for a, b in itertools.pairwise(slots)} == {dt.timedelta(days=3)}


def test_a_weekday_config_is_due_daily_whatever_its_cadence(autopilot_config: Any) -> None:
    """The lookahead is filled day by day; a week-long cadence would leave the
    days just inside the horizon undrafted until the next tick."""
    config = _weekly(autopilot_config, weekdays=[2], window=AutopilotTimeWindow.EVENING)
    config.cadence_days = 7
    config.save()
    autopilot.run_due(now=timezone.now())

    assert autopilot.due_configs(now=timezone.now() + dt.timedelta(hours=12)) == []
    assert [
        c.pk for c in autopilot.due_configs(now=timezone.now() + dt.timedelta(days=1, minutes=1))
    ] == [config.pk]


# -----------------------------------------------------------------------------
# PATCH /products/{id}/autopilot/
# -----------------------------------------------------------------------------
def test_the_config_endpoint_saves_weekdays_and_window(
    auth_client: Any, autopilot_product: Any, autopilot_config: Any
) -> None:
    url = f"/api/v1/products/{autopilot_product.pk}/autopilot/"

    response = auth_client.patch(
        url, {"weekdays": [5, 1, 1], "time_window": "MIDDAY"}, format="json"
    )

    assert response.status_code == 200
    assert (response.json()["weekdays"], response.json()["time_window"]) == ([1, 5], "MIDDAY")
    autopilot_config.refresh_from_db()
    assert autopilot_config.weekdays == [1, 5]


def test_defaults_are_no_weekdays_and_evening(auth_client: Any, autopilot_product: Any) -> None:
    body = auth_client.get(f"/api/v1/products/{autopilot_product.pk}/autopilot/").json()

    assert (body["weekdays"], body["time_window"]) == ([], "EVENING")


@pytest.mark.parametrize("weekdays", [[7], [-1], ["mon"]])
def test_a_weekday_outside_monday_to_sunday_is_a_400(
    auth_client: Any, autopilot_product: Any, autopilot_config: Any, weekdays: list[Any]
) -> None:
    url = f"/api/v1/products/{autopilot_product.pk}/autopilot/"

    response = auth_client.patch(url, {"weekdays": weekdays}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_an_unknown_time_window_is_a_400(
    auth_client: Any, autopilot_product: Any, autopilot_config: Any
) -> None:
    url = f"/api/v1/products/{autopilot_product.pk}/autopilot/"

    response = auth_client.patch(url, {"time_window": "MIDNIGHT"}, format="json")

    assert response.status_code == 400


def test_another_workspaces_product_schedule_is_a_404(
    autopilot_product: Any, other_user: Any, plans: dict[str, Any]
) -> None:
    from rest_framework.test import APIClient

    from workspaces.services.provisioning import provision_workspace

    other = provision_workspace(other_user, name="Someone Else")
    other.organization.plan = plans["pro"]
    other.organization.save(update_fields=["plan", "updated_at"])
    intruder = APIClient()
    intruder.force_authenticate(other_user)

    response = intruder.patch(
        f"/api/v1/products/{autopilot_product.pk}/autopilot/", {"weekdays": [1]}, format="json"
    )

    assert response.status_code == 404
