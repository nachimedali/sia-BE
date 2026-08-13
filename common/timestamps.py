"""Vendor timestamp parsing.

Every external provider encodes time differently — epoch seconds, ISO with a
`Z`, ISO with an offset, naive ISO — and three places in this system now have to
read one: the trend adapters, the publishing adapter's comment feed, and
whatever comes next. The parsing is shared; what to do when it fails is not, so
each caller wraps this with its own answer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def parse_or_none(value: Any) -> dt.datetime | None:
    """A timezone-aware datetime, or `None` when the value is unreadable.

    A naive ISO string is assumed UTC: every provider in play reports UTC, and
    guessing a local zone would be worse than assuming the documented one.
    """
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None
