"""Recurrence (P1-10).

**A grid, not a queue.** Every scan recomputes the same slot times from the
rule and creates only the ones nothing covers yet. That is the shape autopilot
already uses, and it buys three properties a queue does not: a worker down for
a week backfills on its next run, a rule edited today takes effect today
rather than after the queued items drain, and two scans racing each other
collide on a unique constraint instead of double-creating.

**Local wall time, not UTC.** "Every Monday at 09:00" means 09:00 on the
office clock. Expanding in UTC would move the slot an hour twice a year, which
is exactly the bug Part 3's "store UTC, convert at edges" rule is about — the
conversion happens at expansion, and what is stored is UTC.

**An RRULE is user input.** `FREQ=SECONDLY` over a 30-day horizon is 2.6
million datetimes and a dead worker, so the frequency floor is validated
rather than trusted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import time_machine
from django.contrib.auth import get_user_model

from billing.models import FeatureFlag
from billing.services.flags import CONTENT_MODEL_V2
from content.models import PostTemplate, RecurrenceOccurrence, RecurrenceRule
from content.services import recurrence
from content.tasks import recurrence_materialise
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

RULES_URL = "/api/v1/recurrence-rules/"
PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture
def template(workspace: Any, user: Any) -> Any:
    return PostTemplate.objects.create(
        workspace=workspace,
        name="Monday spotlight",
        created_by=user,
        payload={"master_body": "Monday spotlight"},
    )


@pytest.fixture
def rule(template: Any) -> Any:
    return RecurrenceRule.objects.create(
        source=template,
        rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        timezone="Europe/Paris",
        horizon_days=21,
    )


# -----------------------------------------------------------------------------
# The grid
# -----------------------------------------------------------------------------
@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_slots_are_the_same_on_every_scan(rule: Any) -> None:
    """The property the uniqueness constraint depends on: two scans must agree
    about slot times exactly, or a duplicate is unrecognisable."""
    assert recurrence.planned_slots(rule) == recurrence.planned_slots(rule)


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_slots_land_on_local_nine_in_the_morning(rule: Any) -> None:
    for slot in recurrence.planned_slots(rule):
        local = slot.astimezone(PARIS)
        assert (local.hour, local.minute, local.weekday()) == (9, 0, 0)


@time_machine.travel("2026-03-20 12:00:00+00:00", tick=False)
def test_a_slot_keeps_local_time_across_a_dst_shift(rule: Any) -> None:
    """Europe/Paris moves to CEST on 2026-03-29, so a window opened on Friday
    the 20th straddles it: 23 March is CET (09:00 local = 08:00 UTC) and every
    Monday after is CEST (07:00 UTC). A rule expanded in UTC would hold the
    UTC hour still and silently start posting at 08:00 local."""
    rule.horizon_days = 28
    slots = recurrence.planned_slots(rule)

    assert {slot.astimezone(PARIS).hour for slot in slots} == {9}, "the local clock moved"
    assert len({slot.hour for slot in slots}) == 2, "the UTC hour must differ either side"


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_slots_stop_at_the_horizon(rule: Any) -> None:
    horizon = dt.datetime(2026, 3, 4, 12, tzinfo=dt.UTC) + dt.timedelta(days=rule.horizon_days)
    slots = recurrence.planned_slots(rule)

    assert slots
    assert all(slot <= horizon for slot in slots)


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_slots_never_reach_into_the_past(rule: Any) -> None:
    """A rule created today must not backfill last year — a grid anchored on
    the rule's own start would otherwise produce a year of posts on its first
    scan."""
    assert all(
        slot >= dt.datetime(2026, 3, 4, 12, tzinfo=dt.UTC)
        for slot in recurrence.planned_slots(rule)
    )


# -----------------------------------------------------------------------------
# Materialisation
# -----------------------------------------------------------------------------
@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_materialising_creates_one_post_per_slot(rule: Any) -> None:
    created = recurrence.materialise_due()

    assert created == len(recurrence.planned_slots(rule))
    assert RecurrenceOccurrence.objects.filter(rule=rule).count() == created
    assert all(occ.post.master_body == "Monday spotlight" for occ in rule.occurrences.all())


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_a_second_scan_creates_nothing(rule: Any) -> None:
    """Idempotence is the whole point of a grid."""
    first = recurrence.materialise_due()
    assert first > 0
    assert recurrence.materialise_due() == 0


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_a_racing_scan_collides_rather_than_double_creating(rule: Any) -> None:
    """Simulates the race by inserting the occurrence a competing scan would
    have written, then running the scan that lost."""
    slot = recurrence.planned_slots(rule)[0]
    recurrence.materialise_slot(rule, slot)
    before = RecurrenceOccurrence.objects.count()

    recurrence.materialise_slot(rule, slot)
    assert RecurrenceOccurrence.objects.count() == before


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_an_inactive_rule_materialises_nothing(rule: Any) -> None:
    rule.active = False
    rule.save(update_fields=["active"])
    assert recurrence.materialise_due() == 0


def test_a_worker_that_was_down_backfills_the_current_window(rule: Any) -> None:
    """Not the missed slots — the *current* window. A grid recomputes; it does
    not replay. Posting last Monday's spotlight on Thursday is worse than not
    posting it."""
    with time_machine.travel("2026-03-04 12:00:00+00:00", tick=False):
        pass  # the scan that never ran

    with time_machine.travel("2026-04-04 12:00:00+00:00", tick=False):
        created = recurrence.materialise_due()
        assert created == len(recurrence.planned_slots(rule))
        assert all(
            occurrence.slot_at >= dt.datetime(2026, 4, 4, 12, tzinfo=dt.UTC)
            for occurrence in rule.occurrences.all()
        )


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_materialised_posts_are_drafts_not_scheduled(rule: Any) -> None:
    """Recurrence plans; it never schedules. Scheduling has horizon, approval
    and quota gates that belong to one service, and a second writer of
    `scheduled_at` is exactly what P1-04's sole-writer rule forbids."""
    recurrence.materialise_due()

    for occurrence in rule.occurrences.select_related("post"):
        assert occurrence.post.status == "DRAFT"
        assert occurrence.post.scheduled_at is None
        assert occurrence.post.delivery_mode == ""


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_the_task_runs_the_scan(rule: Any) -> None:
    assert recurrence_materialise() == len(recurrence.planned_slots(rule))


