"""Generation adapters.

`LocalGeneration` is today's behaviour: create the row, run it through
`ai.services.pipeline`. Phase 12 adds the adapter that posts to trendgen's
`POST /v1/generations` and writes the same local row as a receipt — see
`integrations.ports` for why that row stays local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from billing.services.flags import TRENDGEN_GENERATION
from integrations.ports import CostNotConfiguredError, GenerationPort
from integrations.seam import Seam

if TYPE_CHECKING:
    from ai.models import Generation
    from workspaces.models import Organization


def _record(
    *,
    workspace: Any,
    user: Any,
    kind: str,
    mode: str,
    prompt: str,
    product: Any,
    category: Any,
    is_batch: bool,
    taste_profile_version: int | None,
    prompt_template_version: str,
    status: str | None = None,
) -> Generation:
    """Write the local `Generation` row.

    The single place that decides what a receipt contains. Every adapter calls
    it — including the Phase 12 HTTP one — because a forgotten
    `taste_profile_version` in a third copy would silently break C-08's
    attribution, and nothing would fail until someone asked why acceptance rate
    moved.
    """
    from ai.models import Generation as GenerationModel

    fields: dict[str, Any] = {
        "workspace": workspace,
        "user": user,
        "kind": kind,
        "mode": mode,
        "prompt": prompt,
        "product": product,
        "category": category,
        "is_batch": is_batch,
        "taste_profile_version": taste_profile_version,
        "prompt_template_version": prompt_template_version,
    }
    if status is not None:
        fields["status"] = status
    return GenerationModel.objects.create(**fields)


class LocalGeneration:
    """Runs generation in process. Pre-phase behaviour, exactly."""

    def resolve_cost(self, *, kind: str, mode: str) -> int:
        from ai.services.costing import GenerationCostNotConfiguredError, resolve_cost

        try:
            return resolve_cost(kind=kind, mode=mode)
        except GenerationCostNotConfiguredError as exc:
            raise CostNotConfiguredError(str(exc)) from exc

    def generate(
        self,
        *,
        workspace: Any,
        user: Any,
        kind: str,
        mode: str,
        prompt: str,
        product: Any = None,
        category: Any = None,
        is_batch: bool = False,
        taste_profile_version: int | None = None,
        prompt_template_version: str = "",
        n: int = 1,
    ) -> Generation:
        from ai.services import pipeline

        generation = _record(
            workspace=workspace,
            user=user,
            kind=kind,
            mode=mode,
            prompt=prompt,
            product=product,
            category=category,
            is_batch=is_batch,
            taste_profile_version=taste_profile_version,
            prompt_template_version=prompt_template_version,
        )
        return pipeline.run_generation(generation, n=n)


@dataclass
class FakeGeneration:
    """Recording fake. Creates the same local row and marks it with a scripted
    status, without calling a provider."""

    status: str = "succeeded"
    body: str = "fake caption"
    costs: dict[tuple[str, str], int] = field(default_factory=dict)
    default_cost: int = 1
    calls: list[dict[str, Any]] = field(default_factory=list)

    #: Set to raise `CostNotConfiguredError`, which is what an unseeded price table does.
    unconfigured: bool = False

    def resolve_cost(self, *, kind: str, mode: str) -> int:
        if self.unconfigured:
            raise CostNotConfiguredError(f"no price for {kind}/{mode}")
        return self.costs.get((kind, mode), self.default_cost)

    def generate(
        self,
        *,
        workspace: Any,
        user: Any,
        kind: str,
        mode: str,
        prompt: str,
        product: Any = None,
        category: Any = None,
        is_batch: bool = False,
        taste_profile_version: int | None = None,
        prompt_template_version: str = "",
        n: int = 1,
    ) -> Generation:
        from ai.models import GenerationVariant

        self.calls.append(
            {
                "kind": kind,
                "mode": mode,
                "prompt": prompt,
                "taste_profile_version": taste_profile_version,
                "prompt_template_version": prompt_template_version,
                "n": n,
            }
        )
        generation = _record(
            workspace=workspace,
            user=user,
            kind=kind,
            mode=mode,
            prompt=prompt,
            product=product,
            category=category,
            is_batch=is_batch,
            taste_profile_version=taste_profile_version,
            prompt_template_version=prompt_template_version,
            status=self.status,
        )
        if self.status == "succeeded":
            GenerationVariant.objects.create(generation=generation, kind=kind, body=self.body)
        return generation


_seam: Seam[GenerationPort] = Seam(
    flag=TRENDGEN_GENERATION, local=LocalGeneration, remote_phase="Phase 12 (P12-01)"
)


def set_override(port: GenerationPort | None) -> None:
    _seam.override(port)


def get_generation(organization: Organization) -> GenerationPort:
    return _seam.resolve(organization)
