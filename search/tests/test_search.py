"""Workspace search (`GET /search/`, closes A-14's dead top-bar link).

What it must never do is show something the list pages would not: every source
is scoped to the request's workspace (and, for posts, its audience), so another
organization's or a sibling brand's rows cannot appear, and a workspace id the
caller does not belong to is a 404, never a 403.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from common.visibility import Audience, Visibility
from content.models import Post
from content.services.posts import create_post
from planning.models import Campaign
from products.models import Product
from search import services
from workspaces.models import Membership, Role
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/search/"


def post(workspace: Any, user: Any, body: str, **fields: Any) -> Post:
    made = create_post(workspace=workspace, author=user, master_body=body, **fields)
    return made


def product(workspace: Any, name: str, description: str = "") -> Product:
    return Product.objects.create(workspace=workspace, name=name, description=description)


def campaign(workspace: Any, user: Any, name: str) -> Campaign:
    now = timezone.now()
    return Campaign.objects.create(
        workspace=workspace,
        name=name,
        starts_at=now,
        ends_at=now + dt.timedelta(days=14),
        created_by=user,
    )


def age(row: Any, days: int) -> None:
    """`updated_at` is `auto_now`, so ageing a row goes around `save()`."""
    type(row).objects.filter(pk=row.pk).update(updated_at=timezone.now() - dt.timedelta(days=days))


def ids(body: dict[str, Any], group: str) -> list[int]:
    return [hit["id"] for hit in body["results"].get(group, [])]


# --- happy path ----------------------------------------------------------------------------
def test_results_are_grouped_and_counted(auth_client: Any, workspace: Any, user: Any) -> None:
    shown = post(workspace, user, "Harvest glaze, fresh from the kiln.")
    post(workspace, user, "Nothing to see.")
    mug = product(workspace, "Harvest mug", "Stoneware.")
    autumn = campaign(workspace, user, "Harvest week")

    response = auth_client.get(SEARCH, {"q": "harvest"})

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"all": 3, "posts": 1, "products": 1, "campaigns": 1}
    assert ids(body, "posts") == [shown.pk]
    assert ids(body, "products") == [mug.pk]
    assert ids(body, "campaigns") == [autumn.pk]
    hit = body["results"]["posts"][0]
    assert hit["type"] == "post"
    assert hit["title"] == "Harvest glaze, fresh from the kiln."
    assert hit["status"] == "DRAFT"
    assert hit["date_label"] == "Edited"


def test_every_word_must_match_but_any_field_may_hold_it(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    mug = product(workspace, "Aurora mug")
    both = post(workspace, user, "Terracotta, back in stock.")
    Post.objects.filter(pk=both.pk).update(product=mug)
    post(workspace, user, "Terracotta only.")

    body = auth_client.get(SEARCH, {"q": "terracotta aurora"}).json()

    # "terracotta" is in the caption, "aurora" in the post's product name.
    assert ids(body, "posts") == [both.pk]


def test_matching_ignores_case(auth_client: Any, workspace: Any, user: Any) -> None:
    shown = post(workspace, user, "LINEN season")

    assert ids(auth_client.get(SEARCH, {"q": "linen"}).json(), "posts") == [shown.pk]


def test_the_all_tab_previews_and_a_tab_pages(auth_client: Any, workspace: Any, user: Any) -> None:
    for n in range(services.PAGE_SIZE + 2):
        post(workspace, user, f"Glaze study {n}")

    everything = auth_client.get(SEARCH, {"q": "glaze"}).json()
    assert everything["counts"]["posts"] == services.PAGE_SIZE + 2
    assert len(everything["results"]["posts"]) == services.ALL_PREVIEW

    first = auth_client.get(SEARCH, {"q": "glaze", "type": "posts"}).json()
    assert set(first["results"]) == {"posts"}
    assert len(first["results"]["posts"]) == services.PAGE_SIZE

    second = auth_client.get(SEARCH, {"q": "glaze", "type": "posts", "page": 2}).json()
    # Cumulative, so "Show more" replaces the list rather than stitching pages.
    assert len(second["results"]["posts"]) == services.PAGE_SIZE + 2
    assert second["counts"]["posts"] == services.PAGE_SIZE + 2


def test_newest_first(auth_client: Any, workspace: Any, user: Any) -> None:
    old = post(workspace, user, "Kiln notes, old")
    new = post(workspace, user, "Kiln notes, new")
    age(old, 3)

    assert ids(auth_client.get(SEARCH, {"q": "kiln"}).json(), "posts") == [new.pk, old.pk]


# --- the history window --------------------------------------------------------------------
def test_older_matches_are_counted_never_returned(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    recent = post(workspace, user, "Olive harvest")
    old = post(workspace, user, "Olive harvest, last year")
    age(old, 30)  # outside the trial's 7 days
    stale = campaign(workspace, user, "Olive season")
    age(stale, 30)
    shelf = product(workspace, "Olive bowl")
    age(shelf, 400)  # products are not time-bound

    body = auth_client.get(SEARCH, {"q": "olive"}).json()

    assert ids(body, "posts") == [recent.pk]
    assert ids(body, "campaigns") == []
    assert ids(body, "products") == [shelf.pk]
    history = body["history"]
    assert history["days"] == 7
    assert history["older_matches"] == 2
    # Spark (the default plan) → Discovery, the cheapest public plan that looks
    # further back.
    assert history["next_plan"] == {"name": "Discovery", "days": 30}


def test_a_longer_plan_searches_further_back(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    old = post(paid_workspace, user, "Olive harvest, last quarter")
    age(old, 60)

    body = auth_client.get(SEARCH, {"q": "olive"}).json()

    assert ids(body, "posts") == [old.pk]
    assert body["history"] == {
        "plan": "Pro",
        "days": 90,
        "older_matches": 0,
        # Pro is retired from the public list; the next public plan that looks
        # further back than 90 days is Growth.
        "next_plan": {"name": "Growth", "days": 365},
    }


def test_the_longest_plan_names_no_next_plan(
    auth_client: Any, advanced_workspace: Any, user: Any
) -> None:
    body = auth_client.get(SEARCH, {"q": "olive"}).json()

    assert body["history"]["next_plan"] is None


# --- tenancy: organization, workspace, visibility -------------------------------------------
def test_another_organizations_rows_never_appear(
    auth_client: Any, workspace: Any, user: Any, other_user: Any
) -> None:
    theirs = provision_workspace(other_user, name="Theirs")
    post(theirs, other_user, "Saffron launch")
    product(theirs, "Saffron jar")
    campaign(theirs, other_user, "Saffron week")

    body = auth_client.get(SEARCH, {"q": "saffron"}).json()

    assert body["counts"]["all"] == 0
    assert body["history"]["older_matches"] == 0


def test_naming_another_organizations_workspace_is_404(
    auth_client: Any, workspace: Any, other_user: Any
) -> None:
    theirs = provision_workspace(other_user, name="Theirs")

    response = auth_client.get(SEARCH, {"q": "saffron"}, HTTP_X_WORKSPACE_ID=str(theirs.pk))

    assert response.status_code == 404


def test_a_sibling_workspace_is_searched_only_when_named(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    here = post(workspace, user, "Cedar candle")
    there = post(sibling, user, "Cedar candle, second brand")

    home = auth_client.get(SEARCH, {"q": "cedar"}, HTTP_X_WORKSPACE_ID=str(workspace.pk)).json()
    away = auth_client.get(SEARCH, {"q": "cedar"}, HTTP_X_WORKSPACE_ID=str(sibling.pk)).json()

    assert ids(home, "posts") == [here.pk]
    assert ids(away, "posts") == [there.pk]


def test_posts_are_narrowed_to_the_audience(workspace: Any, user: Any) -> None:
    internal = post(workspace, user, "Quince draft")
    shared = post(workspace, user, "Quince for the client")
    Post.objects.filter(pk=internal.pk).update(visibility=Visibility.INTERNAL)
    Post.objects.filter(pk=shared.pk).update(visibility=Visibility.SHARED)

    staff = services.search(workspace=workspace, audience=Audience.STAFF, q="quince")
    client = services.search(workspace=workspace, audience=Audience.CLIENT, q="quince")

    assert {hit["id"] for hit in staff["results"]["posts"]} == {internal.pk, shared.pk}
    assert [hit["id"] for hit in client["results"]["posts"]] == [shared.pk]
    assert client["counts"]["posts"] == 1


# --- every error the endpoint emits ---------------------------------------------------------
@pytest.mark.parametrize("q", ["", " ", "a", " a "])
def test_a_query_under_two_characters_is_400(auth_client: Any, workspace: Any, q: str) -> None:
    response = auth_client.get(SEARCH, {"q": q})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"


def test_a_query_over_the_limit_is_400(auth_client: Any, workspace: Any) -> None:
    response = auth_client.get(SEARCH, {"q": "x" * (services.MAX_QUERY + 1)})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"


def test_an_unknown_type_is_400(auth_client: Any, workspace: Any) -> None:
    response = auth_client.get(SEARCH, {"q": "glaze", "type": "people"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_type"


@pytest.mark.parametrize("page", ["0", "-1", "two", str(services.MAX_PAGE + 1)])
def test_a_bad_page_is_400(auth_client: Any, workspace: Any, page: str) -> None:
    response = auth_client.get(SEARCH, {"q": "glaze", "type": "posts", "page": page})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_page"


def test_signed_out_is_401(workspace: Any) -> None:
    from rest_framework.test import APIClient

    assert APIClient().get(SEARCH, {"q": "glaze"}).status_code == 401


def test_a_member_without_view_is_403(client_as: Any, workspace: Any) -> None:
    from django.contrib.auth import get_user_model

    nobody = get_user_model().objects.create_user(
        email="nobody@example.com", password="x-Pass-1234"
    )
    Membership.objects.create(user=nobody, workspace=workspace, role=Role.VIEWER)
    # Non-empty on purpose: an empty list is read as "derive from the role".
    Membership.objects.filter(user=nobody).update(permissions=["comment"])

    response = client_as(nobody).get(SEARCH, {"q": "glaze"})

    assert response.status_code == 403
