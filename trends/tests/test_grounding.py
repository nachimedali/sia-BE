"""The category signal reaching a prompt (design.md §8.3, closing A70).

Phase 7 shipped `_category_signal` as a stub with a test asserting it stayed
one. This is the other side of that bargain: the signal is real now, and these
hold it to the two properties that make it safe to have wired in — it grounds
when a corpus exists, and it is inert when one does not.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.services.prompting import assemble_image_prompt, assemble_text_prompt
from content.models import Platform
from trends.services import extract

pytestmark = pytest.mark.django_db


def test_a_generated_prompt_is_grounded_in_the_category_corpus(
    trend_workspace: Any, source: Any
) -> None:
    clusters = extract(
        category_id=trend_workspace.category_id, platform=Platform.TIKTOK, force=True
    )
    top = clusters[0]

    prompt = assemble_text_prompt(idea="launch day", workspace=trend_workspace)

    assert prompt.grounding["category_signal"] is True
    assert top.label in prompt.user
    # Framed as evidence, not as material to copy — the same line design.md
    # §8.4's synthesis rule draws for recipes.
    assert "Take the approach, not the wording." in prompt.user


def test_the_image_prompt_is_grounded_too(trend_workspace: Any, source: Any) -> None:
    extract(category_id=trend_workspace.category_id, platform=Platform.TIKTOK, force=True)

    prompt = assemble_image_prompt(idea="the mug on a table", workspace=trend_workspace)

    assert prompt.grounding["category_signal"] is True


def test_grounding_is_skipped_when_the_corpus_has_expired(
    trend_workspace: Any, source: Any
) -> None:
    """Freshness is resolved at read time, so an expired corpus stops grounding
    without anything having to sweep it away first."""
    from trends.models import TrendCluster

    extract(category_id=trend_workspace.category_id, platform=Platform.TIKTOK, force=True)
    TrendCluster.objects.update(expires_at="2020-01-01T00:00:00Z")

    prompt = assemble_text_prompt(idea="launch day", workspace=trend_workspace)

    assert prompt.grounding["category_signal"] is False


def test_generation_never_waits_on_a_trend_vendor(
    trend_workspace: Any, source: Any, monkeypatch: Any
) -> None:
    """The prompt path reads the cache and only the cache. If it could trigger
    an extraction, a slow or dead vendor would become a slow or failed
    generation — and generation quality is the product (design.md §1)."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prompt assembly must not reach a trend vendor")

    monkeypatch.setattr("trends.services.ingest.get_trend_vendor", explode)

    prompt = assemble_text_prompt(idea="launch day", workspace=trend_workspace)

    assert prompt.grounding["category_signal"] is False


def test_a_workspace_with_no_category_grounds_nothing(workspace: Any) -> None:
    workspace.category = None
    workspace.save(update_fields=["category"])

    prompt = assemble_text_prompt(idea="launch day", workspace=workspace)

    assert prompt.grounding["category_signal"] is False
