"""S21-01 — Studio projects (`studio.html`, owner 2026-11-25).

A project is the Studio's saved work: its title, whether it makes a video or a
still, and the editor's state as the page wrote it (scenes, overlays, kit,
history). The server keeps it, scopes it and bounds it; it never interprets
the state — every price and lock is checked again when the state is rendered.
Per workspace: the whole brand team sees its projects, newest first.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.models import StudioProject
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PROJECTS = "/api/v1/ai/studio-projects/"


def code_of(response: Any) -> str:
    return str(response.json()["error"]["code"])


def make(client: Any, **over: Any) -> dict[str, Any]:
    body = {"title": "Olive oil · summer", "kind": "video", "state": {"template": "pour-shot"}}
    response = client.post(PROJECTS, {**body, **over}, format="json")
    assert response.status_code == 201, response.json()
    return dict(response.json())


def test_a_project_is_created_read_updated_and_deleted(auth_client: Any, workspace: Any) -> None:
    made = make(auth_client)
    assert made["title"] == "Olive oil · summer"
    assert made["kind"] == "video"
    assert made["state"] == {"template": "pour-shot"}
    assert made["created_by"]

    one = auth_client.get(f"{PROJECTS}{made['id']}/")
    assert one.status_code == 200
    assert one.json()["state"] == {"template": "pour-shot"}

    patched = auth_client.patch(
        f"{PROJECTS}{made['id']}/",
        {"title": "Renamed", "state": {"template": "slow-unbox", "scenes": [1, 2]}},
        format="json",
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"
    assert StudioProject.objects.get(pk=made["id"]).state["template"] == "slow-unbox"

    assert auth_client.delete(f"{PROJECTS}{made['id']}/").status_code == 204
    assert not StudioProject.objects.filter(pk=made["id"]).exists()


def test_the_list_is_newest_first_and_light(auth_client: Any, workspace: Any) -> None:
    first = make(auth_client, title="First")
    second = make(auth_client, title="Second")
    auth_client.patch(f"{PROJECTS}{first['id']}/", {"title": "First, edited"}, format="json")

    rows = auth_client.get(PROJECTS).json()["results"]
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    # The list carries what a card shows, not the whole editor state.
    assert "state" not in rows[0]
    assert {"id", "title", "kind", "summary", "updated_at"} <= set(rows[0])


def test_the_summary_is_what_the_page_wrote(auth_client: Any, workspace: Any) -> None:
    made = make(auth_client, state={"summary": {"status": "rendered", "poster": "x"}, "scenes": []})
    row = auth_client.get(PROJECTS).json()["results"][0]
    assert row["id"] == made["id"]
    assert row["summary"] == {"status": "rendered", "poster": "x"}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"kind": "reel"}, "kind"),
        ({"title": ""}, "title"),
        ({"title": "x" * 121}, "title"),
        ({"state": ["not", "an", "object"]}, "state"),
    ],
)
def test_a_bad_project_is_400(auth_client: Any, workspace: Any, body: Any, field: str) -> None:
    payload = {"title": "Ad", "kind": "video", "state": {}, **body}
    response = auth_client.post(PROJECTS, payload, format="json")
    assert response.status_code == 400
    assert field in str(response.json())


def test_a_state_past_the_size_limit_is_400(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(
        PROJECTS,
        {"title": "Big", "kind": "image", "state": {"blob": "x" * 300_000}},
        format="json",
    )
    assert response.status_code == 400
    assert code_of(response) == "project_too_large"


def test_another_organizations_and_another_brands_project_are_404(
    auth_client: Any, workspace: Any, user: Any, other_user: Any
) -> None:
    stranger = provision_workspace(other_user, name="Stranger Co")
    theirs = StudioProject.objects.create(
        workspace=stranger, created_by=other_user, title="Theirs", kind="video", state={}
    )
    sibling = provision_extra_workspace(user=user, name="Second brand")
    sibling_project = StudioProject.objects.create(
        workspace=sibling, created_by=user, title="Sibling", kind="video", state={}
    )
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}

    for project in (theirs, sibling_project):
        url = f"{PROJECTS}{project.pk}/"
        assert auth_client.get(url, **here).status_code == 404
        assert auth_client.patch(url, {"title": "Mine"}, format="json", **here).status_code == 404
        assert auth_client.delete(url, **here).status_code == 404
    listed = [row["id"] for row in auth_client.get(PROJECTS, **here).json()["results"]]
    assert theirs.pk not in listed and sibling_project.pk not in listed


def test_a_viewer_reads_but_cannot_write(
    client_as: Any, viewer_user: Any, advanced_workspace: Any, user: Any
) -> None:
    project = StudioProject.objects.create(
        workspace=advanced_workspace, created_by=user, title="Team ad", kind="video", state={}
    )
    viewer = client_as(viewer_user)
    assert viewer.get(PROJECTS).status_code == 200
    assert viewer.get(f"{PROJECTS}{project.pk}/").status_code == 200
    body = {"title": "Mine", "kind": "video", "state": {}}
    assert viewer.post(PROJECTS, body, format="json").status_code == 403
    assert (
        viewer.patch(f"{PROJECTS}{project.pk}/", {"title": "x"}, format="json").status_code == 403
    )
    assert viewer.delete(f"{PROJECTS}{project.pk}/").status_code == 403
