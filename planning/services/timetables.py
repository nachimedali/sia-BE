"""Preferred timetables, and the one place local time becomes UTC (P3-11).

**Stored as local wall time plus an IANA zone; resolved at the moment of use.**
"We post at 9am" is a statement about the office clock. Stored as UTC, that
9am silently becomes 8am — or 10am — on one Sunday a year when the zone shifts,
with nobody having edited anything, and the resulting bug reads as the
scheduler being unreliable rather than as a storage decision made months
earlier.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Any

from rest_framework.exceptions import ValidationError

#: Monday is 0, matching `datetime.weekday()`. Picking the same convention the
#: standard library uses means no call site has to remember a second one.
WEEKDAYS = range(7)


def _zone(timezone_name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(timezone_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise ValidationError({"timezone": f"Unknown time zone {timezone_name!r}."}) from error


def _parse_time(value: Any) -> dt.time:
    if not isinstance(value, str):
        raise ValidationError({"slots": "A slot's 'time' is a string like '09:00'."})
    try:
        hour, minute = value.split(":")
        return dt.time(int(hour), int(minute))
    except (ValueError, TypeError) as error:
        raise ValidationError({"slots": f"{value!r} is not a time of day like '09:00'."}) from error


def validate_timetable(slots: Any, *, timezone_name: str) -> list[dict[str, Any]]:
    _zone(timezone_name)

    if not isinstance(slots, list):
        raise ValidationError({"slots": "Slots are a list of {weekday, time} objects."})

    for index, slot in enumerate(slots):
        where = f"Slot {index}"
        if not isinstance(slot, dict):
            raise ValidationError({"slots": f"{where}: each slot is an object."})
        unknown = sorted(set(slot) - {"weekday", "time"})
        if unknown:
            raise ValidationError({"slots": f"{where}: unknown key(s) {', '.join(unknown)}."})
        weekday = slot.get("weekday")
        if isinstance(weekday, bool) or weekday not in WEEKDAYS:
            raise ValidationError({"slots": f"{where}: 'weekday' is 0 (Monday) to 6 (Sunday)."})
        _parse_time(slot.get("time"))

    return slots


def resolve_slot(day: dt.date, *, time_str: str, timezone_name: str) -> dt.datetime:
    """Local wall time on `day` → an aware UTC-comparable datetime.

    **A local time that does not exist is refused, not nudged.** On a
    spring-forward Sunday the clock jumps from 02:00 to 03:00, so 02:30 is not
    a moment in that zone. Guessing costs more than refusing: whichever way it
    is moved the post goes out at a time nobody chose, on one day a year, and
    the report will be that the scheduler is unreliable.

    An *ambiguous* time — the repeated hour in autumn — is not refused, because
    both readings are real moments an hour apart. `fold=0` takes the first,
    which is the earlier of the two and the one a reader means by "2:30am".
    """
    zone = _zone(timezone_name)
    local = dt.datetime.combine(day, _parse_time(time_str), tzinfo=zone)

    # A nonexistent local time is detectable by round-tripping through UTC:
    # the zone maps it to a different wall time than the one asked for.
    if local.astimezone(dt.UTC).astimezone(zone).time() != local.time():
        raise ValidationError(
            {"slots": f"{time_str} does not exist on {day} in {timezone_name} (clocks change)."}
        )

    return local.astimezone(dt.UTC)
