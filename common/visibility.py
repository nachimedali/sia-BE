"""Visibility — the third tenancy dimension (BUILD-PLAN Phase 2, P2-03).

Organization and workspace answer *whose data is this*. Visibility answers a
different question inside one tenant: **is this something the team is saying to
itself, or something the client is meant to see.**

There are two audiences and there is deliberately no third:

* **STAFF** — anyone holding a `Membership` in the workspace. Sees everything.
* **CLIENT** — a guest holding a `GUEST_VIEW` token (P2-09), with no account
  and no membership. Sees only what was explicitly marked `SHARED`.

**A client is a guest, not a role.** Phase 0 locked the permission set at seven
words and the role presets at five, so a sixth "client" preset would contradict
it — and it would be the wrong shape anyway: the people a marketing team shows
drafts to are the customer's stakeholders, who do not want an account and
should not appear in a member list. Guest review is a link, and the link's
audience is what this module names. Should a client *membership* ever be
wanted, it resolves to `Audience.CLIENT` in `request_audience` and every
queryset below inherits it without changing.

**Enforced in the queryset, never the serializer** (P2-03). A serializer that
omits a field still loaded the row, still counted it in a paginated total, and
still answered 200 where the honest answer was 404. `VisibilityScopedQuerySetMixin`
composes underneath `WorkspaceScopedQuerySetMixin` so a request narrows by
tenant first and by audience second, and a view gets both by inheriting rather
than by remembering.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from django.db import models
from django.db.models import QuerySet


class Visibility(models.TextChoices):
    """What a row is for. The default everywhere is `INTERNAL`.

    Defaulting the other way would mean a draft becomes client-visible because
    somebody forgot a field — the failure that cannot be walked back, since the
    client has already read it.
    """

    INTERNAL = "INTERNAL", "Team only"
    SHARED = "SHARED", "Shared with clients"


class Audience(StrEnum):
    STAFF = "staff"
    CLIENT = "client"


#: What each audience may load. A mapping rather than a comparison so the
#: answer is a lookup with no ordering implied — visibility is not a rank, and
#: writing it as one is how a third value later gets silently included.
VISIBLE_TO: dict[Audience, frozenset[str]] = {
    Audience.STAFF: frozenset({Visibility.INTERNAL, Visibility.SHARED}),
    Audience.CLIENT: frozenset({Visibility.SHARED}),
}


def request_audience(request: Any) -> Audience:
    """The audience a request speaks to.

    `request.audience` is set by the guest-token views (P2-09) and by nothing
    else. Its absence means an ordinary authenticated member request, which is
    STAFF — the guest path is the exception and has to announce itself, rather
    than staff access being a default that a missing attribute could switch
    off.
    """
    audience = getattr(request, "audience", None)
    return audience if isinstance(audience, Audience) else Audience.STAFF


def visible_values(audience: Audience) -> frozenset[str]:
    return VISIBLE_TO[audience]


class VisibilityScopedQuerySetMixin:
    """Narrows a queryset to what the request's audience may load.

    Set `visibility_field` when the column is reached through a relation — a
    comment's audience is decided by its own row *and* by the thread it hangs
    in, which is why `collaboration.views` declares both.
    """

    visibility_field: str = "visibility"

    def get_queryset(self) -> QuerySet[Any]:
        queryset: QuerySet[Any] = super().get_queryset()  # type: ignore[misc]
        audience = request_audience(self.request)  # type: ignore[attr-defined]
        if audience is Audience.STAFF:
            # Skip the clause rather than filter on every value: an `IN` over
            # the full domain is a no-op that still costs a plan, and staff is
            # the overwhelmingly common path.
            return queryset
        return queryset.filter(**{f"{self.visibility_field}__in": visible_values(audience)})
