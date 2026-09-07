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
