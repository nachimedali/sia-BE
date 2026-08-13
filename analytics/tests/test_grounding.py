"""Performance grounding reaching a prompt (design.md §8.3, closing A70).

Phase 7 shipped `_performance_signal` as a stub with a test asserting it stayed
one. This is the other side of that bargain — and with it, A70 is fully closed:
all three of design.md §8.3's retrieved contexts are live.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from ai.services.prompting import assemble_text_prompt
from analytics.models import PostMetric
from analytics.tests.conftest import make_population, make_target

pytestmark = pytest.mark.django_db


def _population(workspace: Any, user: Any, account: Any) -> Any:
    """Four ordinary posts and one clear winner, so a percentile exists."""
    return make_population(workspace, user, account, age_days=10)


def test_a_prompt_is_grounded_in_the_workspaces_own_best_post(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    _population(paid_workspace, user, social_account)

    prompt = assemble_text_prompt(idea="launch day", workspace=paid_workspace)

    assert prompt.grounding["performance"] is True
    assert "Three things nobody tells you" in prompt.user
    # Framed as a pattern to learn from, not copy — the same line the category
    # signal draws.
    assert "Match what makes it work, not its wording." in prompt.user


def test_only_the_opening_is_quoted_not_the_whole_post(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """What transfers between two posts by the same brand is how they start;
    handing over a full caption invites rewriting it."""
    long_body = " ".join(f"word{n}" for n in range(80))
    for rate in (0.01, 0.02, 0.03, 0.04):
        PostMetric.objects.create(
            post_target=make_target(paid_workspace, user, social_account, age_days=10),
            captured_at=timezone.now(),
            engagement_rate=rate,
        )
    PostMetric.objects.create(
        post_target=make_target(
            paid_workspace, user, social_account, age_days=10, body=long_body
        ),
        captured_at=timezone.now(),
        engagement_rate=0.40,
    )

    prompt = assemble_text_prompt(idea="launch day", workspace=paid_workspace)

    assert "word0" in prompt.user
    assert "word79" not in prompt.user


def test_grounding_is_absent_without_enough_history(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """One post is not a distribution, so there is no percentile and nothing
    to call a top performer."""
    PostMetric.objects.create(
        post_target=make_target(paid_workspace, user, social_account, age_days=10),
        captured_at=timezone.now(),
        engagement_rate=0.9,
    )

    prompt = assemble_text_prompt(idea="launch day", workspace=paid_workspace)

    assert prompt.grounding["performance"] is False


def test_grounding_respects_the_plans_history_horizon(
    workspace: Any, user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    """A Free workspace's 7-day window cannot see a post from last month, so
    the prompt is ungrounded rather than grounded in data the plan does not
    include."""
    workspace.plan = plans["free"]
    workspace.save(update_fields=["plan"])
    for rate in (0.01, 0.02, 0.03, 0.04, 0.40):
        PostMetric.objects.create(
            post_target=make_target(workspace, user, social_account, age_days=30),
            captured_at=timezone.now(),
            engagement_rate=rate,
        )

    prompt = assemble_text_prompt(idea="launch day", workspace=workspace)

    assert prompt.grounding["performance"] is False


def test_grounding_never_reaches_a_provider(
    paid_workspace: Any, user: Any, social_account: Any, monkeypatch: Any
) -> None:
    """The prompt path reads stored captures and only stored captures — a slow
    or dead platform must never become a slow or failed generation."""
    _population(paid_workspace, user, social_account)

    def explode(**_kwargs: Any) -> Any:
        raise AssertionError("prompt assembly must not reach a platform adapter")

    monkeypatch.setattr(
        "channels.adapters.fake.FakePlatformAdapter.fetch_metrics", explode
    )

    prompt = assemble_text_prompt(idea="launch day", workspace=paid_workspace)

    assert prompt.grounding["performance"] is True


def test_all_three_grounding_signals_can_fire_together(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """A70 closed: category signal (Phase 10), voice (Phase 7) and performance
    (Phase 11) are the three contexts design.md §8.3 names."""
    from categories.models import Category
    from content.models import Platform
    from trends.models import TrendSource, TrendSourceKind
    from trends.services import extract

    _population(paid_workspace, user, social_account)

    ceramics = Category.objects.create(name="Ceramics", slug="ceramics-grounding")
    paid_workspace.category = ceramics
    paid_workspace.platforms = [Platform.TIKTOK]
    paid_workspace.save(update_fields=["category", "platforms"])
    TrendSource.objects.create(
        category=ceramics,
        platform=Platform.TIKTOK,
        kind=TrendSourceKind.CREATIVE_CENTER,
        vendor="rapidapi",
    )
    extract(category_id=paid_workspace.category_id, platform=Platform.TIKTOK, force=True)

    prompt = assemble_text_prompt(idea="launch day", workspace=paid_workspace)

    assert prompt.grounding == {
        "voice": True,
        "restrictions": False,
        "category_signal": True,
        "performance": True,
    }
