"""Grounded prompt assembly (design.md §8.3)."""

from __future__ import annotations

from typing import Any

import pytest

from ai.services.prompting import assemble_image_prompt, assemble_text_prompt

pytestmark = pytest.mark.django_db


def test_product_restrictions_present_in_final_prompt(workspace: Any, product: Any) -> None:
    """The named Phase 7 test."""
    product.restrictions = ["always show the handle", "no hands in frame"]
    product.save(update_fields=["restrictions"])

    prompt = assemble_text_prompt(idea="a cosy morning", workspace=workspace, product=product)

    assert "always show the handle" in prompt.user
    assert "no hands in frame" in prompt.user
    assert prompt.grounding["restrictions"] is True


def test_text_prompt_omits_restrictions_block_when_product_has_none(
    workspace: Any, product: Any
) -> None:
    prompt = assemble_text_prompt(idea="a cosy morning", workspace=workspace, product=product)

    assert "Hard constraints" not in prompt.user
    assert prompt.grounding["restrictions"] is False


def test_voice_profile_feeds_the_system_prompt(workspace: Any, voice_profile: Any) -> None:
    prompt = assemble_text_prompt(
        idea="launch day", workspace=workspace, voice_profile=voice_profile
    )

    assert "small, proud, independent brand" in prompt.system
    assert "warm" in prompt.system
    assert "synergy" in prompt.system


def test_grounding_is_absent_until_there_is_something_to_ground_in(workspace: Any) -> None:
    """Grounding is opportunistic: a workspace with no trend corpus and no
    published history generates exactly as it did in Phase 7, rather than
    failing or waiting on anything.

    A70 is closed — both signals are wired now, so this asserts the *empty*
    case rather than the stubbed one. Their live halves are covered where the
    data lives: `trends/tests/test_grounding.py` and
    `analytics/tests/test_grounding.py`."""
    prompt = assemble_text_prompt(idea="launch day", workspace=workspace)

    assert prompt.grounding["category_signal"] is False
    assert prompt.grounding["performance"] is False


def test_image_prompt_carries_render_style_and_scene(workspace: Any, product: Any) -> None:
    prompt = assemble_image_prompt(
        idea="the mug on a sunlit table",
        workspace=workspace,
        product=product,
        render_style="editorial photography",
        scene="kitchen counter",
    )

    assert "editorial photography" in prompt.user
    assert "kitchen counter" in prompt.user


def test_image_prompt_includes_restrictions(workspace: Any, product: Any) -> None:
    product.restrictions = ["always show the sole"]
    product.save(update_fields=["restrictions"])

    prompt = assemble_image_prompt(idea="a shoe on concrete", workspace=workspace, product=product)

    assert "always show the sole" in prompt.user
