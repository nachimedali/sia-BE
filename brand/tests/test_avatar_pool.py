# ruff: noqa: F811 — the avatar suite's fixtures are imported, then named as parameters
"""S17 — avatar portraits as one 2x2 grid, and the pool of unused faces
(owner, 2026-10-27).

A batch is **one** image call: a 2x2 grid of four different people, split
into four portraits to pick one from. A batch drawn only from trait chips (no
free-text note) is shareable: picking from it closes it and releases the other
three faces to a pool, and one never picked is released after seven days. The
next organization asking for the same traits is served four pooled faces with
no model call and no credits — still one of its monthly batches — each face a
copy in its own files, and each face served once.
"""

from __future__ import annotations

import datetime as dt
import io
import threading
from typing import Any

import pytest
from django.db import close_old_connections
from django.utils import timezone
from PIL import Image

from ai.providers.fake import _fake_image_provider
from billing.services import ledger
from brand.models import AvatarDraft, DraftStatus, PooledPortrait
from brand.services import avatars
from brand.tests.test_avatars import DRAFTS, LIBRARY, catalog, growth, on_plan  # noqa: F401
from content.models import MediaAsset
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PLAIN = {"archetype": "arch-coach"}  # chips only: shareable
NOTED = {"archetype": "arch-coach", "note": "short dark hair"}  # private


@pytest.fixture(autouse=True)
def calls() -> Any:
    _fake_image_provider.clear()
    yield _fake_image_provider.calls
    _fake_image_provider.clear()


def draft_for(client: Any, traits: dict[str, Any], **headers: Any) -> dict[str, Any]:
    response = client.post(DRAFTS, {"traits": traits}, format="json", **headers)
    assert response.status_code == 202, response.json()
    return client.get(f"{DRAFTS}{response.json()['id']}/", **headers).json()


def pick(client: Any, draft: dict[str, Any], index: int = 0, **headers: Any) -> Any:
    return client.post(
        LIBRARY,
        {"draft": draft["id"], "index": index, "name": f"P{index}"},
        format="json",
        **headers,
    )


def other_org(owner: Any, plans: Any, name: str = "Stranger Co") -> Any:
    brand = provision_workspace(owner, name=name)
    on_plan(brand, plans, "growth")
    ledger.grant_credits(brand, 300, note="test")
    return brand


def sweep_after_a_week(draft_id: int) -> None:
    AvatarDraft.objects.filter(pk=draft_id).update(created_at=timezone.now() - dt.timedelta(days=8))
    avatars.release_stale_drafts()


def size_of(media_id: int) -> tuple[int, int]:
    asset = MediaAsset.objects.get(pk=media_id)
    with asset.file.open("rb") as handle:
        return Image.open(io.BytesIO(handle.read())).size


# -----------------------------------------------------------------------------
# One call, four faces
# -----------------------------------------------------------------------------
def test_a_batch_is_one_grid_call_split_into_four_portraits(
    auth_client: Any, growth: Any, calls: list[dict[str, Any]]
) -> None:
    draft = draft_for(auth_client, PLAIN)
    assert draft["status"] == DraftStatus.READY
    [call] = [c for c in calls if "2x2 grid" in c["prompt"]]
    assert call["n"] == 1 and call["aspect"] == "1:1"
    assert "four different people" in call["prompt"]
    ids = [portrait["id"] for portrait in draft["portraits"]]
    assert len(set(ids)) == 4
    grid_w = 256  # the fake draws 1:1 at 256
    for media in ids:
        width, height = size_of(media)
        assert width == height and width < grid_w // 2  # a quadrant, trimmed at the seams
    assert draft["reused"] is False and draft["charged"] == 12


def test_the_grid_splits_in_reading_order_trimmed_at_the_seams() -> None:
    colours = [(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0)]
    grid = Image.new("RGB", (400, 400))
    for (left, top), colour in zip(((0, 0), (200, 0), (0, 200), (200, 200)), colours, strict=True):
        grid.paste(colour, (left, top, left + 200, top + 200))
    buffer = io.BytesIO()
    grid.save(buffer, "PNG")

    faces = [Image.open(io.BytesIO(face)) for face in avatars.split_grid(buffer.getvalue())]

    assert [face.size for face in faces] == [(188, 188)] * 4  # 3% off each side
    for face, colour in zip(faces, colours, strict=True):
        assert face.getcolors() == [(188 * 188, colour)]  # no neighbour bleeds in


