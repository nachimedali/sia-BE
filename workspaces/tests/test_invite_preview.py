"""The invite page, checked as it opens (P6).

`GET /invites/{token}/` says who invited you, to which workspace and with which
role, before anything is accepted — so nobody joins the wrong brand by clicking
an old email. A dead link says why it is dead (expired, already used, not
valid) so the reader knows to ask for a new one; that tells nothing to anyone
who does not already hold the 256-bit token.

Accepting creates the account (with the name and a password the validators
accept) or, for an address that already has one, asks for that account's
password: holding the email is not the same as holding the account.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from conftest import PASSWORD
from workspaces.models import Invitation, Membership, Role

pytestmark = pytest.mark.django_db


def _preview(raw: str) -> Any:
    return APIClient().get(f"/api/v1/invites/{raw}/")


def _accept(raw: str, body: dict[str, Any]) -> Any:
    return APIClient().post(f"/api/v1/invites/{raw}/accept/", body, format="json")


@pytest.fixture
def invite(workspace: Any, user: Any) -> tuple[Invitation, str]:
    user.first_name, user.last_name = "Sarra", "Ben Salah"
    user.save(update_fields=["first_name", "last_name"])
    return Invitation.issue(
        workspace=workspace, email="yassine@example.com", role=Role.EDITOR, invited_by=user
    )


def test_the_page_names_the_inviter_the_workspace_and_the_role_before_anything_is_accepted(
    invite: tuple[Invitation, str], workspace: Any
) -> None:
    _invitation, raw = invite

    response = _preview(raw)

    assert response.status_code == 200
    body = response.json()
    assert body["inviter"] == "Sarra Ben Salah"
    assert body["workspace_name"] == workspace.name
    assert body["role"] == Role.EDITOR
    assert body["role_label"] == "Editor"
    assert body["email"] == "yassine@example.com"
    assert body["account_exists"] is False
    assert not Membership.objects.filter(
        workspace=workspace, user__email="yassine@example.com"
    ).exists()


def test_an_inviter_with_no_name_is_named_by_address(workspace: Any, other_user: Any) -> None:
    _invitation, raw = Invitation.issue(
        workspace=workspace, email="x@example.com", role=Role.VIEWER, invited_by=other_user
    )

    assert _preview(raw).json()["inviter"] == other_user.email


def test_an_invite_for_an_existing_account_says_so(workspace: Any, other_user: Any) -> None:
    _invitation, raw = Invitation.issue(
        workspace=workspace, email=other_user.email, role=Role.VIEWER
    )

    assert _preview(raw).json()["account_exists"] is True


@pytest.mark.parametrize(
    ("make_dead", "status", "code"),
    [
        (
            lambda inv: Invitation.objects.filter(pk=inv.pk).update(
                expires_at=timezone.now() - dt.timedelta(minutes=1)
            ),
            410,
            "expired",
        ),
        (
            lambda inv: Invitation.objects.filter(pk=inv.pk).update(accepted_at=timezone.now()),
            410,
            "used",
        ),
        (
            lambda inv: Invitation.objects.filter(pk=inv.pk).update(revoked_at=timezone.now()),
            404,
            "invalid_token",
        ),
    ],
)
def test_a_dead_link_says_why(
    invite: tuple[Invitation, str], make_dead: Any, status: int, code: str
) -> None:
    invitation, raw = invite
    make_dead(invitation)

    response = _preview(raw)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_a_token_that_never_existed_is_404() -> None:
    response = _preview("not-a-real-token")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_token"


def test_a_new_account_gives_its_name_and_a_password_the_validators_accept(
    invite: tuple[Invitation, str],
) -> None:
    _invitation, raw = invite

    weak = _accept(raw, {"name": "Yassine", "password": "short"})
    assert weak.status_code == 400
    assert "password" in str(weak.json()["error"])

    accepted = _accept(raw, {"name": "Yassine", "password": "a-long-password-1"})
    assert accepted.status_code == 201
    created = get_user_model().objects.get(email="yassine@example.com")
    assert created.first_name == "Yassine"
    assert accepted.json()["user_email"] == "yassine@example.com"
    assert accepted.json()["workspace"] == _invitation.workspace_id


def test_an_existing_account_must_sign_in_to_join(workspace: Any, other_user: Any) -> None:
    _invitation, raw = Invitation.issue(
        workspace=workspace, email=other_user.email, role=Role.EDITOR
    )

    wrong = _accept(raw, {"password": "not-their-password"})
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "wrong_password"
    assert not Membership.objects.filter(workspace=workspace, user=other_user).exists()

    assert _accept(raw, {"password": PASSWORD}).status_code == 201
    assert Membership.objects.filter(workspace=workspace, user=other_user).exists()


def test_the_invite_endpoints_are_throttled(invite: tuple[Invitation, str], settings: Any) -> None:
    from workspaces.views import InvitationAcceptView, InvitationPreviewView

    assert InvitationPreviewView.throttle_classes
    assert InvitationAcceptView.throttle_classes


def test_joining_signs_the_person_in(invite: tuple[Invitation, str]) -> None:
    """P6: the accept answers with a session pair, as registration does, so
    the BFF sets the cookies without replaying the password into a login."""
    _invitation, raw = invite

    body = _accept(raw, {"name": "Yassine", "password": "a-long-password-1"}).json()

    assert body["user_email"] == "yassine@example.com"
    session = APIClient()
    session.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert session.get("/api/v1/auth/me/").json()["email"] == "yassine@example.com"
    assert (
        APIClient().post("/api/v1/auth/refresh/", {"refresh": body["refresh"]}).status_code == 200
    )
