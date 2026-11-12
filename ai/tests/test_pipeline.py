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
    workspace.organization.plan = plans[code]
    workspace.organization.save(update_fields=["plan", "updated_at"])
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


def test_a_task_past_its_time_limit_fails_the_generation(
    monkeypatch: pytest.MonkeyPatch, workspace: Any, user: Any, plans: Any, generation_costs: Any
) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from ai import tasks

    generation = _text_generation(_on_plan(workspace, plans, "pro"), user)

    def hang(*args: Any, **kwargs: Any) -> None:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(tasks, "run_generation", hang)
    tasks.run_generation_task(generation.pk)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.FAILED
    assert generation.error_detail == {"reason": "timeout"}


def test_a_provider_error_fails_the_generation_instead_of_leaving_it_pending(
    monkeypatch: pytest.MonkeyPatch, workspace: Any, user: Any, plans: Any, generation_costs: Any
) -> None:
    """A vendor refusal (e.g. Google's 402 when its billing lapses) must end the
    spinner: a row left PENDING is polled forever by the Studio."""
    from ai import tasks
    from common.exceptions import ProviderError

    generation = _text_generation(_on_plan(workspace, plans, "pro"), user)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise ProviderError("Image provider returned 402.")

    monkeypatch.setattr(tasks, "run_generation", refuse)
    with pytest.raises(ProviderError):
        tasks.run_generation_task(generation.pk)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.FAILED
    assert generation.error_detail == {"reason": "provider_error"}
    assert generation.credits_charged == 0


def test_the_image_provider_key_never_rides_in_the_url(settings: Any) -> None:
    """httpx logs request URLs at INFO, so a `?key=` lands in the worker log."""
    from ai.providers.nanobanana_image import _client

    settings.IMAGE_PROVIDER_API_KEY = "secret-key"
    client = _client()
    assert "secret-key" not in str(client.build_request("POST", "/models/m:generateContent").url)
    assert client.headers["x-goog-api-key"] == "secret-key"


def test_every_image_attempt_keeps_its_picture_vendor_and_prompt(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """A rejected picture is never shown to the user, but staff must be able to
    see what the gate refused and why (admin: Generation → quality checks)."""
    generation = _image_generation(
        workspace,
        user,
        product_with_reference_image,
        prompt=f"an unrelated scene {FORCE_LOW_SIMILARITY_SENTINEL}",
    )
    run_generation(generation, n=1)

    checks = list(generation.quality_checks.order_by("attempt"))
    assert len(checks) == 3
    assert not any(check.passed for check in checks)
    for check in checks:
        assert check.image and check.image.size > 0
        assert (check.provider, check.model) != ("", "")
        assert "an unrelated scene" in check.prompt_sent
    # Not media in the brand's library: nothing to list, nothing counted.
    assert generation.variants.count() == 0


def test_a_text_attempt_stores_no_picture(
    workspace: Any, user: Any, plans: Any, generation_costs: Any
) -> None:
    generation = _text_generation(_on_plan(workspace, plans, "pro"), user)
    run_generation(generation, n=1)
    assert all(not check.image for check in generation.quality_checks.all())


def test_staff_see_each_attempt_and_the_product_photos_in_admin(
    client: Any,
    workspace: Any,
    user: Any,
    product_with_reference_image: Any,
    generation_costs: Any,
) -> None:
    generation = _image_generation(
        workspace,
        user,
        product_with_reference_image,
        prompt=f"an unrelated scene {FORCE_LOW_SIMILARITY_SENTINEL}",
    )
    run_generation(generation, n=1)

    from accounts.models import User

    client.force_login(User.objects.create_superuser(email="staff@example.com", password="x"))
    page = client.get(f"/admin/ai/generation/{generation.pk}/change/", follow=True)
    assert page.status_code == 200
    html = page.content.decode()
    # Three refused attempts plus the product's one reference photo.
    assert html.count("max-height:160px") == 4
    assert "product_identity" in html


def test_an_image_generation_keeps_the_prompt_even_when_the_vendor_refuses(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Any,
    user: Any,
    product_with_reference_image: Any,
    generation_costs: Any,
) -> None:
    """Saved before the call: a 402 from the vendor must not lose what was sent."""
    from ai.providers.fake import _fake_image_provider
    from common.exceptions import ProviderError

    sent: list[str] = []

    def refuse(**kwargs: Any) -> None:
        sent.append(kwargs["prompt"])
        raise ProviderError("Image provider returned 402.")

    monkeypatch.setattr(_fake_image_provider, "generate", refuse)
    generation = _image_generation(workspace, user, product_with_reference_image, prompt="a mug")
    with pytest.raises(ProviderError):
        run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.prompt_sent == sent[0]
    assert "a mug" in generation.prompt_sent


def test_a_text_generation_keeps_system_and_user_prompt(
    workspace: Any, user: Any, plans: Any, generation_costs: Any
) -> None:
    generation = _text_generation(_on_plan(workspace, plans, "pro"), user)
    run_generation(generation, n=1)
    generation.refresh_from_db()
    assert generation.prompt_sent.startswith("SYSTEM:\n")
    assert "\n\nUSER:\n" in generation.prompt_sent
