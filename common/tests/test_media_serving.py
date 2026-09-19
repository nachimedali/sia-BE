"""Filesystem-fake media must be reachable at the URL the API hands out.

Under USE_FAKE_STORAGE, `file.url` is a bare `/media/...` path. Nothing served
it, so every saved reference image rendered as a broken <img> the moment the
form's local blob preview was replaced by the stored asset.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


@pytest.mark.django_db
def test_stored_media_is_served_at_its_url(client) -> None:
    name = default_storage.save("workspaces/1/media/probe.jpeg", ContentFile(b"\xff\xd8probe"))

    response = client.get(default_storage.url(name))

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"\xff\xd8probe"


@pytest.mark.django_db
def test_media_route_does_not_escape_media_root(client) -> None:
    response = client.get("/media/../config/settings/base.py")

    # Django's safe_join refuses it as a SuspiciousFileOperation (400).
    assert response.status_code == 400
