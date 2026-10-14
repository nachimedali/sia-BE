"""Post templates (P1-09).

A template is a saved starting point, not a live link: applying one **copies**
into a new post. The alternative — a post that keeps pointing at its template —
means editing a template silently rewrites posts that are already scheduled,
and in the worst case ones that are already approved.

The payload is validated on the way *in*, against the same `rules.py`
declaration a real post's options go through. A template that stores an
option Instagram does not have is a template that fails at apply time, which
is the worst moment to find out.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from billing.models import FeatureFlag
from billing.services.flags import CONTENT_MODEL_V2
from content.models import Platform, PostTemplate, TemplateKind
from content.services.media import ingest_media
from content.services.templates import apply_template
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/v1/post-templates/"
APPLY_URL = "/api/v1/post-templates/{pk}/apply/"


@pytest.fixture
def template(workspace: Any, user: Any) -> Any:
    return PostTemplate.objects.create(
        workspace=workspace,
        name="Weekly product spotlight",
        created_by=user,
        payload={
            "master_body": "This week we are looking at #ceramics",
            "media_asset_ids": [],
            "platform_options": {Platform.LINKEDIN: {"visibility": "PUBLIC"}},
        },
    )


# -----------------------------------------------------------------------------
# Applying
# -----------------------------------------------------------------------------
def test_applying_a_template_creates_a_post(workspace: Any, user: Any, template: Any) -> None:
    post = apply_template(template, author=user)

    assert post.workspace == workspace
    assert post.master_body == "This week we are looking at #ceramics"
    assert post.status == "DRAFT"


def test_applying_copies_rather_than_links(workspace: Any, user: Any, template: Any) -> None:
    """Editing a template must not rewrite posts already made from it — least
    of all ones that are already scheduled or approved."""
    post = apply_template(template, author=user)

    template.payload = {**template.payload, "master_body": "Rewritten"}
    template.save(update_fields=["payload"])

    post.refresh_from_db()
    assert post.master_body == "This week we are looking at #ceramics"


def test_applying_carries_media_in_order(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    second = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))
    template = PostTemplate.objects.create(
        workspace=workspace,
        name="Carousel",
        payload={"master_body": "", "media_asset_ids": [second.pk, media_asset.pk]},
    )

    post = apply_template(template, author=user)
    assert [asset.pk for asset in post.ordered_media()] == [second.pk, media_asset.pk]


def test_applying_carries_platform_options_onto_targets(
    workspace: Any, user: Any, template: Any
) -> None:
    post = apply_template(template, author=user)
    target = post.targets.get(platform=Platform.LINKEDIN)
    assert target.platform_options["visibility"] == "PUBLIC"


def test_applying_records_a_first_revision(workspace: Any, user: Any, template: Any) -> None:
    post = apply_template(template, author=user)
    first = post.revisions.order_by("sequence").first()
    assert first is not None
    assert first.is_checkpoint is True


def test_media_that_has_since_been_deleted_is_skipped_not_fatal(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    """A template outlives the media it was saved with. Refusing to apply
    would strand the template; silently dropping the missing asset is the
    behaviour that keeps the rest usable."""
    template = PostTemplate.objects.create(
        workspace=workspace,
        name="Stale",
        payload={"master_body": "still fine", "media_asset_ids": [media_asset.pk, 999999]},
    )

    post = apply_template(template, author=user)
    assert [asset.pk for asset in post.ordered_media()] == [media_asset.pk]


def test_a_template_cannot_smuggle_in_another_workspaces_media(
    workspace: Any, user: Any, make_png_upload: Any
) -> None:
    """The id list is resolved against the template's own workspace, so a
    hand-edited payload reaches nothing."""
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))

    template = PostTemplate.objects.create(
        workspace=workspace, name="Sneaky", payload={"media_asset_ids": [foreign.pk]}
    )
    post = apply_template(template, author=user)
    assert post.ordered_media() == []


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def test_an_undeclared_platform_option_is_rejected_on_save(
    auth_client: Any, workspace: Any
) -> None:
    """Caught on the way in. A template that stores an option the platform
    does not have fails at apply time otherwise — the worst moment to learn
    it."""
    response = auth_client.post(
        TEMPLATES_URL,
        {
            "name": "Broken",
            "payload": {"platform_options": {Platform.INSTAGRAM: {"not_a_real_option": 1}}},
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_platform_options"


def test_an_unknown_platform_key_is_rejected(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(
        TEMPLATES_URL,
        {"name": "Broken", "payload": {"platform_options": {"myspace": {}}}},
        format="json",
    )
    assert response.status_code == 400


def test_a_doc_template_is_not_creatable_yet(auth_client: Any, workspace: Any) -> None:
    """`DOC` is declared because Phase 3 needs the enum stable, and gated
    because Phase 3 is what makes a DOC renderable. The same declare-the-enum,
    gate-what-is-reachable shape `PostStatus` and `GenerationMode` already
    use."""
    response = auth_client.post(
        TEMPLATES_URL, {"name": "A brief", "content_kind": TemplateKind.DOC}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "content_kind_not_available"


def test_two_templates_may_not_share_a_name_in_one_workspace(
    auth_client: Any, workspace: Any, template: Any
) -> None:
    response = auth_client.post(TEMPLATES_URL, {"name": template.name}, format="json")
    assert response.status_code == 400


def test_the_same_name_is_free_in_another_workspace(workspace: Any, template: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    PostTemplate.objects.create(workspace=theirs, name=template.name)  # no IntegrityError


# -----------------------------------------------------------------------------
# The endpoints
# -----------------------------------------------------------------------------
def test_listing_templates_is_scoped_to_the_workspace(
    auth_client: Any, workspace: Any, template: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    PostTemplate.objects.create(workspace=theirs, name="Not yours")

    body = auth_client.get(TEMPLATES_URL).json()
    assert [row["name"] for row in body["results"]] == [template.name]


def test_applying_over_the_api_returns_the_post(
    auth_client: Any, workspace: Any, template: Any
) -> None:
    response = auth_client.post(APPLY_URL.format(pk=template.pk))
    assert response.status_code == 201
    assert response.json()["master_body"] == template.payload["master_body"]


def test_another_workspaces_template_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = PostTemplate.objects.create(workspace=theirs, name="Not yours")

    assert auth_client.get(f"{TEMPLATES_URL}{foreign.pk}/").status_code == 404
    assert auth_client.post(APPLY_URL.format(pk=foreign.pk)).status_code == 404


def test_another_organizations_template_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign = PostTemplate.objects.create(workspace=theirs, name="Not yours")
    assert auth_client.get(f"{TEMPLATES_URL}{foreign.pk}/").status_code == 404


def test_with_the_flag_off_templates_are_404(
    auth_client: Any, workspace: Any, template: Any
) -> None:
    FeatureFlag.objects.create(
        organization=workspace.organization, key=CONTENT_MODEL_V2, enabled=False
    )
    assert auth_client.get(TEMPLATES_URL).status_code == 404


# -----------------------------------------------------------------------------
# Versions and uses (`/app/templates`)
# -----------------------------------------------------------------------------
def test_a_new_template_is_version_one(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(
        TEMPLATES_URL, {"name": "Friday tasting", "payload": {"master_body": "Hi"}}, format="json"
    )
    assert response.status_code == 201
    assert response.json()["version"] == 1
    assert response.json()["use_count"] == 0
    assert response.json()["uses"] == []


def test_editing_a_template_makes_the_next_version(auth_client: Any, template: Any) -> None:
    response = auth_client.patch(
        f"{TEMPLATES_URL}{template.pk}/",
        {"payload": {**template.payload, "master_body": "New text"}},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    renamed = auth_client.patch(
        f"{TEMPLATES_URL}{template.pk}/", {"name": "Spotlight"}, format="json"
    )
    assert renamed.json()["version"] == 3


def test_saving_without_a_change_is_not_a_new_version(auth_client: Any, template: Any) -> None:
    response = auth_client.patch(
        f"{TEMPLATES_URL}{template.pk}/",
        {"name": template.name, "payload": template.payload},
        format="json",
    )
    assert response.json()["version"] == 1


def test_the_version_is_not_client_writable(auth_client: Any, template: Any) -> None:
    response = auth_client.patch(f"{TEMPLATES_URL}{template.pk}/", {"version": 9}, format="json")
    assert response.status_code == 200
    assert response.json()["version"] == 1


def test_a_post_records_the_template_version_it_was_copied_from(
    auth_client: Any, user: Any, template: Any
) -> None:
    first = apply_template(template, author=user)
    auth_client.patch(
        f"{TEMPLATES_URL}{template.pk}/",
        {"payload": {**template.payload, "master_body": "Second wording"}},
        format="json",
    )
    template.refresh_from_db()
    second = apply_template(template, author=user)

    first.refresh_from_db()
    assert (first.template_id, first.template_version) == (template.pk, 1)
    assert (second.template_id, second.template_version) == (template.pk, 2)
    # The copy, not the link: the old post kept its own text.
    assert first.master_body == "This week we are looking at #ceramics"


def test_uses_list_the_newest_posts_with_their_version_and_status(
    auth_client: Any, user: Any, template: Any
) -> None:
    posts = [apply_template(template, author=user) for _ in range(7)]

    body = auth_client.get(f"{TEMPLATES_URL}{template.pk}/").json()
    assert body["use_count"] == 7
    # Capped: a template used every week for a year is not a 52-row card.
    assert [use["post"] for use in body["uses"]] == [p.pk for p in reversed(posts)][:5]
    assert body["uses"][0] == {"post": posts[-1].pk, "version": 1, "status": "DRAFT"}


def test_listing_templates_with_uses_does_not_n_plus_one(
    auth_client: Any,
    workspace: Any,
    user: Any,
    django_assert_max_num_queries: Any,
) -> None:
    for i in range(4):
        made = PostTemplate.objects.create(workspace=workspace, name=f"T{i}", payload={})
        apply_template(made, author=user)
        apply_template(made, author=user)

    with django_assert_max_num_queries(7):
        response = auth_client.get(TEMPLATES_URL)
    assert all(row["use_count"] == 2 for row in response.json()["results"])
