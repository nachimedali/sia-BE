"""S17 — brand avatars: portrait batches, the per-brand cap, downgrades, the
look-alike check, real people with consent, and tenancy.

Gate T2: twenty racing creates against a cap of three make exactly three; a
downgrade keeps the oldest, locks the rest and deletes nothing.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

from billing.models import FeatureFlag
from billing.services import ledger
from billing.services.flags import AD_TEMPLATES_S17
from brand.models import Avatar, AvatarDraft, AvatarKind, DraftStatus
from brand.services import avatars
from common.exceptions import ProviderError
from content.services.media import ingest_media
from workspaces.models import AuditLog
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

LIBRARY = "/api/v1/brand/avatars/"
DRAFTS = f"{LIBRARY}drafts/"
TRAITS = {"archetype": "arch-coach", "note": "short dark hair"}


@pytest.fixture
def catalog(db: None, generation_costs: None) -> None:
    call_command("seed_creative_options", verbosity=0)
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=True)


def on_plan(workspace: Any, plans: dict[str, Any], code: str) -> Any:
    workspace.organization.plan = plans[code]
    workspace.organization.save(update_fields=["plan"])
    return workspace


@pytest.fixture
def growth(workspace: Any, plans: dict[str, Any], catalog: None) -> Any:
    on_plan(workspace, plans, "growth")
    ledger.grant_credits(workspace, 300, note="test")
    return workspace


def ready_draft(client: Any) -> dict[str, Any]:
    response = client.post(DRAFTS, {"traits": TRAITS}, format="json")
    assert response.status_code == 202, response.json()
    body = client.get(f"{DRAFTS}{response.json()['id']}/").json()
    assert body["status"] == DraftStatus.READY, body
    return body


def avatar_in(workspace: Any, upload: Any, name: str, **fields: Any) -> Avatar:
    portrait = ingest_media(workspace=workspace, upload=upload(f"{name}.png", (400, 400)))
    return Avatar.objects.create(
        workspace=workspace,
        name=name,
        kind=fields.pop("kind", AvatarKind.SYNTHETIC),
        portrait=portrait,
        languages=["en"],
        **fields,
    )


# -----------------------------------------------------------------------------
# Drafts
# -----------------------------------------------------------------------------
def test_a_batch_of_four_portraits_is_drawn_and_paid_on_success(
    auth_client: Any, growth: Any
) -> None:
    before = ledger.credit_balance(growth)
    draft = ready_draft(auth_client)

    assert len(draft["portraits"]) == 4
    assert draft["traits"]["age"] == "age-30s"  # the archetype filled its groups
    assert draft["charged"] == draft["credits"] == 12
    assert ledger.credit_balance(growth) == before - 12


def test_the_library_says_what_is_left(auth_client: Any, growth: Any) -> None:
    ready_draft(auth_client)
    body = auth_client.get(LIBRARY).json()
    assert body["limits"] == {
        "max_avatars": 3,
        "used": 0,
        "drafts_monthly": 6,
        "drafts_left": 5,
        "real_avatars": False,
        "suggested_plan": "scale",
        "real_suggested_plan": "scale",
        "draft_credits": 12,
    }
    assert {t["group"] for t in body["traits"]} == {"archetype", "age", "look", "style", "energy"}


def test_a_failed_batch_is_not_charged_and_not_counted(
    auth_client: Any, growth: Any, monkeypatch: Any
) -> None:
    from ai.providers import fake

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("down")

    monkeypatch.setattr(fake._fake_image_provider, "generate", broken)
    before = ledger.credit_balance(growth)
    response = auth_client.post(DRAFTS, {"traits": TRAITS}, format="json")
    assert response.json()["status"] == DraftStatus.FAILED
    assert ledger.credit_balance(growth) == before
    assert avatars.drafts_used(growth.organization) == 0


def test_portrait_batches_run_out_for_the_month_across_the_organization(
    auth_client: Any, growth: Any, user: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Sibling brand")
    for _ in range(6):
        AvatarDraft.objects.create(workspace=sibling, status=DraftStatus.READY)
    response = auth_client.post(
        DRAFTS, {"traits": TRAITS}, format="json", HTTP_X_WORKSPACE_ID=str(growth.pk)
    )
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "avatar_drafts_exhausted"
    # Last month's batches do not count.
    AvatarDraft.objects.update(created_at=timezone.now() - dt.timedelta(days=40))
    ok = auth_client.post(
        DRAFTS, {"traits": TRAITS}, format="json", HTTP_X_WORKSPACE_ID=str(growth.pk)
    )
    assert ok.status_code == 202


def test_a_plan_without_avatars_is_402_with_an_upgrade(
    auth_client: Any, workspace: Any, plans: Any, catalog: None
) -> None:
    on_plan(workspace, plans, "spark")
    response = auth_client.post(DRAFTS, {"traits": TRAITS}, format="json")
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]["suggested_plan"] == "discovery"


def test_an_unknown_trait_is_400(auth_client: Any, growth: Any) -> None:
    response = auth_client.post(DRAFTS, {"traits": {"age": "age-90s"}}, format="json")
    assert response.status_code == 400
    assert auth_client.post(DRAFTS, {"traits": {}}, format="json").status_code == 400


# -----------------------------------------------------------------------------
# Creating
# -----------------------------------------------------------------------------
def test_an_invented_avatar_is_saved_from_a_portrait_and_becomes_the_house_face(
    auth_client: Any, growth: Any
) -> None:
    draft = ready_draft(auth_client)
    response = auth_client.post(
        LIBRARY,
        {"draft": draft["id"], "index": 2, "name": "Rami", "languages": ["fr", "en"]},
        format="json",
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["portrait"]["id"] == draft["portraits"][2]["id"]
    assert body["is_house"] is True  # the first one is the house face
    assert body["state"] == "usable"
    assert len(body["angles"]) == 3  # drawn by the task (eager)


def test_a_draft_still_drawing_is_409(auth_client: Any, growth: Any, user: Any) -> None:
    draft = AvatarDraft.objects.create(workspace=growth, status=DraftStatus.RUNNING)
    response = auth_client.post(LIBRARY, {"draft": draft.pk, "name": "R"}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "avatar_draft_not_ready"


def test_the_cap_is_402_once_full(auth_client: Any, growth: Any, make_png_upload: Any) -> None:
    for name in ("A", "B", "C"):
        avatar_in(growth, make_png_upload, name)
    draft = ready_draft(auth_client)
    response = auth_client.post(LIBRARY, {"draft": draft["id"], "name": "D"}, format="json")
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "avatar_limit_reached"
    assert error["detail"] == {"feature": "max_avatars", "limit": 3, "used": 3}


@pytest.mark.django_db(transaction=True)
def test_twenty_racing_creates_against_a_cap_of_three_make_three(
    plans: Any, user: Any, catalog: None
) -> None:
    workspace = provision_workspace(user, name="Race Co")
    on_plan(workspace, plans, "growth")
    ledger.grant_credits(workspace, 50, note="test")
    draft = avatars.start_draft(workspace, user=user, raw_traits=TRAITS)
    draft.refresh_from_db()
    assert draft.status == DraftStatus.READY
    outcomes: list[str] = []

    def create(index: int) -> None:
        close_old_connections()
        try:
            avatars.create_synthetic(
                workspace, user=user, draft_id=draft.pk, index=index % 4, name=f"N{index}"
            )
            outcomes.append("made")
        except avatars.AvatarLimitError:
            outcomes.append("refused")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=create, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("made") == 3
    assert outcomes.count("refused") == 17
    assert Avatar.objects.filter(workspace=workspace).count() == 3


# -----------------------------------------------------------------------------
# Downgrade
# -----------------------------------------------------------------------------
def test_a_downgrade_keeps_the_oldest_locks_the_rest_and_deletes_nothing(
    auth_client: Any, growth: Any, plans: Any, make_png_upload: Any
) -> None:
    from ai.services.ad_templates import resolve  # noqa: F401  (the render path imports it)

    made = [avatar_in(growth, make_png_upload, name) for name in ("Old", "Mid", "New")]
    on_plan(growth, plans, "essential")  # cap 1

    states = {a["name"]: a["state"] for a in auth_client.get(LIBRARY).json()["avatars"]}
    assert states == {"Old": "usable", "Mid": "over_limit", "New": "over_limit"}
    assert Avatar.objects.filter(workspace=growth).count() == 3

    with pytest.raises(avatars.AvatarOverLimitError) as raised:
        avatars.resolve_for_render(growth, made[2].pk)
    assert raised.value.upgrade["suggested_plan"] == "growth"
    assert avatars.resolve_for_render(growth, made[0].pk) == made[0]


# -----------------------------------------------------------------------------
# Real people
# -----------------------------------------------------------------------------
def real_body(photos: list[int], **overrides: Any) -> dict[str, Any]:
    return {
        "kind": "real",
        "name": "Sami Ben Ali",
        "photos": photos,
        "signed_by": "Sami Ben Ali",
        "signed_on": "2026-09-14",
        "languages": ["fr"],
        **overrides,
    }


@pytest.fixture
def scale(growth: Any, plans: Any) -> Any:
    return on_plan(growth, plans, "scale")


@pytest.fixture
def photos(growth: Any, make_png_upload: Any) -> list[int]:
    return [
        ingest_media(workspace=growth, upload=make_png_upload(f"s{i}.png", (500, 500 + i))).pk
        for i in range(3)
    ]


def test_a_real_person_is_saved_with_consent_and_it_is_audited(
    auth_client: Any, scale: Any, photos: list[int]
) -> None:
    response = auth_client.post(LIBRARY, real_body(photos), format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["kind"] == AvatarKind.REAL and body["state"] == "usable"
    assert body["consent"]["signed_by"] == "Sami Ben Ali"
    assert AuditLog.objects.filter(verb="avatar.consent_recorded").count() == 1


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"signed_by": "", "signed_on": None}, 409, "avatar_consent_required"),
        ({"signed_by": "Someone Else"}, 400, "consent_signer"),
        ({"signed_on": "2099-01-01"}, 400, "validation_error"),
    ],
)
def test_a_real_person_without_proper_consent_is_refused(
    auth_client: Any, scale: Any, photos: list[int], overrides: Any, status: int, code: str
) -> None:
    response = auth_client.post(LIBRARY, real_body(photos, **overrides), format="json")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_a_real_person_below_the_plan_is_402(
    auth_client: Any, growth: Any, photos: list[int]
) -> None:
    response = auth_client.post(LIBRARY, real_body(photos), format="json")
    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]["suggested_plan"] == "scale"


def test_only_an_admin_records_consent(
    client_as: Any,
    contributor_user: Any,
    advanced_workspace: Any,
    plans: Any,
    catalog: None,
    make_png_upload: Any,
) -> None:
    on_plan(advanced_workspace, plans, "scale")
    photo_ids = [
        ingest_media(workspace=advanced_workspace, upload=make_png_upload(f"c{i}.png")).pk
        for i in range(3)
    ]
    response = client_as(contributor_user).post(LIBRARY, real_body(photo_ids), format="json")
    assert response.status_code == 403


def test_withdrawn_consent_removes_the_person_from_future_renders(
    auth_client: Any, scale: Any, photos: list[int]
) -> None:
    avatar_id = auth_client.post(LIBRARY, real_body(photos), format="json").json()["id"]
    revoked = auth_client.post(f"{LIBRARY}{avatar_id}/revoke/")
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "consent_revoked"
    assert AuditLog.objects.filter(verb="avatar.consent_revoked").count() == 1
    with pytest.raises(avatars.ConsentRequiredError):
        avatars.resolve_for_render(scale, avatar_id)


def test_an_invented_avatar_has_no_consent_to_withdraw(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    lina = avatar_in(growth, make_png_upload, "Lina")
    assert auth_client.post(f"{LIBRARY}{lina.pk}/revoke/").status_code == 409


def test_a_photo_of_another_brand_is_404(
    auth_client: Any, scale: Any, photos: list[int], user: Any, make_png_upload: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Sibling")
    theirs = ingest_media(workspace=sibling, upload=make_png_upload()).pk
    response = auth_client.post(
        LIBRARY,
        real_body([*photos[:2], theirs]),
        format="json",
        HTTP_X_WORKSPACE_ID=str(scale.pk),
    )
    assert response.status_code == 404


# -----------------------------------------------------------------------------
# House face, archive, language, tenancy, flag
# -----------------------------------------------------------------------------
def test_the_house_face_is_pinned_first_and_archive_hides_without_deleting(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    first = avatar_in(growth, make_png_upload, "First")
    second = avatar_in(growth, make_png_upload, "Second")
    assert auth_client.post(f"{LIBRARY}{second.pk}/house/").json()["is_house"] is True
    assert [a["name"] for a in auth_client.get(LIBRARY).json()["avatars"]] == ["Second", "First"]

    assert auth_client.post(f"{LIBRARY}{second.pk}/archive/").status_code == 200
    assert [a["name"] for a in auth_client.get(LIBRARY).json()["avatars"]] == ["First"]
    assert Avatar.objects.filter(pk=second.pk).exists()
    with pytest.raises(Exception) as raised:
        avatars.resolve_for_render(growth, second.pk)
    assert getattr(raised.value, "status_code", None) == 404
    first.refresh_from_db()


def test_an_avatar_that_does_not_speak_the_language_is_400(
    growth: Any, make_png_upload: Any
) -> None:
    lina = avatar_in(growth, make_png_upload, "Lina")
    with pytest.raises(avatars.AvatarRefusedError) as raised:
        avatars.require_language(lina, "it")
    assert raised.value.code == "language"


def test_another_organizations_and_another_brands_avatars_are_404(
    auth_client: Any, growth: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    stranger = provision_workspace(other_user, name="Stranger")
    sibling = provision_extra_workspace(user=user, name="Sibling")
    here = {"HTTP_X_WORKSPACE_ID": str(growth.pk)}
    for workspace in (stranger, sibling):
        theirs = avatar_in(workspace, make_png_upload, "Theirs")
        draft = AvatarDraft.objects.create(workspace=workspace, status=DraftStatus.READY)
        for path in ("house/", "archive/", "revoke/"):
            assert auth_client.post(f"{LIBRARY}{theirs.pk}/{path}", **here).status_code == 404
        assert auth_client.get(f"{DRAFTS}{draft.pk}/", **here).status_code == 404
        assert (
            auth_client.post(
                LIBRARY, {"draft": draft.pk, "name": "x"}, format="json", **here
            ).status_code
            == 404
        )
        assert theirs.pk not in [
            a["id"] for a in auth_client.get(LIBRARY, **here).json()["avatars"]
        ]


def test_flag_off_the_avatar_routes_do_not_exist(
    auth_client: Any, workspace: Any, plans: Any
) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=False)
    on_plan(workspace, plans, "growth")
    assert auth_client.get(LIBRARY).status_code == 404
    assert auth_client.get(f"{LIBRARY}limits/").status_code == 404
    assert auth_client.post(DRAFTS, {"traits": TRAITS}, format="json").status_code == 404


def test_the_limits_alone_say_used_out_of_max_without_the_library(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    avatar_in(growth, make_png_upload, "Lina")
    body = auth_client.get(f"{LIBRARY}limits/").json()
    assert body["used"] == 1
    assert body["max_avatars"] == auth_client.get(LIBRARY).json()["limits"]["max_avatars"]


def test_a_viewer_reads_the_library_but_cannot_draw(
    client_as: Any, viewer_user: Any, advanced_workspace: Any, catalog: None
) -> None:
    viewer = client_as(viewer_user)
    assert viewer.get(LIBRARY).status_code == 200
    assert viewer.post(DRAFTS, {"traits": TRAITS}, format="json").status_code == 403
