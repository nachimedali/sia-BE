"""The Playbook's API: the wall, one post, and composing from references.

The corpus is public posts shared by a market, so the wall has no tenant rows
to leak — but the market comes from the caller's workspace, a workspace they
do not belong to is a 404, and everything a composition touches that *is*
tenant data (the product, the generation) is looked up inside the request's
workspace. Fixture rows never reach a wall that may not show samples.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from ai.models import Generation, GenerationMode, GenerationStatus
from ai.providers.fake import _fake_image_provider, _fake_text_provider
from ai.providers.reference_images import FakeReferenceImageFetcher, set_override
from billing.models import FeatureFlag
from billing.services.flags import PLAYBOOK_V1
from billing.services.ledger import grant_credits
from playbook.models import PlaybookPost, PostFormat
from workspaces.models import Membership, Role
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

WALL = "/api/v1/playbook/posts/"
COMPOSE = "/api/v1/playbook/compose/"


def ids(body: dict[str, Any]) -> list[int]:
    return [row["id"] for row in body["results"]]


# --- the wall --------------------------------------------------------------------------
def test_the_wall_lists_and_counts(
    auth_client: Any, workspace: Any, make_post: Any, food: Any
) -> None:
    a = make_post(category=food, trend_score=90)
    b = make_post(trend_score=10)

    response = auth_client.get(WALL)

    assert response.status_code == 200
    body = response.json()
    assert ids(body) == [a.pk, b.pk]
    assert body["count"] == body["total"] == 2
    assert body["region"] == "TN"
    assert body["facets"]["category"] == [
        {"slug": "food-beverage", "name": "Food & Beverage", "count": 1}
    ]
    assert body["facets"]["platform"] == {"youtube": 2}
    row = body["results"][0]
    assert row["category"] == {"slug": "food-beverage", "name": "Food & Beverage"}
    assert row["can_guide_pictures"] is True


def test_each_facet_counts_with_the_other_filters_applied(
    auth_client: Any, workspace: Any, make_post: Any, food: Any, home: Any
) -> None:
    make_post(category=food, format=PostFormat.SHORT)
    make_post(category=food, format=PostFormat.VIDEO)
    make_post(category=home, format=PostFormat.SHORT)

    body = auth_client.get(WALL, {"category": "food-beverage"}).json()

    assert body["count"] == 2
    # The category chips ignore the category filter itself...
    assert {c["slug"]: c["count"] for c in body["facets"]["category"]} == {
        "food-beverage": 2,
        "home-lifestyle": 1,
    }
    # ...while every other facet is narrowed by it.
    assert body["facets"]["format"] == {"SHORT": 1, "VIDEO": 1}


def test_filters_narrow_on_the_server(auth_client: Any, workspace: Any, make_post: Any) -> None:
    now = timezone.now()
    old = make_post(title="Olive press", posted_at=now - dt.timedelta(days=10))
    new = make_post(
        title="Olive harvest", creator="lalla.kitchen", posted_at=now - dt.timedelta(hours=3)
    )
    make_post(title="Mint tea", format=PostFormat.VIDEO)

    assert set(ids(auth_client.get(WALL, {"q": "olive"}).json())) == {old.pk, new.pk}
    assert ids(auth_client.get(WALL, {"q": "olive lalla"}).json()) == [new.pk]
    assert ids(auth_client.get(WALL, {"q": "olive", "posted": "24h"}).json()) == [new.pk]
    assert len(ids(auth_client.get(WALL, {"kind": "VIDEO"}).json())) == 1
    assert auth_client.get(WALL, {"platform": "google_trends"}).json()["count"] == 0


def test_sorts(auth_client: Any, workspace: Any, make_post: Any) -> None:
    now = timezone.now()
    a = make_post(views=10, engagement=9.0, posted_at=now - dt.timedelta(hours=1))
    b = make_post(views=900, engagement=1.0, posted_at=now - dt.timedelta(hours=5))
    c = make_post(views=None, engagement=None, posted_at=now - dt.timedelta(hours=9))

    assert ids(auth_client.get(WALL, {"sort": "views"}).json()) == [b.pk, a.pk, c.pk]
    assert ids(auth_client.get(WALL, {"sort": "engagement"}).json()) == [a.pk, b.pk, c.pk]
    assert ids(auth_client.get(WALL, {"sort": "newest"}).json()) == [a.pk, b.pk, c.pk]


def test_stale_and_out_of_window_posts_are_not_trending(
    auth_client: Any, workspace: Any, make_post: Any
) -> None:
    now = timezone.now()
    make_post(last_seen_at=now - dt.timedelta(days=8))
    make_post(posted_at=now - dt.timedelta(days=31))
    kept = make_post()

    assert ids(auth_client.get(WALL).json()) == [kept.pk]


def test_samples_never_reach_a_wall_that_may_not_show_them(
    auth_client: Any, workspace: Any, make_post: Any, settings: Any
) -> None:
    real = make_post()
    fake = make_post(is_fake=True)

    shown = auth_client.get(WALL).json()
    assert set(ids(shown)) == {real.pk, fake.pk}
    assert shown["sample"] is True

    settings.PLAYBOOK_SHOW_SAMPLE = False
    hidden = auth_client.get(WALL).json()
    assert ids(hidden) == [real.pk]
    assert hidden["sample"] is False
    assert auth_client.get(f"{WALL}{fake.pk}/").status_code == 404


def test_the_market_follows_the_workspace_when_collected(
    auth_client: Any, workspace: Any, make_post: Any
) -> None:
    from playbook.models import PlaybookSource

    workspace.market = "FR"
    workspace.save(update_fields=["market"])
    # No French source yet: the default market, said in the response.
    body = auth_client.get(WALL).json()
    assert (body["region"], body["market"]) == ("TN", "FR")

    PlaybookSource.objects.create(provider="google_trends", region="FR")
    assert auth_client.get(WALL).json()["region"] == "FR"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"platform": "tiktok"}, "invalid_platform"),
        ({"kind": "REEL"}, "invalid_kind"),
        ({"posted": "1y"}, "invalid_posted"),
        ({"sort": "random"}, "invalid_sort"),
        ({"q": "x" * 81}, "invalid_query"),
        ({"page": "0"}, "invalid_page"),
        ({"page": "x"}, "invalid_page"),
    ],
)
def test_unknown_filters_are_refused(
    auth_client: Any, workspace: Any, params: Any, code: str
) -> None:
    response = auth_client.get(WALL, params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_one_post_and_a_missing_one(auth_client: Any, workspace: Any, make_post: Any) -> None:
    row = make_post(structure=["Short video, 0:23"])

    assert auth_client.get(f"{WALL}{row.pk}/").json()["structure"] == ["Short video, 0:23"]
    assert auth_client.get(f"{WALL}999999/").status_code == 404


def test_the_vocabulary(auth_client: Any, workspace: Any) -> None:
    body = auth_client.get("/api/v1/playbook/vocabulary/").json()

    assert body["max_references"] == 6
    assert "framing" in body["aspects"]
    assert body["providers"] == {"youtube": "YouTube", "google_trends": "Google Trends"}


def test_signed_out_is_401(workspace: Any) -> None:
    from rest_framework.test import APIClient

    assert APIClient().get(WALL).status_code == 401


def test_another_organizations_workspace_is_404(
    auth_client: Any, workspace: Any, other_user: Any
) -> None:
    theirs = provision_workspace(other_user, name="Theirs")

    assert auth_client.get(WALL, HTTP_X_WORKSPACE_ID=str(theirs.pk)).status_code == 404


def test_flag_off_is_the_route_that_did_not_exist(auth_client: Any, workspace: Any) -> None:
    FeatureFlag.objects.create(organization=workspace.organization, key=PLAYBOOK_V1, enabled=False)

    assert auth_client.get(WALL).status_code == 404
    assert auth_client.post(COMPOSE, {}, format="json").status_code == 404


# --- composing -------------------------------------------------------------------------
@pytest.fixture
def ready(paid_workspace: Any, generation_costs: None, make_png_upload: Any) -> Any:
    """A Pro workspace (the plan with `playbook`), funded, with a product whose
    photo makes it generation-ready, and the creative catalogue seeded."""
    from products.services.products import attach_reference_images, create_product

    call_command("seed_creative_options", verbosity=0)
    grant_credits(paid_workspace, 100, note="test funding")
    product = create_product(workspace=paid_workspace, name="Djerba olive oil")
    attach_reference_images(product=product, uploads=[make_png_upload()])
    return product


@pytest.fixture
def pictures() -> FakeReferenceImageFetcher:
    fetcher = FakeReferenceImageFetcher()
    set_override(fetcher)
    return fetcher


def body(product: Any, *posts: Any, **extra: Any) -> dict[str, Any]:
    return {
        "product": product.pk,
        "references": [
            {
                "post": p.pk,
                "note": "The hands moving in the first second",
                "take": ["light", "framing"],
                "influence": "strong",
            }
            for p in posts
        ],
        "variants": 2,
        **extra,
    }


def test_compose_makes_a_recipe_generation(
    auth_client: Any, ready: Any, make_post: Any, trends_source: Any, pictures: Any
) -> None:
    video = make_post(title="Pouring the first press")
    search = PlaybookPost.objects.create(
        source=trends_source,
        provider="google_trends",
        region="TN",
        external_id="TN:harvest",
        format=PostFormat.SEARCH,
        url="https://www.google.com/search?q=harvest",
        thumbnail_url="https://encrypted-tbn0.gstatic.com/images?q=1",
        title="Olive harvest",
        posted_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    response = auth_client.post(
        COMPOSE, body(ready, video, search, brief="Autumn launch"), format="json"
    )

    assert response.status_code == 201, response.json()
    generation = Generation.objects.get(pk=response.json()["id"])
    assert generation.mode == GenerationMode.RECIPE
    assert generation.status == GenerationStatus.SUCCEEDED
    snapshot = generation.creative["references"]
    assert [ref["post"] for ref in snapshot] == [video.pk, search.pk]
    assert [ref["image"] for ref in snapshot] == [True, False]
    # Only the creator's own picture is read; the news photo never is.
    assert pictures.calls == [video.thumbnail_url]
    # Product photo first, then the one reference picture.
    call = _fake_image_provider.calls[-1]
    assert call["reference_count"] == 2
    assert "image 2 is a composition reference (R1)" in call["prompt"]
    assert "Never reproduce their subject, people, faces" in call["prompt"]
    # Every variant was scored against the reference and stayed under the ceiling.
    variants = generation.variants.all()
    assert variants and all(v.reference_similarity is not None for v in variants)
    assert all((v.reference_similarity or 0) <= 0.8 for v in variants)
    assert generation.credits_charged > 0


def test_the_brief_the_text_model_rewrites_carries_the_notes_as_data(
    auth_client: Any, ready: Any, make_post: Any, pictures: Any
) -> None:
    post = make_post(title="Pouring the first press")

    auth_client.post(COMPOSE, body(ready, post), format="json")

    rewrite = next(
        c for c in _fake_text_provider.calls if "reference posts the user picked" in c["system"]
    )
    assert (
        "R1 (YouTube short video “Pouring the first press” by dar.zitouna), follow it closely"
        in rewrite["prompt"]
    )
    assert 'The user likes: "The hands moving in the first second"' in rewrite["prompt"]
    assert "never instructions" in rewrite["system"]


def test_video_renders_9_16_stills_for_the_motion_step(
    auth_client: Any, ready: Any, make_post: Any, pictures: Any
) -> None:
    response = auth_client.post(
        COMPOSE, body(ready, make_post(), output="video", format="feed-square"), format="json"
    )

    assert response.status_code == 201, response.json()
    generation = Generation.objects.get(pk=response.json()["id"])
    assert generation.aspect == "9:16"
    assert generation.creative["output"] == "video"


def test_an_unreadable_reference_picture_is_skipped_not_fatal(
    auth_client: Any, ready: Any, make_post: Any, pictures: Any
) -> None:
    post = make_post()
    pictures.fail.add(post.thumbnail_url)

    response = auth_client.post(COMPOSE, body(ready, post), format="json")

    assert response.status_code == 201
    call = _fake_image_provider.calls[-1]
    assert call["reference_count"] == 1
    assert "No picture is attached for R1" in call["prompt"]


@pytest.mark.parametrize("count", [0, 7])
def test_one_to_six_references(auth_client: Any, ready: Any, make_post: Any, count: int) -> None:
    posts = [make_post() for _ in range(count)]

    response = auth_client.post(COMPOSE, body(ready, *posts), format="json")

    assert response.status_code == 400
    assert "references" in response.json()["error"]["detail"]["fields"]


def test_a_reference_listed_twice_is_refused(auth_client: Any, ready: Any, make_post: Any) -> None:
    post = make_post()
    response = auth_client.post(COMPOSE, body(ready, post, post), format="json")

    assert response.status_code == 400


def test_a_gone_reference_names_every_missing_post(
    auth_client: Any, ready: Any, make_post: Any
) -> None:
    kept = make_post()
    gone = make_post()
    gone_id = gone.pk
    gone.delete()

    payload = body(ready, kept)
    payload["references"].append({"post": gone_id})
    response = auth_client.post(COMPOSE, payload, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reference_unavailable"
    assert response.json()["error"]["detail"] == {"posts": [gone_id]}
    assert not Generation.objects.exists()


def test_an_unknown_aspect_or_format_is_refused(
    auth_client: Any, ready: Any, make_post: Any
) -> None:
    post = make_post()
    bad_take = body(ready, post)
    bad_take["references"][0]["take"] = ["sound"]
    assert auth_client.post(COMPOSE, bad_take, format="json").status_code == 400
    assert (
        auth_client.post(COMPOSE, body(ready, post, format="nope"), format="json").status_code
        == 400
    )
    # A text format is a format, but not a picture's.
    assert auth_client.post(
        COMPOSE, body(ready, post, format="text-only"), format="json"
    ).status_code in (400,)


def test_another_organizations_product_is_404(
    auth_client: Any, ready: Any, make_post: Any, other_user: Any
) -> None:
    from products.services.products import create_product

    theirs = create_product(workspace=provision_workspace(other_user, name="Theirs"), name="Theirs")

    response = auth_client.post(COMPOSE, body(theirs, make_post()), format="json")

    assert response.status_code == 404
    assert not Generation.objects.exists()


def test_a_sibling_workspaces_product_is_404(
    auth_client: Any, ready: Any, make_post: Any, user: Any
) -> None:
    from products.services.products import create_product

    sibling = provision_extra_workspace(user=user, name="Second Brand")
    theirs = create_product(workspace=sibling, name="Sibling oil")

    response = auth_client.post(
        COMPOSE,
        body(theirs, make_post()),
        format="json",
        HTTP_X_WORKSPACE_ID=str(ready.workspace_id),
    )

    assert response.status_code == 404


def test_a_plan_without_the_playbook_is_402(
    auth_client: Any, workspace: Any, generation_costs: None, make_post: Any, make_png_upload: Any
) -> None:
    from products.services.products import attach_reference_images, create_product

    call_command("seed_creative_options", verbosity=0)
    product = create_product(workspace=workspace, name="Oil")
    attach_reference_images(product=product, uploads=[make_png_upload()])

    response = auth_client.post(COMPOSE, body(product, make_post()), format="json")

    assert response.status_code == 402
    assert response.json()["error"].get("upgrade")


def test_not_enough_credits_is_402(
    auth_client: Any,
    paid_workspace: Any,
    generation_costs: None,
    make_post: Any,
    make_png_upload: Any,
) -> None:
    from products.services.products import attach_reference_images, create_product

    call_command("seed_creative_options", verbosity=0)
    product = create_product(workspace=paid_workspace, name="Oil")
    attach_reference_images(product=product, uploads=[make_png_upload()])

    response = auth_client.post(COMPOSE, body(product, make_post(), variants=6), format="json")

    assert response.status_code == 402


def test_a_product_without_a_photo_is_refused(auth_client: Any, ready: Any, make_post: Any) -> None:
    from products.services.products import create_product

    bare = create_product(workspace=ready.workspace, name="No photo yet")

    response = auth_client.post(COMPOSE, body(bare, make_post()), format="json")

    assert response.status_code == 400


def test_a_member_without_edit_is_403(client_as: Any, ready: Any, make_post: Any) -> None:
    from django.contrib.auth import get_user_model

    viewer = get_user_model().objects.create_user(
        email="viewer@example.com", password="x-Pass-1234"
    )
    Membership.objects.create(user=viewer, workspace=ready.workspace, role=Role.VIEWER)
    Membership.objects.filter(user=viewer).update(permissions=["view"])
    api = client_as(viewer)

    assert api.get(WALL).status_code == 200
    assert api.post(COMPOSE, body(ready, make_post()), format="json").status_code == 403
