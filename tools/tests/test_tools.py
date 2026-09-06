"""The six quick tools (design.md §8.10, C-11 / P0-04).

C-11's rule was "build it or remove the route", and the reason it is a
correction at all is that `/app/tools` had a frontend and no backend — a
shipped 404, which is worse than an absent feature because it implies coverage
that does not exist. These tests are what say it is now built.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from billing.services import ledger
from tools.models import Tool, ToolConfig, ToolUsage
from tools.services import ToolUnavailableError, run

pytestmark = pytest.mark.django_db


@pytest.fixture
def tools(db: None) -> None:
    from django.core.management import call_command

    call_command("seed_tools", verbosity=0)


@pytest.fixture
def funded(workspace: Any, tools: None) -> Any:
    ledger.grant_credits(workspace, 50, note="test")
    return workspace


# -----------------------------------------------------------------------------
# The route exists — C-11's whole point
# -----------------------------------------------------------------------------
def test_the_tools_route_no_longer_404s(auth_client: Any, tools: None) -> None:
    response = auth_client.get(reverse("tools"))

    assert response.status_code == 200
    assert {row["slug"] for row in response.json()} == set(Tool.values)


def test_the_seed_is_idempotent(tools: None) -> None:
    from django.core.management import call_command

    call_command("seed_tools", verbosity=0)

    assert ToolConfig.objects.count() == 6


def test_the_list_carries_what_the_card_renders(auth_client: Any, tools: None) -> None:
    row = auth_client.get(reverse("tools")).json()[0]

    assert set(row) == {"slug", "display_name", "is_enabled", "credits_cost"}


# -----------------------------------------------------------------------------
# The three that write
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("slug", [Tool.HEADLINE, Tool.BIO, Tool.HOOK])
def test_a_writing_tool_returns_variants_and_charges_one_credit(
    funded: Any, user: Any, slug: str
) -> None:
    before = ledger.credit_balance(funded)

    usage = run(workspace=funded, user=user, slug=slug, payload={"topic": "a new mug glaze"})

    assert len(usage.output["variants"]) == 3
    assert usage.credits_charged == 1
    assert ledger.credit_balance(funded) == before - 1


def test_a_writing_tool_without_a_topic_is_refused(funded: Any, user: Any) -> None:
    from common.exceptions import OCCSError

    with pytest.raises(OCCSError):
        run(workspace=funded, user=user, slug=Tool.HEADLINE, payload={})

    assert not ToolUsage.objects.exists()


# -----------------------------------------------------------------------------
# The three that read what is already there
# -----------------------------------------------------------------------------
def test_the_thread_splitter_reuses_the_publishing_splitter(funded: Any, user: Any) -> None:
    """Preview equals publish (Part 7 rule 1), one surface wider: a thread this
    tool shows and a thread the publisher sends must be the same thread."""
    from content.services.adaptation import _split_into_thread
    from content.services.rules import PLATFORM_RULES

    body = "word " * 400

    usage = run(
        workspace=funded, user=user, slug=Tool.THREAD_SPLITTER, payload={"body": body.strip()}
    )

    expected = _split_into_thread(body.strip(), PLATFORM_RULES["threads"].char_limit)
    assert usage.output["thread"] == expected


def test_the_hashtag_tool_ranks_the_workspaces_own_corpus(
    funded: Any, user: Any, category: Any
) -> None:
    """Not from a model. A language model asked for hashtags returns *plausible*
    ones, and plausible is exactly the wrong property — the value is that these
    appear in what is working in this category right now."""
    from trends.models import TrendItem, TrendSource, TrendSourceKind

    funded.category = category
    funded.save(update_fields=["category"])
    source = TrendSource.objects.create(
        category=category, platform="instagram", kind=TrendSourceKind.ACCOUNT
    )
    for index in range(3):
        TrendItem.objects.create(
            source=source,
            external_id=f"t-{index}",
            body="#ceramics #handmade glaze notes",
            posted_at=timezone.now() - dt.timedelta(days=1),
        )
    TrendItem.objects.create(
        source=source,
        external_id="t-solo",
        body="#ceramics only",
        posted_at=timezone.now() - dt.timedelta(days=1),
    )

    usage = run(workspace=funded, user=user, slug=Tool.HASHTAG, payload={})

    tags = {row["tag"]: row["count"] for row in usage.output["hashtags"]}
    assert tags["#ceramics"] == 4
    assert tags["#handmade"] == 3


def test_an_empty_corpus_answers_honestly_and_costs_nothing(funded: Any, user: Any) -> None:
    """ "Not yet" is a real answer, but it is not worth a credit — the same
    reasoning as debiting a generation only on a passed quality gate."""
    before = ledger.credit_balance(funded)

    usage = run(workspace=funded, user=user, slug=Tool.HASHTAG, payload={})

    assert usage.output["hashtags"] == []
    assert usage.credits_charged == 0
    assert ledger.credit_balance(funded) == before


def test_best_time_costs_nothing_before_there_is_history(funded: Any, user: Any) -> None:
    usage = run(workspace=funded, user=user, slug=Tool.BEST_TIME, payload={})

    assert usage.output["best_times"] == []
    assert usage.credits_charged == 0


def test_best_time_reads_captures_rather_than_taking_any(
    funded: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """Free of a provider call, so running it cannot contend with publishing
    for provider budget."""
    from analytics.tests.conftest import capture, make_target

    for _ in range(2):
        target = make_target(funded, user, social_account, age_days=5)
        capture(target, rate=0.2)

    run(workspace=funded, user=user, slug=Tool.BEST_TIME, payload={})

    assert metrics_provider.calls == []


# -----------------------------------------------------------------------------
# Gates
# -----------------------------------------------------------------------------
def test_a_disabled_tool_is_409_not_404(funded: Any, user: Any) -> None:
    """It exists and is listed; it is simply not accepting work. A 404 would
    say the opposite."""
    ToolConfig.objects.filter(slug=Tool.HEADLINE).update(is_enabled=False)

    with pytest.raises(ToolUnavailableError) as caught:
        run(workspace=funded, user=user, slug=Tool.HEADLINE, payload={"topic": "x"})

    assert caught.value.status_code == 409


def test_an_unknown_slug_is_refused(funded: Any, user: Any) -> None:
    from common.exceptions import OCCSError

    with pytest.raises(OCCSError):
        run(workspace=funded, user=user, slug="not-a-tool", payload={})


def test_no_credits_is_402(workspace: Any, user: Any, tools: None) -> None:
    from common.exceptions import InsufficientCredits

    with pytest.raises(InsufficientCredits) as caught:
        run(workspace=workspace, user=user, slug=Tool.HEADLINE, payload={"topic": "x"})

    assert caught.value.status_code == 402


def test_one_workspace_never_sees_anothers_usage(
    auth_client: Any, funded: Any, user: Any, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    theirs = provision_workspace(other_user, name="Not Yours")
    ledger.grant_credits(theirs, 10, note="theirs")
    run(workspace=theirs, user=other_user, slug=Tool.HEADLINE, payload={"topic": "theirs"})

    assert ToolUsage.objects.filter(workspace=funded).count() == 0


def test_running_through_the_api_charges_and_returns_the_output(
    auth_client: Any, funded: Any
) -> None:
    response = auth_client.post(
        reverse("tool-run", args=[Tool.HEADLINE]), {"topic": "a new glaze"}, format="json"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["credits_charged"] == 1
    assert body["output"]["variants"]
