"""Collection and scoring.

Collection must be idempotent (a re-read refreshes, never duplicates), isolated
(one dead provider never stops the other) and due-based (a source runs on its
own cadence). Scoring must rank within a provider, never across units, keep
fixtures out of real ranks, and say nothing it cannot measure.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from django.utils import timezone

from playbook.collectors import registry
from playbook.collectors.base import CollectorError
from playbook.collectors.fake import FakeCollector
from playbook.models import PlaybookPost, PlaybookSource, PostFormat
from playbook.services import collect, scoring

pytestmark = pytest.mark.django_db


# --- collection ------------------------------------------------------------------------
def test_collection_is_idempotent(market_source: PlaybookSource) -> None:
    collect.collect_source(market_source)
    first = PlaybookPost.objects.count()
    collect.collect_source(market_source)

    assert first > 0
    assert PlaybookPost.objects.count() == first


def test_a_re_read_refreshes_the_numbers(market_source: PlaybookSource) -> None:
    collect.collect_source(market_source)
    row = PlaybookPost.objects.first()
    assert row is not None
    PlaybookPost.objects.filter(pk=row.pk).update(views=1)

    collect.collect_source(market_source)

    row.refresh_from_db()
    assert row.views != 1


def test_one_dead_provider_never_stops_the_other(
    market_source: PlaybookSource, trends_source: PlaybookSource
) -> None:
    registry.set_override(
        "youtube", FakeCollector("youtube", fail_with=CollectorError("youtube returned 500"))
    )

    result = collect.collect_due()

    assert result["sources"] == 2
    assert PlaybookPost.objects.filter(provider="google_trends").exists()
    assert not PlaybookPost.objects.filter(provider="youtube").exists()
    market_source.refresh_from_db()
    assert market_source.consecutive_failures == 1
    assert "500" in market_source.last_error


def test_the_third_failure_in_a_row_is_an_error(
    market_source: PlaybookSource, caplog: pytest.LogCaptureFixture
) -> None:
    registry.set_override("youtube", FakeCollector("youtube", fail_with=CollectorError("down")))
    with caplog.at_level(logging.WARNING, logger="playbook.services.collect"):
        for _ in range(3):
            collect.collect_source(market_source)

    levels = [r.levelno for r in caplog.records if r.getMessage() == "playbook source failed"]
    assert levels == [logging.WARNING, logging.WARNING, logging.ERROR]


def test_a_success_resets_the_failure_count(market_source: PlaybookSource) -> None:
    market_source.consecutive_failures = 2
    market_source.last_error = "old"
    market_source.save()

    collect.collect_source(market_source)

    market_source.refresh_from_db()
    assert market_source.consecutive_failures == 0
    assert market_source.last_error == ""


def test_a_source_runs_on_its_own_cadence(market_source: PlaybookSource) -> None:
    now = timezone.now()
    market_source.last_run_at = now - dt.timedelta(minutes=30)
    market_source.refresh_minutes = 120
    market_source.save()
    assert collect.due_sources(now) == []

    assert collect.due_sources(now + dt.timedelta(minutes=91)) == [market_source]


def test_a_chart_sighting_never_erases_a_category(food: Any, market_source: PlaybookSource) -> None:
    search = PlaybookSource.objects.create(
        provider="youtube", region="TN", category=food, config={"search_terms": ["recipe"]}
    )
    # The same fake id from both sources: make the chart return the search's posts.
    collect.collect_source(search)
    found = PlaybookPost.objects.filter(category=food).first()
    assert found is not None
    row = found
    PlaybookPost.objects.filter(pk=row.pk).update(source=market_source)
    from playbook.collectors.base import CollectedPost

    class Same:
        provider = "youtube"
        reports: frozenset[str] = frozenset()

        def plan(self, source: Any) -> list[Any]:
            from playbook.collectors.base import CollectRequest

            return [CollectRequest("youtube")]

        def fetch(self, request: Any) -> list[CollectedPost]:
            return [
                CollectedPost(
                    external_id=row.external_id,
                    posted_at=row.posted_at,
                    format=row.format,
                    url=row.url,
                    title=row.title,
                    views=5,
                )
            ]

    registry.set_override("youtube", Same())
    collect.collect_source(market_source)

    row.refresh_from_db()
    assert row.category == food
    assert row.views == 5


# --- scoring ---------------------------------------------------------------------------
def test_velocity_engagement_and_null_rules(make_post: Any) -> None:
    now = timezone.now()
    full = make_post(views=6000, likes=300, comments=60, posted_at=now - dt.timedelta(hours=12))
    hidden = make_post(views=6000, likes=None, comments=60, posted_at=now - dt.timedelta(hours=12))
    fresh = make_post(views=600, posted_at=now - dt.timedelta(minutes=5))

    scoring.rescore("TN", now=now)

    for row in (full, hidden, fresh):
        row.refresh_from_db()
    assert full.velocity == pytest.approx(500)
    assert full.engagement == pytest.approx(6.0)
    assert hidden.engagement is None  # a hidden like count is not zero
    # Five minutes old is scored as six hours, not as 7,200 views an hour.
    assert fresh.velocity == pytest.approx(100)


def test_trend_score_ranks_within_a_provider_not_across_units(
    make_post: Any, trends_source: PlaybookSource
) -> None:
    now = timezone.now()
    slow = make_post(views=600, posted_at=now - dt.timedelta(hours=10))
    fast = make_post(views=60000, posted_at=now - dt.timedelta(hours=10))
    search = PlaybookPost.objects.create(
        source=trends_source,
        provider="google_trends",
        region="TN",
        external_id="TN:tea",
        format=PostFormat.SEARCH,
        url="https://www.google.com/search?q=tea",
        title="Tea",
        posted_at=now - dt.timedelta(hours=10),
        traffic_floor=5_000_000,
        last_seen_at=now,
    )

    scoring.rescore("TN", now=now)

    for row in (slow, fast, search):
        row.refresh_from_db()
    assert (slow.trend_score, fast.trend_score) == (0, 100)
    # Alone on its provider's wall: no rank to give, rather than a flattering 100.
    assert search.trend_score is None


def test_fixtures_never_move_a_real_rank(make_post: Any) -> None:
    now = timezone.now()
    real_a = make_post(views=1000)
    real_b = make_post(views=2000)
    make_post(views=99_000_000, is_fake=True)
    make_post(views=98_000_000, is_fake=True)

    scoring.rescore("TN", now=now)

    real_a.refresh_from_db()
    real_b.refresh_from_db()
    assert (real_a.trend_score, real_b.trend_score) == (0, 100)


def test_structure_says_only_what_was_measured(make_post: Any) -> None:
    now = timezone.now()
    question = make_post(
        title="Is this the best harissa?",
        views=60000,
        duration_seconds=23,
        posted_at=now - dt.timedelta(hours=10),
    )
    make_post(views=1000, posted_at=now - dt.timedelta(hours=10))
    make_post(views=2000, posted_at=now - dt.timedelta(hours=10))
    hidden = make_post(
        title="3 ways with oil", likes=None, views=1500, posted_at=now - dt.timedelta(hours=10)
    )

    scoring.rescore("TN", now=now)

    question.refresh_from_db()
    hidden.refresh_from_db()
    assert question.structure[0] == "Short video, 0:23"
    assert "The title asks a question" in question.structure
    assert any("faster than the median post on this wall" in fact for fact in question.structure)
    assert "The title leads with a number" in hidden.structure
    assert "The like count is hidden by the creator" in hidden.structure
    joined = " ".join(question.structure + hidden.structure)
    assert "will" not in joined and "%" not in joined  # no prediction, no invented rate


def test_a_search_describes_its_bucket_and_news(trends_source: PlaybookSource) -> None:
    now = timezone.now()
    row = PlaybookPost.objects.create(
        source=trends_source,
        provider="google_trends",
        region="TN",
        external_id="TN:tea",
        format=PostFormat.SEARCH,
        url="https://www.google.com/search?q=tea",
        title="Tea",
        posted_at=now - dt.timedelta(hours=5),
        traffic_floor=2000,
        traffic_label="2,000+",
        news=[{"title": "a", "url": "", "source": "b"}],
        last_seen_at=now,
    )

    scoring.rescore("TN", now=now)

    row.refresh_from_db()
    assert row.structure == [
        "About 2,000+ searches so far (Google's own range)",
        "Rising for 5 h",
        "1 news story attached",
    ]
