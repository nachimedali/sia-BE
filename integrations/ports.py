"""Ports for work that is moving to the trendgen service (BUILD-PLAN L11-L12).

Trend collection and creative generation leave ComposeVision and are served by
a standalone multi-tenant service. This module is the seam: today both ports
resolve to in-process adapters over `trends/` and `ai/`, so nothing changes;
when trendgen serves them, an HTTP adapter is selected by feature flag and the
old apps are deleted (Phase 12).

**Why a DTO for trends and a model for generation.**

`TrendFeedPort` returns plain dataclasses. Nothing in ComposeVision holds a
foreign key to a `TrendItem` or a `TrendCluster`; they are read-only inputs to
a prompt, so a remote implementation can satisfy the contract with JSON.

`GenerationPort` returns an `ai.models.Generation` instance, and that is
deliberate rather than a leak. `AutopilotDraft.generation` and
`ContentCandidate.generation` are foreign keys to that row. The row is local
bookkeeping — who asked, what it cost, what came back — and it stays local no
matter where the pixels are produced. The HTTP adapter will create the same row
as a receipt carrying the remote generation id, so those FKs keep pointing at
something real. A port that returned a DTO here would force a schema change on
two apps to serve an abstraction, which is the wrong trade.

**What is not here, and why.**

`ai/providers/llm_text.py` stays in ComposeVision. Its callers — digest
narration in `learn/`, `analytics/services/sentiment.py`, `tools/services.py` —
read decision logs, findings and audience comments. Part 7 rule 16 forbids raw
tenant content crossing a tenant boundary, so those calls cannot become
requests to a shared service, whatever else moves.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from common.exceptions import OCCSError
from trends.services.extraction import WINDOW_DAYS

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser as User

    from ai.models import Generation
    from categories.models import Category
    from products.models import Product
    from workspaces.models import Workspace


#: The corpus window callers ask for. Taken from the pipeline rather than
#: restated: the seam is the one module allowed to know both sides, so there is
#: no second literal and no drift test to keep honest. It becomes a literal here
#: when Phase 12 deletes `trends/`, and the import failing is the reminder.
CORPUS_WINDOW_DAYS = WINDOW_DAYS


class CostNotConfiguredError(OCCSError):
    """No price is configured for a generation of this shape.

    The port's own exception rather than `ai`'s, so a caller catching it does
    not import the app that is leaving, and the Phase 12 HTTP adapter raises
    the same type without ComposeVision noticing the swap.

    `OCCSError`, not bare `Exception`: `common.exceptions.exception_handler`
    returns None for anything that is not a DRF `APIException`, so a bare one
    escaping a view would be a 500 with no code instead of the enveloped
    response Part 3 guarantees. The code is kept identical to `ai`'s so the
    wire contract does not move when the adapter does.
    """

    default_code = "generation_cost_not_configured"


@dataclass(frozen=True)
class ClusterSummary:
    """What a prompt needs to know about the strongest cluster in a category.

    `platform_display` is carried rather than derived so the caller does not
    need the `TrendCluster` model to render it.
    """

    label: str
    platform_display: str
    item_count: int


@runtime_checkable
class TrendFeedPort(Protocol):
    """Read access to the category trend corpus."""

    def top_cluster(self, *, category_id: int, platform: str) -> ClusterSummary | None:
        """The strongest fresh cluster, or None. Never triggers an extraction:
        a generation must not be made slow, or made to fail, by a trend vendor
        being down."""
        ...

    def corpus_bodies(
        self, *, category_id: int, since: dt.datetime, limit: int | None = None
    ) -> list[str]:
        """Post bodies in the category corpus since `since`, excluding items
        the pipeline marked excluded."""
        ...

    def corpus_size(self, *, category_id: int, since: dt.datetime) -> int:
        """How many items the corpus holds — the readiness signal."""
        ...


@runtime_checkable
class GenerationPort(Protocol):
    """Creative generation. Returns a local `Generation` row; see the module docstring."""

    def resolve_cost(self, *, kind: str, mode: str) -> int:
        """Credits a generation of this shape costs. Raises if unconfigured —
        never returns zero, which would read as free."""
        ...

    def generate(
        self,
        *,
        workspace: Workspace,
        user: User,
        kind: str,
        mode: str,
        prompt: str,
        product: Product | None = None,
        category: Category | None = None,
        is_batch: bool = False,
        taste_profile_version: int | None = None,
        prompt_template_version: str = "",
        n: int = 1,
    ) -> Generation:
        """Run one generation to completion and return its local row."""
        ...