def test_the_portrait_route_asks_for_2k_so_each_face_stays_sharp() -> None:
    from ai.route_seed import ROUTES

    [route] = [row for row in ROUTES if row["task"] == "avatar_portrait"]
    assert route["params"]["image_size"] == "2K"


# -----------------------------------------------------------------------------
# Releasing faces
# -----------------------------------------------------------------------------
def test_picking_from_a_plain_batch_closes_it_and_releases_the_other_three(
    auth_client: Any, growth: Any
) -> None:
    draft = draft_for(auth_client, PLAIN)
    assert pick(auth_client, draft, 1).status_code == 201
    assert AvatarDraft.objects.get(pk=draft["id"]).status == DraftStatus.USED
    released = set(PooledPortrait.objects.values_list("source_id", flat=True))
    assert released == {p["id"] for i, p in enumerate(draft["portraits"]) if i != 1}
    again = pick(auth_client, draft, 2)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "avatar_draft_not_ready"


def test_a_batch_with_a_note_stays_private(auth_client: Any, growth: Any) -> None:
    draft = draft_for(auth_client, NOTED)
    assert pick(auth_client, draft, 0).status_code == 201
    assert pick(auth_client, draft, 1).status_code == 201  # still the creator's to pick from
    assert not PooledPortrait.objects.exists()


def test_a_batch_never_picked_is_released_after_seven_days(auth_client: Any, growth: Any) -> None:
    old = draft_for(auth_client, PLAIN)
    recent = draft_for(auth_client, PLAIN)
    private = draft_for(auth_client, NOTED)
    eight_days = timezone.now() - dt.timedelta(days=8)
    AvatarDraft.objects.filter(pk__in=[old["id"], private["id"]]).update(created_at=eight_days)

    assert avatars.release_stale_drafts() == 4

    assert AvatarDraft.objects.get(pk=old["id"]).status == DraftStatus.USED
    assert AvatarDraft.objects.get(pk=recent["id"]).status == DraftStatus.READY
    assert AvatarDraft.objects.get(pk=private["id"]).status == DraftStatus.READY
    assert PooledPortrait.objects.count() == 4
    assert avatars.release_stale_drafts() == 0  # once


def test_the_release_sweep_is_scheduled_on_a_named_queue() -> None:
    from django.conf import settings

    from config.celery import app

    tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    assert "brand.tasks.release_stale_avatar_drafts" in tasks
    route = app.amqp.router.route({}, "brand.tasks.release_stale_avatar_drafts")
    assert route["queue"].name == "ai_q"


# -----------------------------------------------------------------------------
# Serving from the pool
# -----------------------------------------------------------------------------
def released_set(client: Any, workspace: Any) -> None:
    """Four faces in the pool: a batch never picked, swept."""
    sweep_after_a_week(draft_for(client, PLAIN, HTTP_X_WORKSPACE_ID=str(workspace.pk))["id"])


def test_another_organization_with_the_same_traits_is_served_without_a_call(
    auth_client: Any,
    client_as: Any,
    growth: Any,
    other_user: Any,
    plans: Any,
    calls: list[dict[str, Any]],
) -> None:
    released_set(auth_client, growth)
    stranger = other_org(other_user, plans)
    theirs = client_as(other_user)
    calls.clear()
    before = ledger.credit_balance(stranger)

    draft = draft_for(theirs, PLAIN, HTTP_X_WORKSPACE_ID=str(stranger.pk))

    assert draft["status"] == DraftStatus.READY and draft["reused"] is True
    assert calls == []  # no model call
    assert draft["charged"] == 0 and ledger.credit_balance(stranger) == before
    assert avatars.drafts_used(stranger.organization) == 1  # still a batch of the month
    copies = MediaAsset.objects.filter(pk__in=[p["id"] for p in draft["portraits"]])
    assert {asset.workspace_id for asset in copies} == {stranger.pk}  # copies, in their files
    sources = PooledPortrait.objects.filter(claimed_by_id=draft["id"])
    assert sources.count() == 4
    assert not set(sources.values_list("source_id", flat=True)) & {a.pk for a in copies}
    assert pick(theirs, draft, 0, HTTP_X_WORKSPACE_ID=str(stranger.pk)).status_code == 201


