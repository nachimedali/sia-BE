"""Seeds the §4.2 credit table (implementation.md Phase 7.7).

Idempotent: re-running updates in place. Operators may retune any row in
admin afterwards (I8) — this command only establishes the starting point.

Mapping notes, since §4.2 names categories ("autopilot" / "Studio") rather
than the `(kind, mode)` pairs the model actually keys on:

* "Studio" is the blank-`mode` fallback row for a kind — every directly
  creatable mode (IDEA/PRODUCT/REWRITE) resolves to it (design.md A10's
  fallback chain).
* "Autopilot" is `mode=AUTOPILOT` — unreachable until Phase 12, but the row
  is seeded now so Phase 12 has nothing left to configure.
* "Premium model" is an exact `(kind, mode, provider, model)` row — nothing
  requests it yet; it exists for the day a premium model is offered without
  a deploy being required to price it.
* Tool-suite generation is **not** here — `ToolConfig.credits_cost` prices it
  directly (design.md §8.10), seeded by Phase 15's `seed_tools`.
* Video is **not** here — it is priced in §4.3 via `VideoLedger`, outside the
  credit pool entirely.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from ai.models import GenerationCost, GenerationKind, GenerationMode

IMAGE_PROVIDER = "nanobanana"
PREMIUM_IMAGE_MODEL = "gemini-3.1-pro-image"


def _row(
    kind: str, mode: str, credits: int, *, provider: str = "", model: str = ""
) -> dict[str, Any]:
    return {"kind": kind, "mode": mode, "provider": provider, "model": model, "credits": credits}


GENERATION_COSTS: list[dict[str, Any]] = [
    _row(GenerationKind.TEXT, "", 1),
    _row(GenerationKind.TEXT, GenerationMode.REVISION, 1),
    _row(GenerationKind.IMAGE, GenerationMode.AUTOPILOT, 2),
    _row(GenerationKind.IMAGE, "", 3),
    _row(GenerationKind.IMAGE, GenerationMode.REVISION, 1),
    _row(GenerationKind.IMAGE, "", 9, provider=IMAGE_PROVIDER, model=PREMIUM_IMAGE_MODEL),
]


class Command(BaseCommand):
    help = "Seed or update the §4.2 GenerationCost table."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for spec in GENERATION_COSTS:
            row, created = GenerationCost.objects.update_or_create(
                kind=spec["kind"],
                mode=spec["mode"],
                provider=spec["provider"],
                model=spec["model"],
                defaults={"credits": spec["credits"], "is_active": True},
            )
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {row}")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(GENERATION_COSTS)} generation costs."))
