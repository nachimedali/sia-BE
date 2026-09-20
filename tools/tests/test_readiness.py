"""Tools' guided setup (X-08).

Six tools with four different prerequisites between them, and a page that
rendered all six identically — so a workspace whose corpus is empty pressed
"Hashtag ranker", was charged nothing, and got an empty list with no
explanation. `GET /tools/readiness/` is what lets the page say which tool can
run, and why the others cannot, *before* anyone presses anything.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from billing.services import ledger
from common.setup import DONE, MISSING
from tools.models import Tool, ToolConfig

pytestmark = pytest.mark.django_db


@pytest.fixture
def tools(db: None) -> None:
    from django.core.management import call_command

    call_command("seed_tools", verbosity=0)


@pytest.fixture
def funded(workspace: Any, tools: None) -> Any:
    ledger.grant_credits(workspace, 50, note="test")
    return workspace


def rows(response: Any) -> dict[str, Any]:
    return {row["key"]: row for row in response.json()["requirements"]}


def states(response: Any) -> dict[str, Any]:
    return {row["slug"]: row for row in response.json()["tools"]}


# -----------------------------------------------------------------------------
# The three that need only credits
# -----------------------------------------------------------------------------
def test_the_writers_are_ready_as_soon_as_there_are_credits(auth_client: Any, funded: Any) -> None:
    response = auth_client.get(reverse("tools-readiness"))

    assert response.status_code == 200
    assert rows(response)["credits"]["status"] == DONE
    for slug in (Tool.HEADLINE, Tool.BIO, Tool.HOOK, Tool.THREAD_SPLITTER):
        assert states(response)[slug]["status"] == "ready"


def test_no_credits_blocks_every_tool_with_one_reason(
    auth_client: Any, workspace: Any, tools: None
) -> None:
    response = auth_client.get(reverse("tools-readiness"))

    assert rows(response)["credits"]["status"] == MISSING
    assert {row["reason"] for row in response.json()["tools"]} == {"no_credits"}


# -----------------------------------------------------------------------------
# The three that read what is already there
# -----------------------------------------------------------------------------
def test_the_hashtag_ranker_needs_a_corpus_not_a_model(auth_client: Any, funded: Any) -> None:
    """It ranks tags that actually appear in the category's live corpus, so an
    empty corpus is a real blocker rather than a thin answer."""
    response = auth_client.get(reverse("tools-readiness"))

    assert rows(response)["trend_corpus"]["status"] == MISSING
    assert rows(response)["trend_corpus"]["blocking"] is False
    assert states(response)[Tool.HASHTAG]["reason"] in {"no_corpus", "no_category"}


def test_a_workspace_with_no_category_is_told_so_by_the_tool_that_needs_one(
    auth_client: Any, funded: Any
) -> None:
    funded.category = None
    funded.save(update_fields=["category"])

    assert states(auth_client.get(reverse("tools-readiness")))[Tool.HASHTAG]["reason"] == (
        "no_category"
    )


def test_a_live_corpus_unblocks_the_hashtag_ranker(
    auth_client: Any, funded: Any, category: Any
) -> None:
    from trends.models import TrendItem, TrendSource, TrendSourceKind

    funded.category = category
    funded.save(update_fields=["category"])
    source = TrendSource.objects.create(
        kind=TrendSourceKind.RSS, category=category, platform="instagram", vendor="fake"
    )
    TrendItem.objects.create(
        source=source,
        external_id="t-1",
        body="Morning light on a #ceramic mug",
        posted_at=timezone.now() - dt.timedelta(days=1),
    )

    response = auth_client.get(reverse("tools-readiness"))

    assert rows(response)["trend_corpus"]["status"] == DONE
    assert states(response)[Tool.HASHTAG]["status"] == "ready"


def test_best_time_needs_captures_of_its_own(auth_client: Any, funded: Any) -> None:
    """Nobody's else's numbers will do: the tool reads this workspace's own
    published targets, so with none it has nothing to average."""
    response = auth_client.get(reverse("tools-readiness"))

    assert rows(response)["captures"]["status"] == MISSING
    assert states(response)[Tool.BEST_TIME]["reason"] == "no_captures"


# -----------------------------------------------------------------------------
# Operator-side state
# -----------------------------------------------------------------------------
def test_a_tool_an_operator_switched_off_says_so(auth_client: Any, funded: Any) -> None:
    ToolConfig.objects.filter(slug=Tool.HOOK).update(is_enabled=False)

    assert states(auth_client.get(reverse("tools-readiness")))[Tool.HOOK]["reason"] == "disabled"


def test_an_unseeded_tool_table_is_an_empty_list_not_an_error(
    auth_client: Any, workspace: Any
) -> None:
    response = auth_client.get(reverse("tools-readiness"))

    assert response.status_code == 200
    assert response.json()["tools"] == []


# -----------------------------------------------------------------------------
# Tenancy
# -----------------------------------------------------------------------------
def test_credits_are_read_for_the_requested_workspace_only(
    auth_client: Any, funded: Any, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    second = provision_workspace(other_user, name="Second Brand")
    auth_client.force_authenticate(other_user)

    response = auth_client.get(
        reverse("tools-readiness"), headers={"X-Workspace-Id": str(second.pk)}
    )

    assert rows(response)["credits"]["status"] == MISSING