def test_each_face_is_served_once(
    auth_client: Any,
    client_as: Any,
    growth: Any,
    other_user: Any,
    plans: Any,
    django_user_model: Any,
    calls: list[dict[str, Any]],
) -> None:
    released_set(auth_client, growth)
    first = other_org(other_user, plans)
    third_user = django_user_model.objects.create_user(email="third@example.com", password="x")
    second = other_org(third_user, plans, name="Third Co")

    assert draft_for(client_as(other_user), PLAIN, HTTP_X_WORKSPACE_ID=str(first.pk))["reused"]
    calls.clear()
    late = draft_for(client_as(third_user), PLAIN, HTTP_X_WORKSPACE_ID=str(second.pk))
    assert late["reused"] is False and len(calls) == 1  # the pool was empty: a new grid


def test_the_organization_that_released_faces_is_never_served_them(
    auth_client: Any, growth: Any, user: Any, calls: list[dict[str, Any]]
) -> None:
    released_set(auth_client, growth)
    sibling = provision_extra_workspace(user=user, name="Second brand")
    ledger.grant_credits(sibling, 50, note="test")
    calls.clear()
    draft = draft_for(auth_client, PLAIN, HTTP_X_WORKSPACE_ID=str(sibling.pk))
    assert draft["reused"] is False and len(calls) == 1


def test_fewer_than_four_pooled_faces_means_a_new_grid(
    auth_client: Any,
    growth: Any,
    other_user: Any,
    plans: Any,
    client_as: Any,
    calls: list[dict[str, Any]],
) -> None:
    draft = draft_for(auth_client, PLAIN)
    pick(auth_client, draft, 0)  # three released
    stranger = other_org(other_user, plans)
    calls.clear()
    theirs = draft_for(client_as(other_user), PLAIN, HTTP_X_WORKSPACE_ID=str(stranger.pk))
    assert theirs["reused"] is False and len(calls) == 1
    assert PooledPortrait.objects.filter(claimed_by__isnull=True).count() == 3


def test_a_batch_with_a_note_is_never_served_from_the_pool(
    auth_client: Any, growth: Any, other_user: Any, plans: Any, client_as: Any
) -> None:
    released_set(auth_client, growth)
    stranger = other_org(other_user, plans)
    draft = draft_for(client_as(other_user), NOTED, HTTP_X_WORKSPACE_ID=str(stranger.pk))
    assert draft["reused"] is False


def test_a_pooled_batch_still_needs_a_batch_left_this_month(
    auth_client: Any, growth: Any, other_user: Any, plans: Any, client_as: Any
) -> None:
    released_set(auth_client, growth)
    stranger = other_org(other_user, plans)
    limit = stranger.organization.plan.features["avatar_drafts_monthly"]
    for _ in range(limit):
        AvatarDraft.objects.create(workspace=stranger, status=DraftStatus.READY)
    response = client_as(other_user).post(
        DRAFTS, {"traits": PLAIN}, format="json", HTTP_X_WORKSPACE_ID=str(stranger.pk)
    )
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "avatar_drafts_exhausted"
    assert PooledPortrait.objects.filter(claimed_by__isnull=True).count() == 4


@pytest.mark.django_db(transaction=True)
def test_two_organizations_racing_for_one_set_get_it_once(
    plans: Any, user: Any, other_user: Any, catalog: None, django_user_model: Any
) -> None:
    source = other_org(user, plans, name="Source Co")
    sweep_after_a_week(avatars.start_draft(source, user=user, raw_traits=PLAIN).pk)

    racer = django_user_model.objects.create_user(email="r@example.com", password="x")
    owners = [other_user, racer]
    racers = [(other_org(owner, plans, name=f"Racer {i}"), owner) for i, owner in enumerate(owners)]
    reused: list[bool] = []

    def ask(brand: Any, owner: Any) -> None:
        close_old_connections()
        try:
            made = avatars.start_draft(brand, user=owner, raw_traits=PLAIN)
            made.refresh_from_db()
            reused.append(made.reused)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=ask, args=racer) for racer in racers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(reused) == [False, True]
    assert PooledPortrait.objects.filter(claimed_by__isnull=False).count() == 4
