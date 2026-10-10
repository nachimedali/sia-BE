"""The calendar's read and write surface (`new_temp/calendar.html`).

What the page needs that the planning board did not: a window of time, filters
that run on the server (the list paginates, so a client-side filter would drop
the 101st post), a way to *propose* a time without scheduling, an upload that
lands in review, and an honest auto-schedule.

The rule these tests keep returning to is L-2: **nothing here publishes**.
Placing a post on the calendar writes a proposal; the schedule service, behind
the approval gate, remains the only writer of `scheduled_at`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.services import signals
from collaboration.models import Thread
from content.models import Post, PostSource, PostStatus
from content.services.posts import create_post
from products.services.products import create_product

pytestmark = pytest.mark.django_db

POSTS = "/api/v1/posts/"


def _at(days: float = 3, hour: int = 10) -> dt.datetime:
    base = timezone.now() + dt.timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _post(workspace: Any, user: Any, body: str = "A post", **fields: Any) -> Post:
    post = create_post(workspace=workspace, author=user, master_body=body)
    for name, value in fields.items():
        setattr(post, name, value)
    if fields:
        post.save(update_fields=[*fields, "updated_at"])
    return post


@pytest.fixture
def pro(paid_workspace: Any) -> Any:
    return paid_workspace


@pytest.fixture
def other_workspace(plans: Any) -> Any:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    owner = get_user_model().objects.create_user(email="other-owner@example.com", password="x")
    return provision_workspace(owner, name="Someone Else's Workspace")


# --- fields the calendar draws from -----------------------------------------
def test_a_post_carries_its_product_and_the_time_it_is_placed_at(
    auth_client: Any, pro: Any, user: Any
) -> None:
    product = create_product(workspace=pro, name="Olive oil")
    proposed = _at(4)
    _post(
        pro,
        user,
        product=product,
        status=PostStatus.PENDING_REVIEW,
        proposed_scheduled_at=proposed,
        planned_platforms=["instagram"],
    )

    row = auth_client.get(POSTS).json()["results"][0]

    assert row["product"] == product.pk
    assert row["product_name"] == "Olive oil"
    assert row["planned_platforms"] == ["instagram"]
    assert row["effective_at"] == row["proposed_scheduled_at"]


def test_effective_time_is_the_schedule_once_there_is_one(
    auth_client: Any, pro: Any, user: Any
) -> None:
    when = _at(2)
    _post(
        pro,
        user,
        status=PostStatus.SCHEDULED,
        scheduled_at=when,
        proposed_scheduled_at=_at(5),
    )
    row = auth_client.get(POSTS).json()["results"][0]
    assert row["effective_at"] == row["scheduled_at"]


def test_open_threads_are_counted_per_post(auth_client: Any, pro: Any, user: Any) -> None:
    post = _post(pro, user)
    Thread.objects.create(workspace=pro, post=post, title="Crop", opened_by=user)
    Thread.objects.create(workspace=pro, post=post, title="Done one", opened_by=user, status="DONE")
    assert auth_client.get(POSTS).json()["results"][0]["open_thread_count"] == 1


# --- filters run on the server -----------------------------------------------
def test_filters_by_product_source_and_platform(auth_client: Any, pro: Any, user: Any) -> None:
    oil = create_product(workspace=pro, name="Oil")
    soap = create_product(workspace=pro, name="Soap")
    a = _post(pro, user, "a", product=oil, source=PostSource.AI, planned_platforms=["instagram"])
    _post(pro, user, "b", product=soap, source=PostSource.UPLOAD, planned_platforms=["facebook"])

    def ids(query: str) -> list[int]:
        return [r["id"] for r in auth_client.get(f"{POSTS}?{query}").json()["results"]]

    assert ids(f"product={oil.pk}") == [a.pk]
    assert ids("source=AI") == [a.pk]
    assert ids("platform=instagram") == [a.pk]
    assert len(ids(f"product={oil.pk}&product={soap.pk}")) == 2
    assert ids("source=AI&platform=facebook") == []


def test_a_post_with_targets_is_matched_by_its_targets_not_its_plan(
    auth_client: Any, pro: Any, user: Any, social_account: Any
) -> None:
    from content.models import PostTarget

    post = _post(pro, user, planned_platforms=["facebook"])
    PostTarget.objects.create(post=post, platform="instagram", social_account=social_account)

    found = lambda p: [r["id"] for r in auth_client.get(f"{POSTS}?platform={p}").json()["results"]]  # noqa: E731
    assert found("instagram") == [post.pk]


def test_search_matches_the_caption_the_product_and_a_comment(
    auth_client: Any, pro: Any, user: Any
) -> None:
    from collaboration.services import open_thread

    oil = create_product(workspace=pro, name="Olive oil")
    a = _post(pro, user, "Harvest week", product=oil)
    b = _post(pro, user, "Something else")
    open_thread(b, author=user, title="Crop", body="Make the jar bigger")

    def ids(q: str) -> list[int]:
        return [r["id"] for r in auth_client.get(f"{POSTS}?q={q}").json()["results"]]

    assert ids("harvest") == [a.pk]
    assert ids("olive") == [a.pk]
    assert ids("jar") == [b.pk]
    assert ids("nothing-matches-this") == []


def test_a_window_selects_by_the_time_a_post_is_placed_at(
    auth_client: Any, pro: Any, user: Any
) -> None:
    inside = _post(pro, user, "in", status=PostStatus.PENDING_REVIEW, proposed_scheduled_at=_at(3))
    _post(pro, user, "out", status=PostStatus.PENDING_REVIEW, proposed_scheduled_at=_at(40))
    scheduled = _post(
        pro,
        user,
        "sched",
        status=PostStatus.SCHEDULED,
        scheduled_at=_at(4),
        delivery_mode="REMINDER",
    )
    _post(pro, user, "unplaced")

    start = (timezone.now() + dt.timedelta(days=1)).isoformat()
    end = (timezone.now() + dt.timedelta(days=10)).isoformat()
    response = auth_client.get(POSTS, {"from": start, "to": end})

    assert {r["id"] for r in response.json()["results"]} == {inside.pk, scheduled.pk}


def test_unscheduled_lists_what_is_not_on_the_calendar_yet(
    auth_client: Any, pro: Any, user: Any
) -> None:
    draft = _post(pro, user, "draft")
    _post(pro, user, "placed", proposed_scheduled_at=_at(3))
    _post(pro, user, "published", status=PostStatus.PUBLISHED)

    ids = [r["id"] for r in auth_client.get(f"{POSTS}?unscheduled=true").json()["results"]]
    assert ids == [draft.pk]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("source=NOPE", "invalid_source"),
        ("from=yesterday", "invalid_date"),
        ("to=2026-13-45", "invalid_date"),
        ("product=abc", "invalid_product"),
    ],
)
def test_a_bad_filter_is_a_400_not_an_unfiltered_page(
    auth_client: Any, pro: Any, query: str, code: str
) -> None:
    response = auth_client.get(f"{POSTS}?{query}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_filters_never_cross_a_workspace(
    auth_client: Any, pro: Any, user: Any, other_workspace: Any
) -> None:
    theirs = create_product(workspace=other_workspace, name="Theirs")
    owner = other_workspace.memberships.first().user
    _post(other_workspace, owner, "not yours", product=theirs)
    assert auth_client.get(f"{POSTS}?product={theirs.pk}").json()["results"] == []


# --- proposing a time: not scheduling -----------------------------------------
def test_proposing_a_time_places_a_draft_without_scheduling_it(
    auth_client: Any, pro: Any, user: Any
) -> None:
    post = _post(pro, user)
    when = _at(5)

    response = auth_client.post(
        f"{POSTS}{post.pk}/propose-time/", {"scheduled_at": when.isoformat()}, format="json"
    )

    assert response.status_code == 200, response.json()
    post.refresh_from_db()
    assert post.proposed_scheduled_at == when
    assert post.proposed_delivery_mode  # chosen from the plan, never blank
    assert post.scheduled_at is None  # the schedule service is the only writer of this
    assert post.status == PostStatus.DRAFT  # placed, not submitted, not published


def test_the_default_delivery_follows_the_plan_and_can_be_chosen(
    auth_client: Any, workspace: Any, user: Any, plans: Any
) -> None:
    free = _post(workspace, user)
    auth_client.post(
        f"{POSTS}{free.pk}/propose-time/", {"scheduled_at": _at(3).isoformat()}, format="json"
    )
    free.refresh_from_db()
    assert free.proposed_delivery_mode == "REMINDER"  # Free is reminders-only (D4)

    again = auth_client.post(
        f"{POSTS}{free.pk}/propose-time/",
        {"scheduled_at": _at(4).isoformat(), "delivery_mode": "REMINDER"},
        format="json",
    )
    assert again.status_code == 200


def test_a_proposal_can_be_withdrawn(auth_client: Any, pro: Any, user: Any) -> None:
    post = _post(pro, user, proposed_scheduled_at=_at(3), proposed_delivery_mode="REMINDER")
    response = auth_client.post(
        f"{POSTS}{post.pk}/propose-time/", {"scheduled_at": None}, format="json"
    )
    assert response.status_code == 200
    post.refresh_from_db()
    assert post.proposed_scheduled_at is None
    assert post.proposed_delivery_mode == ""


def test_a_proposal_in_the_past_is_a_400(auth_client: Any, pro: Any, user: Any) -> None:
    post = _post(pro, user)
    past = (timezone.now() - dt.timedelta(hours=1)).isoformat()
    response = auth_client.post(
        f"{POSTS}{post.pk}/propose-time/", {"scheduled_at": past}, format="json"
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "status",
    [PostStatus.PUBLISHED, PostStatus.SCHEDULED, PostStatus.REMINDER_ARMED, PostStatus.PUBLISHING],
)
def test_a_post_that_is_out_of_the_review_loop_cannot_be_re_proposed(
    auth_client: Any, pro: Any, user: Any, status: str
) -> None:
    """409, not 403 and not 400: the request is well-formed and permitted, this
    post is in the wrong state (rule 3). Scheduled posts move through
    `/schedule/`, which applies the horizon and approval gates."""
    post = _post(pro, user, status=status, scheduled_at=_at(2))
    response = auth_client.post(
        f"{POSTS}{post.pk}/propose-time/", {"scheduled_at": _at(5).isoformat()}, format="json"
    )
    assert response.status_code == 409
    post.refresh_from_db()
    assert post.proposed_scheduled_at is None


def test_a_document_has_no_slot_to_propose(auth_client: Any, pro: Any, user: Any) -> None:
    doc = create_post(workspace=pro, author=user, content_kind="DOC", doc_body=[])
    response = auth_client.post(
        f"{POSTS}{doc.pk}/propose-time/", {"scheduled_at": _at(3).isoformat()}, format="json"
    )
    assert response.status_code == 409


def test_proposing_a_time_on_another_workspaces_post_is_a_404(
    auth_client: Any, pro: Any, other_workspace: Any
) -> None:
    owner = other_workspace.memberships.first().user
    theirs = _post(other_workspace, owner)
    response = auth_client.post(
        f"{POSTS}{theirs.pk}/propose-time/", {"scheduled_at": _at(3).isoformat()}, format="json"
    )
    assert response.status_code == 404


def test_a_proposal_is_what_approval_later_schedules(
    auth_client: Any, pro: Any, user: Any, social_account: Any
) -> None:
    """The point of a proposal: the calendar placement is consumed by the final
    approval, through the ordinary schedule service."""
    post = _post(pro, user)
    when = _at(6)
    auth_client.post(
        f"{POSTS}{post.pk}/propose-time/",
        {"scheduled_at": when.isoformat(), "delivery_mode": "REMINDER"},
        format="json",
    )
    assert auth_client.post(f"{POSTS}{post.pk}/submit/", {}, format="json").status_code == 200
    post.refresh_from_db()
    assert post.status == PostStatus.PENDING_REVIEW
    assert post.proposed_scheduled_at == when  # a plain submit kept the placement


# --- planned platforms on create ------------------------------------------------
def test_a_draft_created_with_planned_platforms_keeps_them(auth_client: Any, pro: Any) -> None:
    """The serializer accepted the field on POST and `perform_create` dropped it,
    so the calendar's platform filter could never find a draft the author had
    planned for a platform — an input accepted and silently ignored."""
    response = auth_client.post(
        POSTS, {"master_body": "Planned.", "planned_platforms": ["instagram"]}, format="json"
    )

    assert response.status_code == 201, response.json()
    assert response.json()["planned_platforms"] == ["instagram"]
    assert Post.objects.get(pk=response.json()["id"]).planned_platforms == ["instagram"]
    found = auth_client.get(f"{POSTS}?platform=instagram").json()
    assert [row["id"] for row in found["results"]] == [response.json()["id"]]


def test_an_unknown_planned_platform_is_a_400_not_ignored(auth_client: Any, pro: Any) -> None:
    response = auth_client.post(
        POSTS, {"master_body": "x", "planned_platforms": ["myspace"]}, format="json"
    )
    assert response.status_code == 400


# --- upload --------------------------------------------------------------------
def test_an_upload_lands_in_review_at_the_chosen_time(
    auth_client: Any, pro: Any, user: Any, make_png_upload: Any
) -> None:
    product = create_product(workspace=pro, name="Olive oil")
    when = _at(3)

    response = auth_client.post(
        f"{POSTS}upload/",
        {
            "file": make_png_upload("piece.png"),
            "product": product.pk,
            "platform": "instagram",
            "caption": "Our own photograph.",
            "scheduled_at": when.isoformat(),
        },
        format="multipart",
    )

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["source"] == "UPLOAD"
    assert body["status"] == "PENDING_REVIEW"  # never straight to a schedule (L-2)
    assert body["product"] == product.pk
    assert body["planned_platforms"] == ["instagram"]
    assert body["master_body"] == "Our own photograph."
    assert len(body["media"]) == 1
    assert body["scheduled_at"] is None
    assert body["proposed_scheduled_at"] is not None


def test_an_upload_without_a_time_stays_a_draft(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    response = auth_client.post(
        f"{POSTS}upload/",
        {"file": make_png_upload(), "caption": "Later."},
        format="multipart",
    )
    assert response.status_code == 201
    assert response.json()["status"] == "DRAFT"


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"caption": ""}, "caption"),
        ({"caption": "x", "platform": "myspace"}, "platform"),
        ({"caption": "x", "scheduled_at": "2001-01-01T00:00:00Z"}, "scheduled_at"),
    ],
)
def test_an_upload_names_what_is_wrong_with_it(
    auth_client: Any, pro: Any, make_png_upload: Any, data: dict[str, Any], field: str
) -> None:
    response = auth_client.post(
        f"{POSTS}upload/", {"file": make_png_upload(), **data}, format="multipart"
    )
    assert response.status_code == 400
    assert field in str(response.json())


def test_an_upload_needs_a_file(auth_client: Any, pro: Any) -> None:
    response = auth_client.post(f"{POSTS}upload/", {"caption": "x"}, format="multipart")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_file"
    assert Post.objects.count() == 0


def test_an_upload_cannot_name_another_workspaces_product(
    auth_client: Any, pro: Any, other_workspace: Any, make_png_upload: Any
) -> None:
    theirs = create_product(workspace=other_workspace, name="Theirs")
    response = auth_client.post(
        f"{POSTS}upload/",
        {"file": make_png_upload(), "caption": "x", "product": theirs.pk},
        format="multipart",
    )
    assert response.status_code == 400
    assert Post.objects.count() == 0  # nothing half-created


# --- summary ---------------------------------------------------------------------
def test_the_summary_counts_by_status_under_the_same_filters(
    auth_client: Any, pro: Any, user: Any
) -> None:
    oil = create_product(workspace=pro, name="Oil")
    soap = create_product(workspace=pro, name="Soap")
    _post(pro, user, product=oil, status=PostStatus.PENDING_REVIEW, proposed_scheduled_at=_at(3))
    _post(pro, user, product=oil, status=PostStatus.PUBLISHED, scheduled_at=_at(-3))
    p3 = _post(pro, user, product=soap, status=PostStatus.PENDING_REVIEW)
    Thread.objects.create(workspace=pro, post=p3, title="Open one", opened_by=user)

    everything = auth_client.get(f"{POSTS}summary/").json()
    assert everything["total"] == 3
    assert everything["by_status"]["PENDING_REVIEW"] == 2
    assert everything["open_threads"] == 1

    only_oil = auth_client.get(f"{POSTS}summary/?product={oil.pk}").json()
    assert only_oil["total"] == 2
    assert only_oil["open_threads"] == 0


def test_the_summary_counts_the_window_when_given_one(
    auth_client: Any, pro: Any, user: Any
) -> None:
    _post(pro, user, status=PostStatus.PENDING_REVIEW, proposed_scheduled_at=_at(3))
    _post(pro, user, status=PostStatus.PENDING_REVIEW, proposed_scheduled_at=_at(60))
    start = (timezone.now() + dt.timedelta(days=1)).isoformat()
    end = (timezone.now() + dt.timedelta(days=10)).isoformat()
    body = auth_client.get(f"{POSTS}summary/", {"from": start, "to": end}).json()
    assert body["in_window"] == 1
    assert body["total"] == 2


# --- auto-schedule at the best hour --------------------------------------------
def _performance(platform: str, weekday: int, hour: int) -> list[signals.TargetPerformance]:
    base = timezone.now() - dt.timedelta(days=20)
    # Two posts in the same weekday/hour bucket: `best_times` drops a bucket of one.
    rows = []
    for i in range(2):
        when = base - dt.timedelta(days=(base.weekday() - weekday) % 7 + 7 * i)
        when = when.replace(hour=hour, minute=0, second=0, microsecond=0)
        rows.append(
            signals.TargetPerformance(
                target_id=i,
                post_id=i,
                platform=platform,
                published_at=when,
                engagement_rate=0.1 + i / 100,
                impressions=100,
                likes=10,
                comments=1,
            )
        )
    return rows


def test_auto_schedule_places_each_draft_at_the_platforms_best_hour_as_a_proposal(
    auth_client: Any, pro: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signals, "performance", lambda *a, **k: _performance("instagram", 2, 19))
    post = _post(pro, user, planned_platforms=["instagram"])

    response = auth_client.post(f"{POSTS}auto-schedule/", {}, format="json")

    assert response.status_code == 200, response.json()
    (outcome,) = response.json()["results"]
    assert outcome["post"] == post.pk
    assert outcome["reason"] == ""
    post.refresh_from_db()
    assert post.proposed_scheduled_at is not None
    assert post.proposed_scheduled_at > timezone.now()
    assert post.proposed_scheduled_at.weekday() == 2
    assert post.proposed_scheduled_at.hour == 19
    assert post.scheduled_at is None and post.status == PostStatus.DRAFT


def test_auto_schedule_says_so_when_there_is_no_best_hour_instead_of_inventing_one(
    auth_client: Any, pro: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signals, "performance", lambda *a, **k: [])
    post = _post(pro, user, planned_platforms=["instagram"])

    outcome = auth_client.post(f"{POSTS}auto-schedule/", {}, format="json").json()["results"][0]

    assert outcome["reason"] == "no_best_time"
    post.refresh_from_db()
    assert post.proposed_scheduled_at is None  # unavailable is not a default


def test_auto_schedule_leaves_a_post_with_no_platform_alone(
    auth_client: Any, pro: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signals, "performance", lambda *a, **k: _performance("instagram", 2, 19))
    _post(pro, user)
    outcome = auth_client.post(f"{POSTS}auto-schedule/", {}, format="json").json()["results"][0]
    assert outcome["reason"] == "no_platform"


def test_auto_schedule_does_not_stack_two_posts_on_one_platform_in_one_slot(
    auth_client: Any, pro: Any, user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signals, "performance", lambda *a, **k: _performance("instagram", 2, 19))
    a = _post(pro, user, planned_platforms=["instagram"])
    b = _post(pro, user, planned_platforms=["instagram"])

    auth_client.post(f"{POSTS}auto-schedule/", {}, format="json")

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.proposed_scheduled_at is not None and b.proposed_scheduled_at is not None
    assert a.proposed_scheduled_at != b.proposed_scheduled_at
    assert (b.proposed_scheduled_at - a.proposed_scheduled_at).days % 7 == 0


def test_auto_schedule_only_touches_the_workspaces_own_unplaced_drafts(
    auth_client: Any,
    pro: Any,
    user: Any,
    other_workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signals, "performance", lambda *a, **k: _performance("instagram", 2, 19))
    owner = other_workspace.memberships.first().user
    theirs = _post(other_workspace, owner, planned_platforms=["instagram"])
    placed = _post(pro, user, planned_platforms=["instagram"], proposed_scheduled_at=_at(3))

    body = auth_client.post(f"{POSTS}auto-schedule/", {}, format="json").json()

    assert body["results"] == []
    theirs.refresh_from_db()
    placed.refresh_from_db()
    assert theirs.proposed_scheduled_at is None
    assert placed.proposed_scheduled_at is not None


# --- the Studio hands its drafts to the calendar ---------------------------------
def test_studio_drafts_arrive_marked_generated_with_their_platforms(
    pro: Any, user: Any, make_png_upload: Any
) -> None:
    from ai.models import Generation, GenerationKind, GenerationMode, GenerationVariant
    from ai.services.variants import commit
    from content.services.media import ingest_media

    product = create_product(workspace=pro, name="Olive oil")
    generation = Generation.objects.create(
        workspace=pro,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="x",
        status="SUCCEEDED",
        product=product,
        creative={"platforms": ["instagram", "facebook"]},
    )
    media = ingest_media(workspace=pro, upload=make_png_upload())
    GenerationVariant.objects.create(
        generation=generation,
        kind="IMAGE",
        media_asset=media,
        body="Caption",
        was_selected=True,
    )

    (post,) = commit(generation, actor=user)

    assert post.source == PostSource.AI
    assert post.planned_platforms == ["instagram", "facebook"]
    assert post.product_id == product.pk


def test_listing_posts_does_not_query_a_product_per_row(
    auth_client: Any, pro: Any, user: Any, django_assert_max_num_queries: Any
) -> None:
    """`product_name` reads `post.product`; without `select_related` that is one
    query per post on the calendar's busiest endpoint."""
    oil = create_product(workspace=pro, name="Olive oil")
    for n in range(8):
        _post(pro, user, f"post {n}", product=oil)

    with django_assert_max_num_queries(25) as ctx:
        response = auth_client.get(POSTS)
    assert response.status_code == 200
    assert len(response.json()["results"]) == 8
    assert len([q for q in ctx.captured_queries if 'FROM "products_product"' in q["sql"]]) <= 1
