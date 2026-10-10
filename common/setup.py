"""The shape of a setup requirement, shared by every guided-setup surface.

A surface like `/app/autopilot` or `/app/tools` is useless to a new workspace
if it renders an empty list: the feature is not broken, it is unconfigured, and
nothing on the screen says which of the five things it needs are missing. Each
such surface answers `GET .../readiness/` with a list of these rows.

**The row carries state, never copy.** `status` and `summary` come from real
data (`650 credits`, `version 1 saved, not active`), and the title, the
explanation, the illustration and the destination belong to the client that
renders them — the same split `ToolConfigSerializer` already makes for its six
display names. A backend that also owned the wording would be the place two
copies of it drifted apart.

`blocking` separates "the feature cannot run at all" from "you will hit this
one step later": an autopilot with no connected channel still drafts, and
finding out at approval time is worse than being told now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rest_framework import serializers

DONE = "done"
MISSING = "missing"


@dataclass(frozen=True)
class Requirement:
    """One thing a workspace must do before a surface works.

    `target_id` is what an inline action acts on — the inactive profile to
    activate, the product to enable autopilot on. `None` means the client can
    only send the user somewhere; there is nothing single and unambiguous to
    act on.
    """

    key: str
    status: str
    summary: str
    blocking: bool = True
    target_id: int | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status == DONE


def done(key: str, summary: str, **kwargs: Any) -> Requirement:
    return Requirement(key=key, status=DONE, summary=summary, **kwargs)


def missing(key: str, summary: str, **kwargs: Any) -> Requirement:
    return Requirement(key=key, status=MISSING, summary=summary, **kwargs)


def is_ready(requirements: list[Requirement]) -> bool:
    """Blocking rows only: a non-blocking gap is a warning, not a closed door."""
    return all(row.is_done for row in requirements if row.blocking)


class RequirementSerializer(serializers.Serializer[Requirement]):
    key = serializers.CharField()
    status = serializers.ChoiceField(choices=[DONE, MISSING])
    summary = serializers.CharField()
    blocking = serializers.BooleanField()
    target_id = serializers.IntegerField(allow_null=True)
    facts = serializers.DictField()
