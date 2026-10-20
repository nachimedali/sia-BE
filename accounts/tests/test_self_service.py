"""The person's own account (P2): name, language, password, email, a copy of
their data, and deleting the account.

Each change that matters asks for proof first: the current password to set a
new one or to move the email, a typed confirmation to delete. A new email does
not take effect until the link sent to it is clicked. Changing the password
signs out every other session. A data export is built in the background and
emailed. Deletion is refused while it would strand an organization with other
members or a running paid plan; otherwise the person's own details are removed
and their posts stay with the workspace, under a former member.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import DataExport, DataExportState, EmailToken, EmailTokenPurpose
from billing.models import Subscription, SubscriptionStatus
from conftest import PASSWORD
from content.models import Post
from workspaces.models import Membership, Role

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me/"
ACCOUNT = "/api/v1/auth/me/account/"


# -----------------------------------------------------------------------------
# Name and language
# -----------------------------------------------------------------------------
def test_the_account_says_who_you_are_and_what_is_pending(
    auth_client: Any, user: Any, workspace: Any
) -> None:
    body = auth_client.get(ACCOUNT).json()

    assert body["email"] == user.email
    assert body["language"] == "en"
    assert body["pending_email"] is None
    assert body["exports"] == []
    assert body["sole_owner_of"] == []


def test_the_shells_read_stays_lean(auth_client: Any, user: Any) -> None:
    """`/auth/me/` is read on every app page; the account page's extras are not."""
    body = auth_client.get(ME).json()

    assert body["email"] == user.email
    assert {"pending_email", "exports", "sole_owner_of"}.isdisjoint(body)


