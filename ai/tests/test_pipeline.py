"""The generation pipeline (design.md §8.3, I2, I5, I7)."""

from __future__ import annotations

from typing import Any

import pytest

from ai.models import (
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationStatus,
    QualityCheck,
    QualityGateConfig,
)
from ai.providers.fake import FORCE_LOW_SIMILARITY_SENTINEL, FORCE_VIOLATION_SENTINEL
from ai.services.costing import GenerationCostNotConfiguredError, resolve_cost
from ai.services.pipeline import (
    GenerationKindNotAvailableError,
    GenerationModeNotAvailableError,
    create_generation,
    run_generation,
)
from billing.services.ledger import credit_balance
from common.exceptions import InsufficientCredits, PaymentRequired
from products.services.guards import ProductNotGenerationReadyError

pytestmark = pytest.mark.django_db


def _on_plan(workspace: Any, plans: Any, code: str) -> Any:
    workspace.plan = plans[code]
    workspace.save(update_fields=["plan", "updated_at"])
    return workspace


def _text_generation(workspace: Any, user: Any, **kwargs: Any) -> Generation:
    return create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt=kwargs.pop("prompt", "a cosy morning"),
        **kwargs,
    )


def _image_generation(workspace: Any, user: Any, product: Any, **kwargs: Any) -> Generation:
    return create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt=kwargs.pop("prompt", "the mug on a sunlit table"),
        product=product,
        **kwargs,
    )


