"""ISO 3166-1 alpha-2 markets, read from the IANA tz database's own table.

`tzdata` ships `iso3166.tab` beside the zone files the product already depends
on for every local-time calculation, so the list is maintained upstream and
updated with the same package — not a 249-line literal in this repository that
nobody remembers to revise when a country code changes.
"""

from __future__ import annotations

import importlib.resources
from functools import cache


@cache
def all_markets() -> dict[str, str]:
    """`{"PT": "Portugal", ...}`, in the table's own alphabetical order."""
    table = importlib.resources.files("tzdata").joinpath("zoneinfo", "iso3166.tab").read_text()
    markets: dict[str, str] = {}
    for line in table.splitlines():
        if not line or line.startswith("#"):
            continue
        code, _, name = line.partition("\t")
        markets[code.strip()] = name.strip()
    return markets