def test_name_and_language_are_saved(auth_client: Any, user: Any) -> None:
    response = auth_client.patch(
        ME, {"first_name": "Mohamed", "last_name": "Ali", "language": "fr"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert (user.first_name, user.last_name, user.language) == ("Mohamed", "Ali", "fr")


def test_only_the_three_interface_languages_are_accepted(auth_client: Any) -> None:
    """L-6: English, French and Italian. Arabic is not one."""
    assert auth_client.patch(ME, {"language": "ar"}, format="json").status_code == 400


def test_the_account_endpoints_need_a_session() -> None:
    client = APIClient()
    assert client.get(ME).status_code == 401
    assert client.post(f"{ME}password/", {}, format="json").status_code == 401
    assert client.post(f"{ME}export/").status_code == 401
    assert client.delete(ME, {}, format="json").status_code == 401


# -----------------------------------------------------------------------------
# Password
# -----------------------------------------------------------------------------
def test_a_new_password_needs_the_current_one(auth_client: Any) -> None:
    response = auth_client.post(
        f"{ME}password/", {"current": "wrong", "new": "a-new-long-pass-1"}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wrong_password"


def test_a_weak_new_password_is_refused_in_the_validators_words(auth_client: Any) -> None:
    response = auth_client.post(
        f"{ME}password/", {"current": PASSWORD, "new": "123"}, format="json"
    )

    assert response.status_code == 400
    assert "new" in response.json()["error"]["detail"]["fields"]


def test_changing_the_password_signs_out_every_other_session(auth_client: Any, user: Any) -> None:
    elsewhere = RefreshToken.for_user(user)

    response = auth_client.post(
        f"{ME}password/", {"current": PASSWORD, "new": "a-new-long-pass-1"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password("a-new-long-pass-1")
    token = OutstandingToken.objects.get(jti=elsewhere["jti"])
    assert BlacklistedToken.objects.filter(token=token).exists()


def test_the_password_change_answers_with_this_sessions_replacement(
    auth_client: Any, user: Any
) -> None:
    """Every session is signed out, this one too; the answer carries a fresh
    pair so the BFF keeps the person signed in without a second login."""
    body = auth_client.post(
        f"{ME}password/", {"current": PASSWORD, "new": "a-new-long-pass-1"}, format="json"
    ).json()

    assert body["user"]["email"] == user.email
    fresh = OutstandingToken.objects.get(token=body["refresh"])
    assert not BlacklistedToken.objects.filter(token=fresh).exists()


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------
def test_a_new_email_waits_for_the_link_sent_to_it(
    auth_client: Any, user: Any, outbox: list[Any]
) -> None:
    response = auth_client.post(
        f"{ME}email/", {"email": "New@Example.com", "password": PASSWORD}, format="json"
    )

    assert response.status_code == 202
    assert response.json()["pending_email"]["email"] == "new@example.com"
    user.refresh_from_db()
    assert user.email == "jordan@example.com"
    assert outbox[-1].to == "new@example.com"
    link = outbox[-1].context["confirm_url"]
    assert "/confirm-email?token=" in link

    token = link.split("token=")[1]
    confirmed = APIClient().post("/api/v1/auth/email/confirm/", {"token": token}, format="json")

    assert confirmed.status_code == 200
    user.refresh_from_db()
    assert user.email == "new@example.com"
    assert user.pending_email == ""


def test_moving_the_email_needs_the_password(auth_client: Any) -> None:
    response = auth_client.post(
        f"{ME}email/", {"email": "new@example.com", "password": "nope"}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wrong_password"


def test_an_address_another_account_uses_is_refused(auth_client: Any, other_user: Any) -> None:
    response = auth_client.post(
        f"{ME}email/", {"email": other_user.email, "password": PASSWORD}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "email_taken"


def test_a_pending_change_can_be_cancelled_and_its_link_dies(
    auth_client: Any, user: Any, outbox: list[Any]
) -> None:
    auth_client.post(
        f"{ME}email/", {"email": "new@example.com", "password": PASSWORD}, format="json"
    )
    token = outbox[-1].context["confirm_url"].split("token=")[1]

    assert auth_client.delete(f"{ME}email/").status_code == 200

    assert (
        APIClient().post("/api/v1/auth/email/confirm/", {"token": token}, format="json").status_code
        == 400
    )
    user.refresh_from_db()
    assert user.email == "jordan@example.com"
    assert not EmailToken.objects.filter(
        user=user, purpose=EmailTokenPurpose.EMAIL_CHANGE, used_at__isnull=True
    ).exists()


# -----------------------------------------------------------------------------
# A copy of your data
# -----------------------------------------------------------------------------
def test_an_export_is_built_and_emailed(
    auth_client: Any,
    user: Any,
    workspace: Any,
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    Post.objects.create(workspace=workspace, author=user, master_body="Harvest week is here")

    with django_capture_on_commit_callbacks(execute=True):
        response = auth_client.post(f"{ME}export/")

    assert response.status_code == 202
    export = DataExport.objects.get(user=user)
    assert export.state == DataExportState.EMAILED
    assert outbox[-1].to == user.email
    assert outbox[-1].context["download_url"]
    with default_storage.open(str(export.file.name)) as handle:
        data = json.loads(handle.read())
    assert data["profile"]["email"] == user.email
    assert any(post["body"] == "Harvest week is here" for post in data["posts"])
    assert auth_client.get(ACCOUNT).json()["exports"][0]["state"] == "EMAILED"


def test_one_export_at_a_time(auth_client: Any, user: Any) -> None:
    DataExport.objects.create(user=user, state=DataExportState.PREPARING, sent_to=user.email)

    response = auth_client.post(f"{ME}export/")

    assert response.status_code == 409


def test_an_export_holds_nothing_of_another_person(
    auth_client: Any,
    user: Any,
    workspace: Any,
    other_user: Any,
    django_capture_on_commit_callbacks: Any,
) -> None:
    Membership.objects.create(user=other_user, workspace=workspace, role=Role.EDITOR)
    Post.objects.create(workspace=workspace, author=other_user, master_body="Not mine")

    with django_capture_on_commit_callbacks(execute=True):
        auth_client.post(f"{ME}export/")

    export = DataExport.objects.get(user=user)
    with default_storage.open(str(export.file.name)) as handle:
        data = json.loads(handle.read())
    assert "Not mine" not in json.dumps(data)
    assert other_user.email not in json.dumps(data)


# -----------------------------------------------------------------------------
# Deleting the account
# -----------------------------------------------------------------------------
def test_deleting_needs_the_exact_phrase(auth_client: Any, user: Any) -> None:
    response = auth_client.delete(ME, {"confirm": "delete"}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_mismatch"


def test_the_only_owner_of_a_team_cannot_leave_it_ownerless(
    auth_client: Any, user: Any, workspace: Any, other_user: Any
) -> None:
    Membership.objects.create(user=other_user, workspace=workspace, role=Role.EDITOR)

    response = auth_client.delete(ME, {"confirm": f"DELETE {user.email}"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sole_owner"
    assert auth_client.get(ACCOUNT).json()["sole_owner_of"] == [workspace.organization.name]


def test_a_running_paid_plan_must_be_cancelled_first(
    auth_client: Any, user: Any, workspace: Any, plans: Any
) -> None:
    Subscription.objects.create(
        workspace=workspace,
        organization=workspace.organization,
        plan=plans["pro"],
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_live",
    )

    response = auth_client.delete(ME, {"confirm": f"DELETE {user.email}"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_subscription"


def test_deleting_removes_the_person_and_keeps_the_brands_posts(
    auth_client: Any, user: Any, workspace: Any
) -> None:
    post = Post.objects.create(workspace=workspace, author=user, master_body="Harvest week is here")
    email = user.email

    response = auth_client.delete(ME, {"confirm": f"DELETE {email}"}, format="json")

    assert response.status_code == 204
    user.refresh_from_db()
    assert not user.is_active
    assert user.email != email
    assert (user.first_name, user.last_name) == ("Former", "member")
    assert not user.has_usable_password()
    assert not Membership.objects.filter(user=user).exists()
    post.refresh_from_db()
    assert post.author_id == user.pk
    assert not get_user_model().objects.filter(email=email).exists()
    login = APIClient().post(
        "/api/v1/auth/login/", {"email": email, "password": PASSWORD}, format="json"
    )
    assert login.status_code == 401