# -----------------------------------------------------------------------------
# I2 — credits debit only after the quality gate passes
# -----------------------------------------------------------------------------
def test_passed_quality_gate_debits_exactly_generation_cost(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """The named Phase 7 test."""
    before = credit_balance(workspace)
    expected_cost = resolve_cost(kind=GenerationKind.IMAGE, mode=GenerationMode.PRODUCT)

    generation = _image_generation(workspace, user, product_with_reference_image)
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    assert generation.credits_charged == expected_cost
    assert before - credit_balance(workspace) == expected_cost
    assert generation.variants.count() == 1


def test_failed_quality_gate_nets_zero_debit(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """The named Phase 7 test. A single-attempt failure (the gate never
    passes even once) must leave the balance untouched."""
    before = credit_balance(workspace)

    generation = _image_generation(
        workspace,
        user,
        product_with_reference_image,
        prompt=f"an unrelated scene {FORCE_LOW_SIMILARITY_SENTINEL}",
    )
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.FAILED
    assert credit_balance(workspace) == before
    assert generation.credits_charged == 0


def test_regeneration_capped_at_three_attempts_then_refunds(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """The named Phase 7 test. Every attempt is persisted as a QualityCheck
    even though none of them are ever shown to the user, and the caller ends
    up paying nothing — "refunds" in the sense design.md's flow diagram uses
    it (design.md §15.8 A72)."""
    before = credit_balance(workspace)

    generation = _image_generation(
        workspace,
        user,
        product_with_reference_image,
        prompt=f"an unrelated scene {FORCE_LOW_SIMILARITY_SENTINEL}",
    )
    run_generation(generation, n=1)

    generation.refresh_from_db()
    config = QualityGateConfig.get_solo()
    assert generation.status == GenerationStatus.FAILED
    assert generation.error_detail["attempts"] == config.max_regeneration_attempts
    attempt_count = QualityCheck.objects.filter(generation=generation).count()
    assert attempt_count == config.max_regeneration_attempts
    assert QualityCheck.objects.filter(generation=generation, passed=True).count() == 0
    assert credit_balance(workspace) == before


def test_regeneration_stops_early_on_a_passing_attempt(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    generation = _image_generation(workspace, user, product_with_reference_image)
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    assert QualityCheck.objects.filter(generation=generation).count() == 1


def test_run_generation_is_idempotent_on_a_resolved_row(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """A retried Celery task calling this twice must not double-debit."""
    generation = _image_generation(workspace, user, product_with_reference_image)
    run_generation(generation, n=1)
    balance_after_first_run = credit_balance(workspace)

    run_generation(generation, n=1)

    assert credit_balance(workspace) == balance_after_first_run


# -----------------------------------------------------------------------------
# I7 — generation rejected without a reference image
# -----------------------------------------------------------------------------
def test_generation_rejected_without_reference_image(
    workspace: Any, user: Any, product: Any, generation_costs: Any
) -> None:
    with pytest.raises(ProductNotGenerationReadyError):
        _image_generation(workspace, user, product)


# -----------------------------------------------------------------------------
# I5 — entitlement gates
# -----------------------------------------------------------------------------
def test_insufficient_credits_blocks_creation(
    workspace: Any, user: Any, generation_costs: Any
) -> None:
    from billing.services.entitlements import entitlements_for
    from billing.services.ledger import debit_credits

    # Drain the Free plan's balance to zero.
    balance = credit_balance(workspace)
    debit_credits(workspace, balance, quota=entitlements_for(workspace).quota("monthly_ai_credits"))

    with pytest.raises(InsufficientCredits):
        _text_generation(workspace, user)


def test_free_plan_blocked_from_video_generation(workspace: Any, user: Any) -> None:
    """The named Phase 7 test."""
    with pytest.raises(PaymentRequired):
        create_generation(
            workspace=workspace,
            user=user,
            kind=GenerationKind.VIDEO,
            mode=GenerationMode.PRODUCT,
            prompt="a short clip",
        )


def test_paid_plan_gets_a_clear_not_yet_available_error_for_video(
    workspace: Any, plans: Any, user: Any
) -> None:
    """Video is entitlement-gated now, but the provider itself is Phase 14 —
    Pro/Advanced must not silently succeed at something unbuilt."""
    _on_plan(workspace, plans, "pro")

    with pytest.raises(GenerationKindNotAvailableError):
        create_generation(
            workspace=workspace,
            user=user,
            kind=GenerationKind.VIDEO,
            mode=GenerationMode.PRODUCT,
            prompt="a short clip",
        )


def test_unavailable_mode_is_rejected(workspace: Any, user: Any, generation_costs: Any) -> None:
    with pytest.raises(GenerationModeNotAvailableError):
        create_generation(
            workspace=workspace,
            user=user,
            kind=GenerationKind.TEXT,
            mode=GenerationMode.TREND,
            prompt="whatever is trending",
        )


def test_revision_mode_is_not_directly_creatable(
    workspace: Any, user: Any, generation_costs: Any
) -> None:
    """Only `ai.services.revisions.create_revision` may set REVISION."""
    with pytest.raises(GenerationModeNotAvailableError):
        create_generation(
            workspace=workspace,
            user=user,
            kind=GenerationKind.TEXT,
            mode=GenerationMode.REVISION,
            prompt="make it punchier",
        )


# -----------------------------------------------------------------------------
# Costing (A10)
# -----------------------------------------------------------------------------
def test_generation_cost_lookup_falls_back_then_hard_errors(generation_costs: Any) -> None:
    """The named Phase 7 test. Exact -> (kind, mode) -> (kind); no match is a
    hard error, never a silent zero."""
    from ai.models import GenerationCost

    GenerationCost.objects.create(
        kind=GenerationKind.IMAGE, mode="", provider="nanobanana", model="special", credits=42
    )

    exact = resolve_cost(kind=GenerationKind.IMAGE, mode="", provider="nanobanana", model="special")
    kind_mode_fallback = resolve_cost(kind=GenerationKind.IMAGE, mode=GenerationMode.PRODUCT)
    kind_fallback = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.PRODUCT)

    assert exact == 42
    assert kind_mode_fallback == 3
    assert kind_fallback == 1

    with pytest.raises(GenerationCostNotConfiguredError):
        resolve_cost(kind=GenerationKind.VIDEO)


# -----------------------------------------------------------------------------
# Prompt grounding reaches the provider (design.md §8.3)
# -----------------------------------------------------------------------------
def test_product_restrictions_reach_the_image_provider_prompt(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    from ai.providers.fake import _fake_image_provider

    product_with_reference_image.restrictions = ["always show the handle"]
    product_with_reference_image.save(update_fields=["restrictions"])

    generation = _image_generation(workspace, user, product_with_reference_image)
    run_generation(generation, n=1)

    assert "always show the handle" in _fake_image_provider.calls[-1]["prompt"]


def test_brand_constraint_violation_triggers_regeneration_then_exhausts(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    product_with_reference_image.restrictions = [f"{FORCE_VIOLATION_SENTINEL}: no hands in frame"]
    product_with_reference_image.save(update_fields=["restrictions"])

    generation = _image_generation(workspace, user, product_with_reference_image)
    run_generation(generation, n=1)

    generation.refresh_from_db()
    config = QualityGateConfig.get_solo()
    assert generation.status == GenerationStatus.FAILED
    attempt_count = QualityCheck.objects.filter(generation=generation).count()
    assert attempt_count == config.max_regeneration_attempts


# -----------------------------------------------------------------------------
# Batch vs sync routing (D8)
# -----------------------------------------------------------------------------
def test_batch_routing_for_autopilot_sync_for_studio(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """The named Phase 7 test."""
    from ai.providers.fake import _fake_image_provider

    studio = _image_generation(workspace, user, product_with_reference_image, is_batch=False)
    run_generation(studio, n=1)
    assert _fake_image_provider.calls[-1]["batch"] is False

    autopilot_workspace_generation = Generation.objects.create(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="batch run",
        product=product_with_reference_image,
        is_batch=True,
    )
    run_generation(autopilot_workspace_generation, n=1)
    assert _fake_image_provider.calls[-1]["batch"] is True
