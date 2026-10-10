"""The trendgen seam (M0).

Two properties matter here and nothing else does yet:

1. **Flag off is pre-M0 behaviour.** The local adapter returns what the direct
   ORM query returned, so repointing the call sites changed nothing.
2. **The two ports stay separate.** Neither carries the other's methods, for
   the same structural reason BUILD-PLAN Part 7 rule 18 keeps publishing and
   measurement apart: a vendor covering one must not be wireable into the other
   by accident.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from billing.services.flags import TRENDGEN_FEED, TRENDGEN_GENERATION, flag_enabled
from integrations import generation as generation_mod
from integrations import trendfeed as trendfeed_mod
from integrations.ports import (
    CORPUS_WINDOW_DAYS,
    ClusterSummary,
    GenerationPort,
    TrendFeedPort,
)

pytestmark = pytest.mark.django_db


# --- Resolution --------------------------------------------------------------


def test_both_flags_default_off(organization):
    """Off means in-process. The remote side does not exist until Phase 12."""
    assert flag_enabled(organization, TRENDGEN_FEED) is False
    assert flag_enabled(organization, TRENDGEN_GENERATION) is False


def test_flag_off_resolves_to_the_local_adapter(organization):
    assert isinstance(trendfeed_mod.get_trend_feed(organization), trendfeed_mod.LocalTrendFeed)
    assert isinstance(generation_mod.get_generation(organization), generation_mod.LocalGeneration)


def test_flipping_a_flag_early_fails_loudly(organization):
    """A premature flip must not silently degrade; it names the phase that
    builds the other side."""
    from billing.models import FeatureFlag

    FeatureFlag.objects.create(organization=organization, key=TRENDGEN_FEED, enabled=True)
    with pytest.raises(NotImplementedError, match="P12-02"):
        trendfeed_mod.get_trend_feed(organization)

    FeatureFlag.objects.create(organization=organization, key=TRENDGEN_GENERATION, enabled=True)
    with pytest.raises(NotImplementedError, match="P12-01"):
        generation_mod.get_generation(organization)


def test_an_override_wins_over_the_flag(organization, trend_feed):
    assert trendfeed_mod.get_trend_feed(organization) is trend_feed


# --- The ports stay separate -------------------------------------------------


def test_neither_port_carries_the_others_methods():
    feed_methods = {m for m in dir(TrendFeedPort) if not m.startswith("_")}
    gen_methods = {m for m in dir(GenerationPort) if not m.startswith("_")}
    assert not feed_methods & gen_methods


def test_the_local_adapters_satisfy_their_protocols():
    assert isinstance(trendfeed_mod.LocalTrendFeed(), TrendFeedPort)
    assert isinstance(generation_mod.LocalGeneration(), GenerationPort)


def test_the_fakes_satisfy_the_same_protocols():
    """A fake that drifts from its port is worse than no fake: the suite goes
    green against a contract the real adapter does not implement."""
    assert isinstance(trendfeed_mod.FakeTrendFeed(), TrendFeedPort)
    assert isinstance(generation_mod.FakeGeneration(), GenerationPort)


# --- Parity with the code the port replaced ----------------------------------


def _trend_item(category, *, body: str, posted_at: dt.datetime, excluded: str = ""):
    from trends.models import TrendItem, TrendSource, TrendSourceKind

    source, _ = TrendSource.objects.get_or_create(
        category=category,
        platform="instagram",
        handle="seam-fixture",
        defaults={"kind": TrendSourceKind.ACCOUNT, "label": "seam"},
    )
    return TrendItem.objects.create(
        source=source,
        external_id=f"x{TrendItem.objects.count()}",
        body=body,
        posted_at=posted_at,
        excluded_reason=excluded,
    )


def test_local_corpus_reads_match_a_direct_query(category):
    from trends.models import TrendItem

    now = timezone.now()
    since = now - dt.timedelta(days=CORPUS_WINDOW_DAYS)
    _trend_item(category, body="#alpha fresh", posted_at=now - dt.timedelta(days=1))
    _trend_item(category, body="#beta fresh", posted_at=now - dt.timedelta(days=2))
    _trend_item(category, body="#stale", posted_at=now - dt.timedelta(days=60))
    _trend_item(category, body="#dropped", posted_at=now, excluded="spam")

    direct = list(
        TrendItem.objects.filter(
            source__category_id=category.id, posted_at__gte=since, excluded_reason=""
        ).values_list("body", flat=True)
    )
    through_port = trendfeed_mod.LocalTrendFeed().corpus_bodies(
        category_id=category.id, since=since
    )

    assert sorted(through_port) == sorted(direct)
    assert sorted(through_port) == ["#alpha fresh", "#beta fresh"]
    assert trendfeed_mod.LocalTrendFeed().corpus_size(category_id=category.id, since=since) == 2


def test_the_limit_is_applied(category):
    now = timezone.now()
    for n in range(5):
        _trend_item(category, body=f"#tag{n}", posted_at=now - dt.timedelta(hours=n))
    bodies = trendfeed_mod.LocalTrendFeed().corpus_bodies(
        category_id=category.id, since=now - dt.timedelta(days=1), limit=3
    )
    assert len(bodies) == 3


def test_an_empty_corpus_is_empty_not_an_error(category):
    since = timezone.now() - dt.timedelta(days=CORPUS_WINDOW_DAYS)
    assert trendfeed_mod.LocalTrendFeed().corpus_bodies(category_id=category.id, since=since) == []
    assert trendfeed_mod.LocalTrendFeed().corpus_size(category_id=category.id, since=since) == 0


def test_top_cluster_returns_a_dto_not_a_model(category):
    """A remote implementation can return JSON; it cannot return a Django row."""
    from trends.models import TrendCluster

    now = timezone.now()
    TrendCluster.objects.create(
        category=category,
        platform="instagram",
        label="sunrise runs",
        item_count=7,
        composite_score=0.9,
        window_start=now - dt.timedelta(days=CORPUS_WINDOW_DAYS),
        window_end=now,
        # `is_fresh` is resolved against the clock at read time, so an expiry in
        # the past would make the cluster invisible rather than stale.
        expires_at=now + dt.timedelta(days=30),
    )
    summary = trendfeed_mod.LocalTrendFeed().top_cluster(
        category_id=category.id, platform="instagram"
    )
    assert isinstance(summary, ClusterSummary)
    assert (summary.label, summary.item_count) == ("sunrise runs", 7)
    assert summary.platform_display  # carried, so the caller needs no model


def test_top_cluster_is_none_when_the_corpus_is_empty(category):
    assert (
        trendfeed_mod.LocalTrendFeed().top_cluster(category_id=category.id, platform="instagram")
        is None
    )


# --- The fakes ----------------------------------------------------------------


def test_the_trend_fake_records_what_was_asked(trend_feed):
    trend_feed.bodies[7] = ["#a", "#b"]
    assert trend_feed.corpus_bodies(category_id=7, since=timezone.now()) == ["#a", "#b"]
    assert trend_feed.calls[0][0] == "corpus_bodies"
    assert trend_feed.calls[0][1]["category_id"] == 7


def test_the_generation_fake_creates_the_same_local_row(generation_port, workspace, user):
    from ai.models import Generation, GenerationKind, GenerationMode

    row = generation_port.generate(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.AUTOPILOT,
        prompt="brief",
        taste_profile_version=3,
    )
    assert isinstance(row, Generation)
    assert row.pk is not None
    assert row.taste_profile_version == 3
    assert generation_port.calls[0]["prompt"] == "brief"


# --- the fakes' knobs, exercised through the real callers --------------------


def test_readiness_reports_no_price_rather_than_erroring(generation_port, workspace):
    """`_per_slot_cost` returns None so an unseeded environment gets guidance
    instead of a 400 — the reason the port has its own error type at all."""
    from products.services import readiness

    generation_port.unconfigured = True
    assert readiness._per_slot_cost(workspace) is None


def test_readiness_sums_the_two_slot_costs(generation_port, workspace):
    from ai.models import GenerationKind, GenerationMode
    from products.services import readiness

    generation_port.costs = {
        (GenerationKind.IMAGE, GenerationMode.AUTOPILOT): 7,
        (GenerationKind.TEXT, GenerationMode.AUTOPILOT): 2,
    }
    assert readiness._per_slot_cost(workspace) == 9


def test_the_prompt_is_grounded_from_the_scripted_cluster(trend_feed, workspace, category):
    """The trend fake's `clusters` knob, read through the code that consumes it."""
    from ai.services import prompting

    workspace.category = category
    workspace.platforms = ["instagram"]
    workspace.save()

    trend_feed.clusters[(category.id, "instagram")] = ClusterSummary(
        label="sunrise runs", platform_display="Instagram", item_count=9
    )
    signal = prompting._category_signal(workspace)

    assert signal is not None
    assert "sunrise runs" in signal and "9 independent posts" in signal
    assert trend_feed.calls[0][0] == "top_cluster"


def test_no_cluster_means_an_ungrounded_prompt_not_an_error(trend_feed, workspace, category):
    from ai.services import prompting

    workspace.category = category
    workspace.platforms = ["instagram"]
    workspace.save()

    assert prompting._category_signal(workspace) is None
