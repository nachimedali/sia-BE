"""The generation pipeline (design.md §8.3, I2, I5, I7).

```
entitlement preflight (feature? credits? product ready?)
  -> build grounded prompt
  -> provider call
  -> QUALITY GATE
  -> pass  -> debit ledger -> persist Generation + Variants
  -> fail  -> auto-regenerate (max attempts) -> exhausted -> no debit, surface to user
```

Split in two, matching implementation.md §4.1 ("no provider call inside a
request/response cycle"): `create_generation` is the fast, DB-only half a view
calls directly — entitlement checks and the `PENDING` row. `run_generation` is
the provider-calling half a Celery task (`ai/tasks.py`) calls; it is also what
every test in this module calls directly, in eager mode or not, per A4.

**On regeneration and I2's literal wording.** design.md's flow diagram says
"exhausted -> refund"; I2 itself says "credits debit only after the quality
gate passes." Taken together, and given the two are separately named tests
(`test_failed_quality_gate_nets_zero_debit`, `test_regeneration_capped_at_
three_attempts_then_refunds`), the "refund" language describes an outcome —
the caller ends up paying nothing, as if refunded — not a required ledger
`REFUND` row. This implementation debits nothing until an attempt passes, so
on exhaustion there is nothing to literally reverse; the "then_refunds" test
asserts the regeneration loop actually runs `max_regeneration_attempts` times
and nets to a zero balance change (design.md §15.8 A72).

**On cost and `n`.** design.md's §4.2 table prices a kind/mode pair (e.g.
"Studio image: 3 credits") without saying whether requesting `n` variants in
one call multiplies that cost by `n`. This implementation charges the
resolved cost once per generation *action*, covering every variant it
produces — "one Studio generation costs 3 credits" reads more naturally than
a per-variant multiplier the spec never states, and it is what
`test_passed_quality_gate_debits_exactly_generation_cost` asserts literally
(design.md §15.8 A73).
"""

from __future__ import annotations

import time
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from ai.models import (
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationStatus,
    GenerationVariant,
    QualityCheck,
    QualityGateConfig,
    VoiceProfile,
)
from ai.providers.base import ImageVariant, TextVariant
from ai.providers.llm_text import get_text_provider
from ai.providers.nanobanana_image import get_image_provider
from ai.services import prompting, quality
from ai.services.costing import resolve_cost
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import InsufficientCredits, OCCSError
from content.models import MediaAsset, MediaSource
from content.services.media import ingest_media
from products.models import Product
from products.services.guards import ensure_generation_ready
from workspaces.models import Workspace

# TREND/RECIPE need Phase 10/14's grounding sources; AUTOPILOT needs Phase 12's
# engine; REPURPOSE needs Phase 11's `origin_post`. REVISION is reachable only
# through `ai.services.revisions.create_revision`, never directly.
ALLOWED_MODES = frozenset(
    {GenerationMode.IDEA, GenerationMode.PRODUCT, GenerationMode.REWRITE, GenerationMode.REVISION}
)
DIRECTLY_CREATABLE_MODES = ALLOWED_MODES - {GenerationMode.REVISION}


class GenerationKindNotAvailableError(OCCSError):
    default_code = "generation_kind_not_available"
    default_detail = "This generation kind is not available yet."


class GenerationModeNotAvailableError(OCCSError):
    default_code = "generation_mode_not_available"
    default_detail = "This generation mode is not available yet."


def create_generation(
    *,
    workspace: Workspace,
    user: Any,
    kind: str,
    mode: str,
    prompt: str,
    product: Product | None = None,
    voice_profile: VoiceProfile | None = None,
    aspect: str = "1:1",
    render_style: str = "",
    scene: str = "",
    is_batch: bool = False,
) -> Generation:
    """Validates and persists the `PENDING` row. No provider call — that is
    `run_generation`'s job (design.md §11)."""
    entitlements = entitlements_for(workspace)

    if mode not in DIRECTLY_CREATABLE_MODES:
        raise GenerationModeNotAvailableError(detail={"mode": mode})

    if kind == GenerationKind.VIDEO:
        # The entitlement gate is real even though nothing behind it is
        # (Phase 14 builds the provider) — Free must be blocked here, not
        # only once a VideoProvider exists to call.
        entitlements.require_feature("video_generation")
        raise GenerationKindNotAvailableError(
            "Video generation lands in a later phase.", detail={"kind": kind}
        )
    if kind not in {GenerationKind.TEXT, GenerationKind.IMAGE}:
        raise GenerationKindNotAvailableError(detail={"kind": kind})

    if product is not None:
        ensure_generation_ready(product)

    # Preflight only (I5) — the authoritative check is inside the debit's own
    # transaction in `run_generation`, once the gate has passed.
    cost = resolve_cost(kind=kind, mode=mode)
    entitlements.require_credits(cost)

    return Generation.objects.create(
        workspace=workspace,
        user=user,
        kind=kind,
        mode=mode,
        prompt=prompt,
        product=product,
        category=workspace.category,
        voice_profile=voice_profile,
        aspect=aspect,
        render_style=render_style,
        scene=scene,
        is_batch=is_batch,
    )


def _reference_image_bytes(product: Product | None) -> list[bytes]:
    if product is None:
        return []
    contents = []
    for asset in product.reference_images.all():
        with asset.file.open("rb") as handle:
            contents.append(handle.read())
    return contents


