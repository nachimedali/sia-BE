"""Saved views (P3-10).

The filter declaration is **shared with the list endpoint** rather than
duplicated: two declarations of what may be filtered is the drift P1-16 refused
for `rules.py`, and here it would mean a view that saves cleanly and then
filters nothing.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

from billing.services.entitlements import entitlements_for
from content.models import ContentKind, PostStatus
from planning.models import SavedView
from workspaces.models import Workspace

#: Filter key → the values it accepts, or `None` where any string will do.
#: A saved view carrying a key nothing applies would silently show everything,
#: which is the most dangerous failure available to a view called "Awaiting me".
FILTER_KEYS: dict[str, frozenset[str] | None] = {
    "status": frozenset(PostStatus.values),
    "content_kind": frozenset(ContentKind.values),
    "label": None,
    "campaign": None,
    "author": None,
    "platform": None,
}


def validate_filters(filters: Any) -> dict[str, Any]:
    if not isinstance(filters, dict):
        raise ValidationError({"filters": "Filters are an object of key → list of values."})

    unknown = sorted(set(filters) - set(FILTER_KEYS))
    if unknown:
        raise ValidationError(
            {"filters": f"Unknown filter(s): {', '.join(unknown)}.", "allowed": sorted(FILTER_KEYS)}
        )

    for key, values in filters.items():
        if not isinstance(values, list):
            raise ValidationError({"filters": f"'{key}' takes a list of values."})
        allowed = FILTER_KEYS[key]
        if allowed is None:
            continue
        bad = sorted({str(value) for value in values} - allowed)
        if bad:
            raise ValidationError({"filters": f"'{key}': unknown value(s) {', '.join(bad)}."})

    return filters


def create_saved_view(*, workspace: Workspace, name: str, filters: Any) -> SavedView:
    entitlements_for(workspace).check_quota(
        "included_views", SavedView.objects.filter(workspace=workspace).count()
    )
    validate_filters(filters)
    return SavedView.objects.create(workspace=workspace, name=name, filters=filters)
