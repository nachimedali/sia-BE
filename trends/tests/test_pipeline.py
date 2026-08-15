"""The extraction pipeline, stage by stage (design.md §8.4).

The five named tests implementation.md Phase 10 asks for live here, plus the
per-stage coverage that makes a failure point at the stage that broke rather
than at "extraction returned the wrong thing".
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from trends.models import TrendCluster, TrendItem
from trends.services import extract, extraction, ingest, normalize, scoring
from trends.services.clustering import cluster_window, embed_items
from trends.services.normalize import (
    EXCLUDED_DUPLICATE,
    EXCLUDED_LANGUAGE,
    EXCLUDED_SPAM,
)

pytestmark = pytest.mark.django_db


def _window(source: Any) -> list[TrendItem]:
    ingest.ingest_source(source)
    return ingest.window_items([source], days=365)


# -----------------------------------------------------------------------------
# test_forced_extract_materialises_clusters_for_category
# -----------------------------------------------------------------------------
def test_forced_extract_materialises_clusters_for_category(category: Any, source: Any) -> None:
    """Phase 10's done-when, end to end: nothing in the database, one call, a
    ranked corpus for that category on that platform."""
    assert not TrendCluster.objects.exists()

    clusters = extract(category_id=category.pk, platform=source.platform, force=True)

    assert clusters
    assert all(cluster.category_id == category.pk for cluster in clusters)
    assert all(cluster.platform == source.platform for cluster in clusters)
    # Ranked, and every cluster is evidence-backed rather than a lone post.
    assert [c.composite_score for c in clusters] == sorted(
        (c.composite_score for c in clusters), reverse=True
    )
    assert all(cluster.item_count >= 2 for cluster in clusters)
    # The exemplars the UI renders are attached, not orphaned.
    assert all(cluster.items.exists() for cluster in clusters)


def test_extraction_inherits_sources_from_the_nearest_ancestor(category: Any, source: Any) -> None:
    """Sources are seeded on roots (`seed_trend_sources`) but a workspace's
    category is a leaf, so a leaf with none of its own must borrow rather than
    come back empty."""
    assert source.category_id == category.parent_id

    clusters = extract(category_id=category.pk, platform=source.platform, force=True)

    assert clusters


# -----------------------------------------------------------------------------
# test_trend_ingest_is_idempotent_on_external_id
# -----------------------------------------------------------------------------
def test_trend_ingest_is_idempotent_on_external_id(source: Any) -> None:
    """The refresh action (D11) is a button a user can press twice."""
    ingest.ingest_source(source)
    first = TrendItem.objects.count()
    assert first > 0

    source.watermark_at = None  # force the vendor to hand back the same page
    source.save(update_fields=["watermark_at"])
    ingest.ingest_source(source)

    assert TrendItem.objects.count() == first


def test_ingest_advances_the_watermark_and_asks_only_for_what_is_new(source: Any) -> None:
    from trends.providers.fake import get_fake_vendor

    ingest.ingest_source(source)
    source.refresh_from_db()
    assert source.watermark_at is not None

    ingest.ingest_source(source)

    vendor: Any = get_fake_vendor(source.kind)
    assert vendor.calls[0]["since"] is None
    assert vendor.calls[1]["since"] == source.watermark_at


def test_re_ingest_does_not_undo_what_normalisation_rejected(source: Any) -> None:
    """Metrics are refreshed on a re-fetch; the pipeline's own columns are not.
    Otherwise every refresh would silently re-admit the spam."""
    normalize.normalize(_window(source))
    rejected = TrendItem.objects.exclude(excluded_reason="").count()
    assert rejected > 0

    source.watermark_at = None
    source.save(update_fields=["watermark_at"])
    ingest.ingest_source(source)

    assert TrendItem.objects.exclude(excluded_reason="").count() == rejected


def test_a_dead_source_does_not_fail_the_extraction(
    category: Any, source: Any, monkeypatch: Any
) -> None:
    """`trends_q` is lowest priority and the last corpus stays valid
    (design.md §5.1) — one vendor being down is a log line, not a 500."""
    from trends.providers.base import TrendVendorError

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise TrendVendorError("vendor down")

    monkeypatch.setattr("trends.services.ingest.get_trend_vendor", explode)

    assert ingest.ingest_source(source) == 0


# -----------------------------------------------------------------------------
# Stage 2 — normalize
# -----------------------------------------------------------------------------
def test_normalisation_marks_language_duplicates_and_spam(source: Any) -> None:
    kept = normalize.normalize(_window(source))

    reasons = dict(
        TrendItem.objects.exclude(excluded_reason="").values_list("external_id", "excluded_reason")
    )
    # The French item is out on language, the repost on dedup, the giveaway on
    # spam — and the account with the larger following keeps the shared body.
    assert reasons["cc-6"] == EXCLUDED_LANGUAGE
    assert reasons["cc-3"] == EXCLUDED_DUPLICATE
    assert reasons["cc-5"] == EXCLUDED_SPAM
    assert {item.external_id for item in kept} == {"cc-1", "cc-2", "cc-4"}


def test_the_kept_copy_of_a_duplicate_is_the_one_with_the_audience(source: Any) -> None:
    normalize.normalize(_window(source))

    assert TrendItem.objects.get(external_id="cc-2").excluded_reason == ""
    assert TrendItem.objects.get(external_id="cc-3").excluded_reason == EXCLUDED_DUPLICATE


def test_normalisation_is_stable_when_run_twice(source: Any) -> None:
    window = _window(source)
    first = normalize.normalize(window)
    second = normalize.normalize(ingest.window_items([window[0].source], days=365))

    assert {item.pk for item in first} == {item.pk for item in second}


# -----------------------------------------------------------------------------
# test_scoring_matches_reference_vectors
# -----------------------------------------------------------------------------
def test_scoring_matches_reference_vectors(source: Any) -> None:
    """Fixed inputs, asserted composite scores. The formula is design.md §8.4's,
    so a change to a weight or a half-life has to be a deliberate edit here
    rather than a silent re-ranking."""
    with time_machine.travel(dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC), tick=False):
        scored = scoring.score(normalize.normalize(_window(source)))

    by_id = {item.external_id: item for item in scored}
    # Every component is a percentile within the kind, so with three survivors
    # the ranks can only be 0.0, 0.5 and 1.0 — and the composite is their
    # weighted sum, which pins the formula, the weights and the normalisation
    # all at once:
    #   cc-2  0.40·1.0 + 0.25·0.5 + 0.10·0.0 + 0.25·0.5 = 0.65
    #   cc-4  0.40·0.5 + 0.25·1.0 + 0.10·1.0 + 0.25·0.0 = 0.55
    #   cc-1  0.40·0.0 + 0.25·0.0 + 0.10·0.5 + 0.25·1.0 = 0.30
    assert round(by_id["cc-2"].composite_score, 4) == 0.65
    assert round(by_id["cc-4"].composite_score, 4) == 0.55
    assert round(by_id["cc-1"].composite_score, 4) == 0.30
    # Velocity leads the formula, so the post pulling hardest *now* wins even
    # though cc-4 has the bigger audience and cc-1 the better raw engagement.
    assert scored[0].external_id == "cc-2"


def test_scoring_ranks_within_a_source_kind_not_across_them(category: Any) -> None:
    """A Reddit thread reports no followers and a YouTube channel reports
    hundreds of thousands. Without the per-kind partition the denominators
    decide the ranking and one source sweeps every cluster."""
    from content.models import Platform
    from trends.models import TrendSource, TrendSourceKind

    for kind in (TrendSourceKind.YOUTUBE, TrendSourceKind.REDDIT):
        TrendSource.objects.create(
            category=category, platform=Platform.YOUTUBE, kind=kind, vendor=kind.lower()
        )
    sources = list(TrendSource.objects.all())
    ingest.ingest_sources(sources)
    scored = scoring.score(normalize.normalize(ingest.window_items(sources, days=365)))

    # One item per kind: each is top of its own distribution, so both come out
    # at the maximum rather than the follower-rich one burying the other.
    assert len(scored) == 2
    assert {round(item.composite_score, 4) for item in scored} == {1.0}


def test_scoring_an_empty_window_is_not_an_error() -> None:
    assert scoring.score([]) == []


# -----------------------------------------------------------------------------
# Stage 4 — cluster
# -----------------------------------------------------------------------------
def test_reworded_posts_about_one_idea_land_in_one_cluster(category: Any, source: Any) -> None:
    """The fixture's two unboxing posts are deliberately reworded rather than
    identical, so dedup leaves them both and clustering is what has to notice
    they are the same idea."""
    clusters = extract(category_id=category.pk, platform=source.platform, force=True)

    labels = {cluster.label: cluster for cluster in clusters}
    unboxing = next(c for c in labels.values() if "unboxing" in c.label)
    assert unboxing.item_count == 2
    assert {item.external_id for item in unboxing.items.all()} == {"cc-1", "cc-2"}


def test_a_cluster_of_one_is_not_published(category: Any, source: Any) -> None:
    """`item_count` is the evidence the UI shows; "1" would be an
    anti-recommendation."""
    clusters = extract(category_id=category.pk, platform=source.platform, force=True)

    assert all(cluster.item_count >= 2 for cluster in clusters)
    # cc-4 (the flatlay) is a genuine singleton in this corpus and is dropped.
    assert TrendItem.objects.get(external_id="cc-4").cluster_id is None


def test_embeddings_are_computed_once_and_reused(source: Any) -> None:
    from ai.providers.fake import _fake_embedding_provider

    _fake_embedding_provider.clear()
    items = normalize.normalize(_window(source))

    embed_items(items)
    first_call_count = len(_fake_embedding_provider.calls)
    embed_items(list(TrendItem.objects.filter(excluded_reason="")))

    assert first_call_count == 1
    assert len(_fake_embedding_provider.calls) == 1


def test_re_clustering_replaces_rather_than_accumulates(category: Any, source: Any) -> None:
    first = extract(category_id=category.pk, platform=source.platform, force=True)
    second = extract(category_id=category.pk, platform=source.platform, force=True)

    assert TrendCluster.objects.count() == len(second) == len(first)


def test_clustering_an_empty_window_produces_nothing(category: Any) -> None:
    assert (
        cluster_window(category_id=category.pk, platform="tiktok", items=[], window_days=14) == []
    )


# -----------------------------------------------------------------------------
# test_cache_served_until_expiry_then_re_extracted
# -----------------------------------------------------------------------------
def test_cache_served_until_expiry_then_re_extracted(category: Any, source: Any) -> None:
    """D11: extracted on demand, served from cache for 30 days, re-extracted
    once stale. Driven with time-machine, never a sleep."""
    from trends.providers.fake import get_fake_vendor

    vendor: Any = get_fake_vendor(source.kind)
    start = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC)

    with time_machine.travel(start, tick=False):
        first = extract(category_id=category.pk, platform=source.platform)
        assert first
        created_ids = {cluster.pk for cluster in first}
        assert len(vendor.calls) == 1

        # Inside the TTL: the same rows come back, and the vendor is not asked
        # again — which is the entire economic argument for D11.
        again = extract(category_id=category.pk, platform=source.platform)
        assert {cluster.pk for cluster in again} == created_ids
        assert len(vendor.calls) == 1

    with time_machine.travel(start + dt.timedelta(days=31), tick=False):
        # Past `expires_at` nothing is servable, and the next read re-extracts.
        assert extraction.cached_clusters(category.pk, source.platform) == []
        extract(category_id=category.pk, platform=source.platform)
        assert len(vendor.calls) == 2
        # The previous window's clusters are gone rather than lingering as a
        # corpus nobody can date — a source with nothing new to say leaves the
        # category empty, which is what the risk register means by "cached
        # recipes stay valid *to* expires_at".
        assert TrendCluster.objects.filter(pk__in=created_ids).count() == 0


def test_force_re_extracts_inside_the_ttl(category: Any, source: Any) -> None:
    first = extract(category_id=category.pk, platform=source.platform)
    forced = extract(category_id=category.pk, platform=source.platform, force=True)

    assert {c.pk for c in forced} != {c.pk for c in first}


def test_top_cluster_never_triggers_an_extraction(category: Any, source: Any) -> None:
    """Generation reads this on the prompt path — it must not be able to make a
    generation wait on a trend vendor."""
    assert extraction.top_cluster(category.pk, source.platform) is None
    assert not TrendItem.objects.exists()


def test_expiry_is_resolved_at_read_time(category: Any, source: Any) -> None:
    extract(category_id=category.pk, platform=source.platform, force=True)
    TrendCluster.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))

    assert extraction.cached_clusters(category.pk, source.platform) == []
