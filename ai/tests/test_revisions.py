"""Revisions (design.md §8.3, §7)."""

from __future__ import annotations

from typing import Any

import pytest

from ai.models import GenerationKind, GenerationMode, GenerationStatus
from ai.services.pipeline import create_generation, run_generation
from ai.services.revisions import RevisionNotAllowedError, create_revision
from billing.services.ledger import credit_balance

pytestmark = pytest.mark.django_db


def _succeeded_image_generation(
    workspace: Any, user: Any, product: Any, generation_costs: Any
) -> Any:
    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug on a sunlit table",
        product=product,
    )
    run_generation(generation, n=1)
    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    return generation


def test_revision_costs_one_credit_not_three(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    """The named Phase 7 test."""
    parent = _succeeded_image_generation(
        workspace, user, product_with_reference_image, generation_costs
    )
    before = credit_balance(workspace)

    child = create_revision(parent=parent, user=user, instructions="make the mug more prominent")
    run_generation(child, n=1)

    child.refresh_from_db()
    assert child.status == GenerationStatus.SUCCEEDED
    assert child.credits_charged == 1
    assert before - credit_balance(workspace) == 1


def test_revision_carries_the_parent_link_and_context(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    parent = _succeeded_image_generation(
        workspace, user, product_with_reference_image, generation_costs
    )

    child = create_revision(parent=parent, user=user, instructions="warmer lighting")

    assert child.parent_generation_id == parent.id
    assert child.product_id == parent.product_id
    assert child.mode == GenerationMode.REVISION


def test_revision_rejected_against_a_pending_or_failed_parent(
    workspace: Any, user: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    pending = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="not run yet",
        product=product_with_reference_image,
    )

    with pytest.raises(RevisionNotAllowedError):
        create_revision(parent=pending, user=user, instructions="anything")