def _attempt_text(
    generation: Generation, *, n: int
) -> tuple[list[tuple[TextVariant, quality.QualityResult]], dict[str, Any]]:
    provider = get_text_provider()
    grounded = prompting.assemble_text_prompt(
        idea=generation.prompt,
        workspace=generation.workspace,
        product=generation.product,
        voice_profile=generation.voice_profile,
    )
    result = provider.generate(system=grounded.system, prompt=grounded.user, n=n)

    banned = generation.voice_profile.banned_phrases if generation.voice_profile else []
    checked = [
        (variant, quality.run_text_quality_gate(body=variant.body, banned_phrases=banned))
        for variant in result.variants
    ]
    meta = {
        "provider": result.provider,
        "model": result.model,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
    }
    return checked, meta


def _attempt_image(
    generation: Generation, *, n: int, config: QualityGateConfig, reference_images: list[bytes]
) -> tuple[list[tuple[ImageVariant, quality.QualityResult]], dict[str, Any]]:
    provider = get_image_provider()
    text_provider = get_text_provider()
    grounded = prompting.assemble_image_prompt(
        idea=generation.prompt,
        workspace=generation.workspace,
        product=generation.product,
        render_style=generation.render_style,
        scene=generation.scene,
    )
    restrictions = generation.product.restrictions if generation.product else []

    result = provider.generate(
        prompt=grounded.user,
        reference_images=reference_images,
        aspect=generation.aspect,
        n=n,
        batch=generation.is_batch,
    )
    checked = [
        (
            variant,
            quality.run_image_quality_gate(
                content=variant.content,
                requested_aspect=generation.aspect,
                reference_images=reference_images,
                restrictions=restrictions,
                text_provider=text_provider,
                identity_similarity_threshold=config.identity_similarity_threshold,
            ),
        )
        for variant in result.variants
    ]
    meta = {"provider": result.provider, "model": result.model}
    return checked, meta


def _persist_generated_image(generation: Generation, variant: ImageVariant) -> MediaAsset:
    extension = variant.mime.split("/")[-1] or "png"
    upload = SimpleUploadedFile(
        f"generation-{generation.pk}.{extension}", variant.content, content_type=variant.mime
    )
    asset = ingest_media(
        workspace=generation.workspace, upload=upload, source=MediaSource.GENERATED
    )
    asset.generation = generation
    asset.save(update_fields=["generation"])
    return asset


def _persist_variants(
    generation: Generation, passing: list[tuple[Any, quality.QualityResult]]
) -> None:
    for rank, (candidate, _check) in enumerate(passing):
        if generation.kind == GenerationKind.TEXT:
            GenerationVariant.objects.create(
                generation=generation,
                kind=GenerationKind.TEXT,
                body=candidate.body,
                rank=rank,
                rationale=candidate.rationale,
            )
        else:
            media_asset = _persist_generated_image(generation, candidate)
            GenerationVariant.objects.create(
                generation=generation,
                kind=GenerationKind.IMAGE,
                media_asset=media_asset,
                rank=rank,
            )


def run_generation(generation: Generation, *, n: int = 3) -> Generation:
    """Idempotent on a non-`PENDING` row — a retried Celery task must not
    regenerate (and re-debit) a generation that already resolved."""
    if generation.status != GenerationStatus.PENDING:
        return generation

    workspace = generation.workspace
    entitlements = entitlements_for(workspace)
    config = QualityGateConfig.get_solo()
    cost = resolve_cost(kind=generation.kind, mode=generation.mode)
    reference_images = (
        _reference_image_bytes(generation.product)
        if generation.kind == GenerationKind.IMAGE
        else []
    )

    started = time.monotonic()
    passing: list[tuple[Any, quality.QualityResult]] = []
    meta: dict[str, Any] = {}
    attempts_made = 0

    for attempt in range(1, config.max_regeneration_attempts + 1):
        attempts_made = attempt
        attempt_results: list[tuple[Any, quality.QualityResult]]
        if generation.kind == GenerationKind.TEXT:
            attempt_results, meta = _attempt_text(generation, n=n)
        else:
            attempt_results, meta = _attempt_image(
                generation, n=n, config=config, reference_images=reference_images
            )

        QualityCheck.objects.bulk_create(
            QualityCheck(
                generation=generation,
                checks=check.checks,
                identity_score=check.identity_score,
                passed=check.passed,
                attempt=attempt,
                rejected_reason=check.rejected_reason,
            )
            for _candidate, check in attempt_results
        )

        passing = [(candidate, check) for candidate, check in attempt_results if check.passed]
        if passing:
            break

    generation.provider = meta.get("provider", "")
    generation.model = meta.get("model", "")
    generation.tokens_in = meta.get("tokens_in", 0)
    generation.tokens_out = meta.get("tokens_out", 0)
    generation.latency_ms = int((time.monotonic() - started) * 1000)

    if not passing:
        generation.status = GenerationStatus.FAILED
        generation.error_detail = {
            "reason": "quality_gate_exhausted",
            "attempts": attempts_made,
        }
        generation.save()
        return generation

    try:
        with transaction.atomic():
            ledger.debit_credits(
                workspace,
                cost,
                quota=entitlements.quota("monthly_ai_credits"),
                note=f"generation {generation.pk}",
                generation=generation,
            )
            generation.credits_charged = cost
            generation.status = GenerationStatus.SUCCEEDED
            generation.save()
            _persist_variants(generation, passing)
    except InsufficientCredits:
        # I5's task-preflight gate: `create_generation`'s own check ran
        # against the balance at request time, which can be stale by the
        # time this task actually runs — someone else may have spent the
        # workspace's last credits in between. A clean FAILED generation,
        # not an unhandled exception the Celery task would otherwise crash
        # on (design.md §15.8 A76). The quality gate already passed, so the
        # output is discarded rather than persisted unpaid-for (I2).
        generation.status = GenerationStatus.FAILED
        generation.error_detail = {"reason": "insufficient_credits_at_task_time"}
        generation.save()

    return generation