# -----------------------------------------------------------------------------
# An RRULE is user input
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("rrule", ["FREQ=SECONDLY", "FREQ=MINUTELY;INTERVAL=5", "FREQ=HOURLY"])
def test_a_frequency_below_the_floor_is_rejected(template: Any, rrule: str) -> None:
    """`FREQ=SECONDLY` over a 30-day horizon is 2.6 million datetimes and a
    dead worker. The floor is validated, not trusted."""
    with pytest.raises(recurrence.InvalidRecurrenceError):
        recurrence.validate(rrule=rrule, timezone_name="Europe/Paris")


def test_an_unparseable_rrule_is_rejected(template: Any) -> None:
    with pytest.raises(recurrence.InvalidRecurrenceError):
        recurrence.validate(rrule="not an rrule at all", timezone_name="Europe/Paris")


def test_an_unknown_timezone_is_rejected(template: Any) -> None:
    with pytest.raises(recurrence.InvalidRecurrenceError):
        recurrence.validate(rrule="FREQ=DAILY", timezone_name="Mars/Olympus_Mons")


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_expansion_is_capped_even_if_a_rule_slips_through(template: Any) -> None:
    """Belt and braces. Validation is the gate; the cap is what stops an
    admin-edited or migrated row from taking a worker down with it."""
    rule = RecurrenceRule.objects.create(
        source=template, rrule="FREQ=DAILY", timezone="Europe/Paris", horizon_days=3650
    )
    assert len(recurrence.planned_slots(rule)) == recurrence.MAX_SLOTS_PER_SCAN


# -----------------------------------------------------------------------------
# The endpoints
# -----------------------------------------------------------------------------
def test_creating_a_rule_over_the_api(auth_client: Any, workspace: Any, template: Any) -> None:
    response = auth_client.post(
        RULES_URL,
        {
            "source": template.pk,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "timezone": "Europe/Paris",
            "horizon_days": 30,
        },
        format="json",
    )
    assert response.status_code == 201


def test_creating_a_rule_with_a_bad_rrule_is_400(
    auth_client: Any, workspace: Any, template: Any
) -> None:
    response = auth_client.post(
        RULES_URL,
        {"source": template.pk, "rrule": "FREQ=SECONDLY", "timezone": "UTC", "horizon_days": 30},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_recurrence"


def test_a_rule_cannot_point_at_another_workspaces_template(
    auth_client: Any, workspace: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = PostTemplate.objects.create(workspace=theirs, name="Not yours")

    response = auth_client.post(
        RULES_URL,
        {"source": foreign.pk, "rrule": "FREQ=DAILY", "timezone": "UTC", "horizon_days": 7},
        format="json",
    )
    assert response.status_code == 400


def test_another_workspaces_rule_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign_template = PostTemplate.objects.create(workspace=theirs, name="Not yours")
    foreign = RecurrenceRule.objects.create(
        source=foreign_template, rrule="FREQ=DAILY", timezone="UTC", horizon_days=7
    )

    assert auth_client.get(f"{RULES_URL}{foreign.pk}/").status_code == 404


def test_another_organizations_rule_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign_template = PostTemplate.objects.create(workspace=theirs, name="Not yours")
    foreign = RecurrenceRule.objects.create(
        source=foreign_template, rrule="FREQ=DAILY", timezone="UTC", horizon_days=7
    )
    assert auth_client.get(f"{RULES_URL}{foreign.pk}/").status_code == 404


@time_machine.travel("2026-03-04 12:00:00+00:00", tick=False)
def test_with_the_flag_off_nothing_is_materialised(rule: Any, workspace: Any) -> None:
    """Pre-phase behaviour: recurrence did not exist, so it creates nothing.
    Not an error — the scan simply passes this organization by."""
    FeatureFlag.objects.create(
        organization=workspace.organization, key=CONTENT_MODEL_V2, enabled=False
    )
    assert recurrence.materialise_due() == 0
