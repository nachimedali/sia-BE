"""Recurrence (P1-10).

**A grid, not a queue.** Every scan recomputes the same slot times from the
rule and materialises only what nothing covers yet. That is the shape
`products.services.autopilot` already uses, and it buys three properties a
queue cannot: a worker that was down for a week backfills on its next run, a
rule edited today takes effect today rather than after the queue drains, and
two scans racing each other collide on `unique_recurrence_slot` instead of
double-creating.

It also means recurrence does **not replay**. A scan recomputes the *current*
window; slots that passed while nothing was running are gone. Posting last
Monday's spotlight on Thursday is worse than not posting it.

**Local wall time in, UTC out.** "Every Monday at 09:00" means 09:00 on the
office clock, so the rule is expanded naive in its own zone and each occurrence
is localised afterwards. Expanding in UTC would move the slot an hour twice a
year — precisely what Part 3's "store UTC, convert at edges" is about.

**An RRULE is user input.** `FREQ=SECONDLY` over a 30-day horizon is 2.6
million datetimes and a dead worker, so the frequency floor is validated at the
edge and the expansion is capped underneath it.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.services.flags import CONTENT_MODEL_V2, flag_enabled
from common.exceptions import OCCSError
from content.models import Post, RecurrenceOccurrence, RecurrenceRule
from content.services.templates import apply_template

logger = logging.getLogger(__name__)

#: The finest cadence a rule may ask for. Daily, because this materialises a
#: *post* per slot and nothing below a day is a content calendar — it is a
#: mistake or an attack. The names are dateutil's own vocabulary.
FORBIDDEN_FREQUENCIES = frozenset({"SECONDLY", "MINUTELY", "HOURLY"})

#: A hard ceiling under the validation above, so an admin-edited or migrated
#: row cannot take a worker down. Sized well past any real calendar: 30 days of
#: four-a-day is 120.
MAX_SLOTS_PER_SCAN = 500


class InvalidRecurrenceError(OCCSError):
    default_code = "invalid_recurrence"
    default_detail = "This recurrence rule cannot be used."


def validate(*, rrule: str, timezone_name: str) -> None:
    """Raises `InvalidRecurrenceError` if the rule could not be safely
    expanded. Called from the serializer, so a bad rule is a 400 rather than a
    worker that never finishes."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidRecurrenceError(
            f"Unknown timezone '{timezone_name}'.", detail={"timezone": timezone_name}
        ) from exc

    upper = rrule.upper()
    for frequency in FORBIDDEN_FREQUENCIES:
        if f"FREQ={frequency}" in upper:
            raise InvalidRecurrenceError(
                f"{frequency.title()} recurrence is not supported; the finest cadence is daily.",
                detail={"rrule": rrule},
            )

    try:
        rrulestr(rrule, dtstart=dt.datetime(2026, 1, 1, tzinfo=None))
    except Exception as exc:  # dateutil raises bare ValueError/TypeError variants
        raise InvalidRecurrenceError(
            "This is not a valid RFC 5545 recurrence rule.", detail={"rrule": rrule}
        ) from exc

    # Referenced so a timezone that parses but resolves to nothing still fails
    # here rather than at the first scan.
    assert zone is not None


def planned_slots(rule: RecurrenceRule, *, now: dt.datetime | None = None) -> list[dt.datetime]:
    """Every slot between now and the horizon, in UTC.

    Deterministic: two scans over the same window must agree exactly, or the
    uniqueness constraint stops being able to recognise a duplicate.
    """
    moment = now or timezone.now()
    zone = ZoneInfo(rule.timezone)
    start_local = moment.astimezone(zone).replace(tzinfo=None)
    end_local = (
        (moment + dt.timedelta(days=rule.horizon_days)).astimezone(zone).replace(tzinfo=None)
    )

    try:
        occurrences = rrulestr(rule.rrule, dtstart=start_local)
    except Exception:
        logger.exception("unparseable recurrence rule", extra={"rule_id": rule.pk})
        return []

    slots: list[dt.datetime] = []
    for naive in occurrences:
        if naive > end_local:
            break
        if len(slots) >= MAX_SLOTS_PER_SCAN:
            logger.warning(
                "recurrence expansion hit the slot ceiling",
                extra={"rule_id": rule.pk, "ceiling": MAX_SLOTS_PER_SCAN},
            )
            break
        # `fold=0` resolves the ambiguous hour an autumn DST shift creates to
        # the first occurrence. Either answer is defensible; picking one and
        # staying with it is what keeps two scans agreeing.
        slots.append(naive.replace(tzinfo=zone, fold=0).astimezone(dt.UTC))
    return slots


def materialise_slot(rule: RecurrenceRule, slot: dt.datetime) -> Post | None:
    """Creates the post for one slot, or returns `None` if it already exists.

    The `IntegrityError` catch is the race handler, not a fallback: two scans
    can both see the slot as uncovered and both try to write it. One wins on
    `unique_recurrence_slot` and the other finds out here, which is exactly
    what the grid is for.
    """
    author = rule.source.created_by or rule.source.workspace.organization.owner
    try:
        with transaction.atomic():
            if RecurrenceOccurrence.objects.filter(rule=rule, slot_at=slot).exists():
                return None
            post = apply_template(rule.source, author=author)
            RecurrenceOccurrence.objects.create(rule=rule, slot_at=slot, post=post)
            return post
    except IntegrityError:
        logger.info(
            "recurrence slot was taken by a concurrent scan",
            extra={"rule_id": rule.pk, "slot_at": slot.isoformat()},
        )
        return None


def materialise_due(*, now: dt.datetime | None = None) -> int:
    """Fills every active rule's window. Returns how many posts were created."""
    moment = now or timezone.now()
    created = 0

    rules = RecurrenceRule.objects.filter(active=True).select_related(
        "source", "source__workspace", "source__workspace__organization"
    )
    # One flag lookup per organization, not per rule — an org running several
    # recurring templates would otherwise ask the same question once per rule
    # in every scan.
    flag_by_org: dict[int, bool] = {}
    for rule in rules:
        organization = rule.source.workspace.organization
        enabled = flag_by_org.get(organization.pk)
        if enabled is None:
            enabled = flag_enabled(organization, CONTENT_MODEL_V2)
            flag_by_org[organization.pk] = enabled
        if not enabled:
            # Pre-phase behaviour: recurrence did not exist, so this
            # organization is passed by rather than erroring.
            continue
        covered = set(
            RecurrenceOccurrence.objects.filter(rule=rule).values_list("slot_at", flat=True)
        )
        for slot in planned_slots(rule, now=moment):
            if slot in covered:
                continue
            if materialise_slot(rule, slot) is not None:
                created += 1
    return created
