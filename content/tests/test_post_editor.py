"""The post editor's surface (`/app/posts/{id}`, `new_temp/post.html`).

What the page needs that the earlier panels did not:

* **one save, one revision** — platforms, media, alt text and the master body
  staged together and written as a single version of the post;
* **unschedule** — the slot cleared, the approval kept;
* **cancel** — a terminal `CANCELLED`, archived with its history, never
  publishable;
* **duplicate** — "reproduce as a new post", a fresh draft from the same brief;
* **the state at a revision**, so the page can compare and restore;
* **regenerate / apply-generation** — a new generation from the post's own
  settings, then its result applied to this post as one revision;
* **the platform switch is honoured by publishing**: a post planned for some
  platforms builds targets only for those.

Every endpoint 404s across organizations and across workspaces (rule 3).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from ai.models import CreativeOption, Generation, GenerationKind, GenerationMode, GenerationStatus
from ai.services.pipeline import create_generation, run_generation
from billing.services.ledger import grant_credits
from channels.models import SocialAccount
from content.models import Post, PostRevision, PostStatus, PostTarget
from content.services.media import ingest_media
from content.services.posts import create_post, set_alt_text, set_platform_options, update_post
from products.services.products import attach_reference_images, create_product
from reminders.models import Reminder, ReminderState
from scheduling.publishing import build_targets
from workspaces.models import AuditLog
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

POSTS = "/api/v1/posts/"


def _url(post: Post, action: str = "") -> str:
    return f"{POSTS}{post.pk}/{action + '/' if action else ''}"


def _soon(minutes: int = 120) -> dt.datetime:
    return timezone.now() + dt.timedelta(minutes=minutes)


def _revisions(post: Post) -> int:
    return PostRevision.objects.filter(post=post).count()


@pytest.fixture
def post(paid_workspace: Any, user: Any, make_png_upload: Any) -> Post:
    a = ingest_media(workspace=paid_workspace, upload=make_png_upload("a.png", (600, 600)))
    b = ingest_media(workspace=paid_workspace, upload=make_png_upload("b.png", (640, 480)))
    return create_post(
        workspace=paid_workspace,
        author=user,
        master_body="Pressed the same day.",
        media_assets=[a, b],
    )


@pytest.fixture
def stranger_post(db: None) -> Post:
    owner = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    elsewhere = provision_workspace(owner, name="Elsewhere")
    return create_post(workspace=elsewhere, author=owner, master_body="not yours")


@pytest.fixture
def sibling_post(user: Any, paid_workspace: Any, plans: Any) -> Post:
    paid_workspace.organization.plan = plans["advanced"]
    paid_workspace.organization.save(update_fields=["plan"])
    sibling = provision_extra_workspace(user=user, name="Sister Brand")
    return create_post(workspace=sibling, author=user, master_body="the sister brand's")


def _scheduled(post: Post, client: Any, mode: str = "REMINDER") -> Post:
    response = client.post(
        _url(post, "schedule"),
        {"delivery_mode": mode, "scheduled_at": _soon().isoformat()},
        format="json",
    )
    assert response.status_code == 200, response.json()
    post.refresh_from_db()
    return post


# --- tenancy ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "action", "body"),
    [
        ("post", "edit", {"master_body": "x"}),
        ("post", "unschedule", {}),
        ("post", "cancel", {"reason": "No longer relevant"}),
        ("post", "duplicate", {}),
        ("get", "revisions/1", None),
        ("post", "regenerate", {}),
        ("post", "apply-generation", {"generation": 1}),
    ],
)
def test_every_editor_endpoint_is_404_across_organizations_and_workspaces(
    auth_client: Any,
    paid_workspace: Any,
    stranger_post: Post,
    sibling_post: Post,
    method: str,
    action: str,
    body: Any,
) -> None:
    for theirs in (stranger_post, sibling_post):
        call = getattr(auth_client, method)
        kwargs: dict[str, Any] = {"HTTP_X_WORKSPACE_ID": str(paid_workspace.id)}
        if body is not None:
            kwargs.update(data=body, format="json")
        response = call(_url(theirs, action), **kwargs)
        assert response.status_code == 404, (action, theirs.workspace_id, response.content)


# --- the serializer carries what the editor reads ---------------------------------
def test_a_post_exposes_its_generation_and_its_stored_platform_options(
    auth_client: Any, post: Post, user: Any
) -> None:
    set_platform_options(
        post, platform="instagram", options={"first_comment": "#tags"}, author=user
    )

    body = auth_client.get(_url(post)).json()

    assert body["generation"] is None
    # Stored as validated: the declared defaults travel with what was set.
    assert body["platform_options"]["instagram"]["first_comment"] == "#tags"


# --- edit: one save, one revision ---------------------------------------------------
def test_an_edit_across_body_media_alt_platforms_and_options_is_one_revision(
    auth_client: Any, post: Post
) -> None:
    a, b = list(post.media_assets.order_by("id"))
    before = _revisions(post)

    response = auth_client.post(
        _url(post, "edit"),
        {
            "master_body": "Picked in November, pressed within hours.",
            "media": [
                {"media_asset": b.pk, "alt_text": "The bottle on a terrace."},
                {"media_asset": a.pk, "alt_text": ""},
            ],
            "planned_platforms": ["instagram", "facebook"],
            "platform_options": {"instagram": {"first_comment": "#OliveOil"}},
        },
        format="json",
    )

    assert response.status_code == 200, response.json()
    assert _revisions(post) == before + 1
    body = response.json()
    assert body["master_body"] == "Picked in November, pressed within hours."
    assert [m["id"] for m in body["media"]] == [b.pk, a.pk]
    assert body["media"][0]["alt_text"] == "The bottle on a terrace."
    assert body["planned_platforms"] == ["instagram", "facebook"]
    assert body["platform_options"]["instagram"]["first_comment"] == "#OliveOil"


def test_an_edit_that_changes_nothing_writes_no_revision(auth_client: Any, post: Post) -> None:
    before = _revisions(post)

    response = auth_client.post(
        _url(post, "edit"), {"master_body": post.master_body}, format="json"
    )

    assert response.status_code == 200
    assert _revisions(post) == before


def test_an_edit_is_all_or_nothing(auth_client: Any, post: Post) -> None:
    """A bad platform option refuses the whole save — the body it came with is
    not half-written."""
    response = auth_client.post(
        _url(post, "edit"),
        {
            "master_body": "Should not land.",
            "platform_options": {"instagram": {"no_such_option": True}},
        },
        format="json",
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_platform_options"
    assert "instagram" in error["detail"]
    post.refresh_from_db()
    assert post.master_body == "Pressed the same day."


def test_an_edit_refuses_media_from_another_workspace(
    auth_client: Any, post: Post, stranger_post: Post, make_png_upload: Any
) -> None:
    theirs = ingest_media(workspace=stranger_post.workspace, upload=make_png_upload())

    response = auth_client.post(
        _url(post, "edit"), {"media": [{"media_asset": theirs.pk, "alt_text": ""}]}, format="json"
    )

    assert response.status_code == 400
    assert post.media_assets.filter(pk=theirs.pk).exists() is False


def test_an_unknown_planned_platform_is_a_400(auth_client: Any, post: Post) -> None:
    response = auth_client.post(
        _url(post, "edit"), {"planned_platforms": ["myspace"]}, format="json"
    )
    assert response.status_code == 400


def test_editing_the_content_of_an_approved_post_sends_it_back_for_review(
    auth_client: Any, post: Post
) -> None:
    Post.objects.filter(pk=post.pk).update(status=PostStatus.APPROVED)

    response = auth_client.post(_url(post, "edit"), {"master_body": "Changed."}, format="json")

    assert response.json()["status"] == PostStatus.PENDING_REVIEW


def test_a_locked_post_refuses_an_edit(auth_client: Any, post: Post) -> None:
    Post.objects.filter(pk=post.pk).update(locked_at=timezone.now())

    response = auth_client.post(_url(post, "edit"), {"master_body": "x"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "post_locked"


@pytest.mark.parametrize(
    "status", [PostStatus.PUBLISHED, PostStatus.PUBLISHING, PostStatus.CANCELLED]
)
def test_a_post_that_has_gone_or_was_cancelled_refuses_an_edit(
    auth_client: Any, post: Post, status: str
) -> None:
    Post.objects.filter(pk=post.pk).update(status=status)

    response = auth_client.post(_url(post, "edit"), {"master_body": "x"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "post_not_editable"


# --- revision state -------------------------------------------------------------------
def test_the_state_at_a_revision_is_what_the_post_said_then(
    auth_client: Any, post: Post, user: Any
) -> None:
    update_post(post, author=user, master_body="Second take.")

    first = auth_client.get(_url(post, "revisions/1")).json()
    second = auth_client.get(_url(post, "revisions/2")).json()

    assert first["sequence"] == 1
    assert first["state"]["master_body"] == "Pressed the same day."
    assert second["state"]["master_body"] == "Second take."
    assert len(first["state"]["media"]) == 2


def test_a_revision_the_post_does_not_have_is_404(auth_client: Any, post: Post) -> None:
    response = auth_client.get(_url(post, "revisions/99"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "revision_not_found"


# --- unschedule ---------------------------------------------------------------------
def test_unscheduling_a_reminder_clears_the_slot_and_keeps_the_approval(
    auth_client: Any, post: Post
) -> None:
    post = _scheduled(post, auth_client, "REMINDER")

    response = auth_client.post(_url(post, "unschedule"), {}, format="json")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == PostStatus.APPROVED
    assert body["scheduled_at"] is None
    assert body["delivery_mode"] == ""
    reminder = Reminder.objects.get(post=post)
    assert reminder.state == ReminderState.SKIPPED


def test_unscheduling_an_auto_publish_post_removes_its_pending_targets_only(
    auth_client: Any, post: Post, user: Any, social_account: Any
) -> None:
    set_platform_options(post, platform="instagram", options={"first_comment": "#x"}, author=user)
    post = _scheduled(post, auth_client, "AUTO_PUBLISH")
    assert PostTarget.objects.filter(post=post, social_account__isnull=False).exists()

    response = auth_client.post(_url(post, "unschedule"), {}, format="json")

    assert response.status_code == 200
    assert not PostTarget.objects.filter(post=post, social_account__isnull=False).exists()
    # The composer's settings are not the schedule's, and survive it.
    kept = PostTarget.objects.get(post=post, social_account__isnull=True).platform_options
    assert kept["first_comment"] == "#x"


def test_unscheduling_a_post_with_no_slot_is_a_409(auth_client: Any, post: Post) -> None:
    response = auth_client.post(_url(post, "unschedule"), {}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_scheduled"


# --- cancel ---------------------------------------------------------------------------
def test_cancelling_archives_the_post_with_its_reason_and_clears_any_slot(
    auth_client: Any, post: Post
) -> None:
    post = _scheduled(post, auth_client, "REMINDER")

    response = auth_client.post(
        _url(post, "cancel"), {"reason": "Replaced by another post"}, format="json"
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == PostStatus.CANCELLED
    assert body["scheduled_at"] is None
    assert Reminder.objects.get(post=post).state == ReminderState.SKIPPED
    entry = AuditLog.objects.get(verb="post.cancelled")
    assert entry.meta == {"post": post.pk, "reason": "Replaced by another post"}
    # History is kept: nothing about the post itself was deleted.
    assert _revisions(post) >= 1
    assert post.media_assets.count() == 2


def test_a_cancelled_post_cannot_be_scheduled_or_submitted(auth_client: Any, post: Post) -> None:
    auth_client.post(_url(post, "cancel"), {"reason": "Other"}, format="json")

    scheduled = auth_client.post(
        _url(post, "schedule"),
        {"delivery_mode": "REMINDER", "scheduled_at": _soon().isoformat()},
        format="json",
    )
    submitted = auth_client.post(_url(post, "submit"), {}, format="json")

    assert scheduled.status_code == 409
    assert submitted.status_code == 409


@pytest.mark.parametrize(
    "status", [PostStatus.PUBLISHED, PostStatus.PUBLISHING, PostStatus.CANCELLED]
)
def test_a_post_that_has_gone_or_is_cancelled_cannot_be_cancelled(
    auth_client: Any, post: Post, status: str
) -> None:
    Post.objects.filter(pk=post.pk).update(status=status)

    response = auth_client.post(_url(post, "cancel"), {"reason": "Other"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_cancellable"


def test_cancelling_needs_a_reason(auth_client: Any, post: Post) -> None:
    response = auth_client.post(_url(post, "cancel"), {}, format="json")
    assert response.status_code == 400


# --- duplicate ------------------------------------------------------------------------
def test_duplicating_makes_a_draft_from_the_same_brief_and_media(
    auth_client: Any, post: Post, user: Any
) -> None:
    a = post.media_assets.order_by("id").first()
    assert a is not None
    set_alt_text(post, media_asset=a, alt_text="Described once.", author=user)
    set_platform_options(post, platform="instagram", options={"first_comment": "#x"}, author=user)
    update_post(post, author=user, planned_platforms=["instagram"])
    post = _scheduled(post, auth_client, "REMINDER")

    response = auth_client.post(_url(post, "duplicate"), {}, format="json")

    assert response.status_code == 201, response.json()
    copy = response.json()
    assert copy["id"] != post.pk
    assert copy["status"] == PostStatus.DRAFT
    assert copy["scheduled_at"] is None
    assert copy["origin_post"] == post.pk
    assert copy["master_body"] == post.master_body
    assert [m["id"] for m in copy["media"]] == [
        m["id"] for m in auth_client.get(_url(post)).json()["media"]
    ]
    assert copy["media"][0]["alt_text"] == "Described once."
    assert copy["planned_platforms"] == ["instagram"]
    assert copy["platform_options"] == auth_client.get(_url(post)).json()["platform_options"]
    assert copy["platform_options"]["instagram"]["first_comment"] == "#x"


def test_a_cancelled_post_can_still_be_reproduced(auth_client: Any, post: Post) -> None:
    auth_client.post(_url(post, "cancel"), {"reason": "Other"}, format="json")

    response = auth_client.post(_url(post, "duplicate"), {}, format="json")

    assert response.status_code == 201
    assert response.json()["status"] == PostStatus.DRAFT


# --- the platform switch reaches publishing ---------------------------------------------
def test_building_targets_honours_the_planned_platforms(paid_workspace: Any, user: Any) -> None:
    for platform in ("instagram", "facebook"):
        SocialAccount.objects.create(
            workspace=paid_workspace, platform=platform, provider_account_id=f"acct-{platform}"
        )
    planned = create_post(workspace=paid_workspace, author=user, master_body="Instagram only")
    update_post(planned, author=user, planned_platforms=["instagram"])
    unplanned = create_post(workspace=paid_workspace, author=user, master_body="Everywhere")

    assert {t.platform for t in build_targets(planned)} == {"instagram"}
    assert {t.platform for t in build_targets(unplanned)} == {"instagram", "facebook"}


# --- regenerate / apply-generation --------------------------------------------------------
@pytest.fixture
def generated(paid_workspace: Any, user: Any, make_png_upload: Any, generation_costs: None) -> Post:
    grant_credits(paid_workspace, 200, note="test funding")
    product = create_product(workspace=paid_workspace, name="Olive oil")
    attach_reference_images(product=product, uploads=[make_png_upload()])
    generation = run_generation(
        create_generation(
            workspace=paid_workspace,
            user=user,
            kind=GenerationKind.IMAGE,
            mode=GenerationMode.PRODUCT,
            prompt="the new season",
            product=product,
        ),
        n=1,
    )
    assert generation.status == GenerationStatus.SUCCEEDED
    variant = generation.variants.order_by("rank").first()
    assert variant is not None
    post = create_post(
        workspace=paid_workspace,
        author=user,
        master_body=variant.body or "From the Studio.",
        media_assets=[variant.media_asset] if variant.media_asset else [],
    )
    update_post(post, author=user, reason="studio", product=product, generation=generation)
    return post


@pytest.fixture
def reason_row(db: None) -> CreativeOption:
    return CreativeOption.objects.create(
        kind="revise_reason",
        key="product-too-small",
        label="Product too small",
        prompt_fragment="make the product larger in the frame",
    )


def test_regenerating_starts_a_generation_from_the_posts_own_settings(
    auth_client: Any, generated: Post, reason_row: CreativeOption
) -> None:
    parent = generated.generation
    assert parent is not None

    response = auth_client.post(
        _url(generated, "regenerate"),
        {"reasons": ["product-too-small"], "note": "warmer light"},
        format="json",
    )

    assert response.status_code == 201, response.json()
    child = Generation.objects.get(pk=response.json()["id"])
    assert child.product_id == parent.product_id
    assert child.kind == parent.kind
    assert child.mode == parent.mode
    assert "make the product larger in the frame" in child.prompt
    assert "warmer light" in child.prompt
    # Nothing about the post moves until the result is applied.
    generated.refresh_from_db()
    assert generated.generation_id == parent.pk


def test_an_unknown_reason_is_a_400_not_ignored(auth_client: Any, generated: Post) -> None:
    response = auth_client.post(
        _url(generated, "regenerate"), {"reasons": ["no-such-reason"]}, format="json"
    )
    assert response.status_code == 400


def test_a_post_that_was_not_generated_cannot_be_regenerated(auth_client: Any, post: Post) -> None:
    response = auth_client.post(_url(post, "regenerate"), {}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_generated"


def test_applying_a_generation_replaces_body_and_media_as_one_revision(
    auth_client: Any, generated: Post
) -> None:
    child = Generation.objects.get(
        pk=auth_client.post(_url(generated, "regenerate"), {}, format="json").json()["id"]
    )
    Post.objects.filter(pk=generated.pk).update(status=PostStatus.APPROVED)
    before = _revisions(generated)

    response = auth_client.post(
        _url(generated, "apply-generation"), {"generation": child.pk}, format="json"
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["generation"] == child.pk
    top = child.variants.order_by("rank").first()
    assert top is not None
    assert [m["id"] for m in body["media"]] == [
        v.media_asset_id for v in child.variants.order_by("rank") if v.media_asset_id
    ]
    assert body["status"] == PostStatus.PENDING_REVIEW  # approvals reset
    assert _revisions(generated) == before + 1


def test_applying_a_generation_that_has_not_finished_is_a_409(
    auth_client: Any, generated: Post, user: Any
) -> None:
    pending = create_generation(
        workspace=generated.workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="not run",
    )

    response = auth_client.post(
        _url(generated, "apply-generation"), {"generation": pending.pk}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "generation_not_ready"


def test_another_workspaces_generation_cannot_be_applied(
    auth_client: Any, generated: Post, stranger_post: Post
) -> None:
    theirs = Generation.objects.create(
        workspace=stranger_post.workspace,
        user=stranger_post.author,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="not yours",
        status=GenerationStatus.SUCCEEDED,
    )

    response = auth_client.post(
        _url(generated, "apply-generation"), {"generation": theirs.pk}, format="json"
    )

    assert response.status_code == 400
    generated.refresh_from_db()
    assert generated.generation_id != theirs.pk


def test_a_scheduled_post_is_not_rewritten_by_a_generation(
    auth_client: Any, generated: Post
) -> None:
    child = auth_client.post(_url(generated, "regenerate"), {}, format="json").json()["id"]
    _scheduled(generated, auth_client, "REMINDER")

    response = auth_client.post(
        _url(generated, "apply-generation"), {"generation": child}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "post_scheduled"
