"""`/workspaces/profile/` — the brand profile Settings edits."""

from __future__ import annotations

from typing import Any

import pytest

from workspaces.models import AuditLog, Membership, Permission, Role
from workspaces.services.provisioning import provision_extra_workspace

pytestmark = pytest.mark.django_db

PROFILE = "/api/v1/workspaces/profile/"


def test_any_member_reads_the_profile(auth_client: Any, workspace: Any) -> None:
    body = auth_client.get(PROFILE).json()

    assert body["id"] == workspace.pk
    assert body["name"] == "Acme Studio"
    assert set(body) >= {
        "website",
        "description",
        "timezone",
        "regions",
        "platforms",
        "brand_voice_default",
    }


def test_an_admin_updates_it_and_the_change_is_audited(auth_client: Any, workspace: Any) -> None:
    response = auth_client.patch(
        PROFILE,
        {
            "description": "Hand-glazed ceramics.",
            "timezone": "Africa/Tunis",
            "regions": ["TN", " TN ", ""],
        },
        format="json",
    )

    assert response.status_code == 200, response.content
    workspace.refresh_from_db()
    assert workspace.description == "Hand-glazed ceramics."
    assert workspace.timezone == "Africa/Tunis"
    assert workspace.regions == ["TN"]
    entry = AuditLog.objects.filter(workspace=workspace, verb="workspace.profile_updated").get()
    assert entry.meta == {"fields": ["description", "regions", "timezone"]}


def test_an_unchanged_save_writes_no_audit_entry(auth_client: Any, workspace: Any) -> None:
    auth_client.patch(PROFILE, {"name": "Acme Studio"}, format="json")

    assert not AuditLog.objects.filter(verb="workspace.profile_updated").exists()


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"name": "  "}, "name"),
        ({"regions": "TN"}, "regions"),
        ({"brand_voice_default": "LOUD"}, "brand_voice_default"),
        ({"website": "not a url"}, "website"),
    ],
)
def test_invalid_values_are_400_by_field(
    auth_client: Any, workspace: Any, payload: dict[str, Any], field: str
) -> None:
    response = auth_client.patch(PROFILE, payload, format="json")

    assert response.status_code == 400
    assert field in response.json()["error"]["detail"]["fields"]


def test_a_member_without_admin_cannot_change_it(
    client_as: Any, workspace: Any, other_user: Any
) -> None:
    Membership.objects.create(
        workspace=workspace,
        user=other_user,
        role=Role.EDITOR,
        permissions=[Permission.VIEW, Permission.EDIT],
    )
    client = client_as(other_user)

    assert client.get(PROFILE).status_code == 200
    response = client.patch(PROFILE, {"description": "x"}, format="json")
    assert response.status_code == 403


def test_the_header_picks_which_workspace_is_edited(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")

    auth_client.patch(
        PROFILE,
        {"description": "Sibling only."},
        format="json",
        HTTP_X_WORKSPACE_ID=str(sibling.pk),
    )

    sibling.refresh_from_db()
    workspace.refresh_from_db()
    assert sibling.description == "Sibling only."
    assert workspace.description != "Sibling only."
