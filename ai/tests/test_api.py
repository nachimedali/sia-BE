"""AI endpoints (design.md §7).

`CELERY_TASK_ALWAYS_EAGER=True` in `config/settings/test.py` means
`run_generation_task.delay(...)` runs inline here — these tests exercise the
real pipeline through the HTTP layer, not a mocked task queue.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.models import GenerationStatus

pytestmark = pytest.mark.django_db

GENERATE_URL = "/api/v1/ai/generate/"
GENERATIONS_URL = "/api/v1/ai/generations/"
VOICE_PROFILES_URL = "/api/v1/ai/voice-profiles/"


def test_generate_creates_and_runs_a_generation(
    auth_client: Any, workspace: Any, generation_costs: Any
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {"kind": "TEXT", "mode": "IDEA", "prompt": "a cosy morning", "n": 2},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == GenerationStatus.SUCCEEDED
    assert body["credits_charged"] == 1
    assert len(body["variants"]) == 2


def test_generate_with_a_product_injects_restrictions(
    auth_client: Any, workspace: Any, product_with_reference_image: Any, generation_costs: Any
) -> None:
    product_with_reference_image.restrictions = ["always show the handle"]
    product_with_reference_image.save(update_fields=["restrictions"])

    response = auth_client.post(
        GENERATE_URL,
        {
            "kind": "IMAGE",
            "mode": "PRODUCT",
            "prompt": "on a sunlit table",
            "product": product_with_reference_image.id,
            "n": 1,
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["status"] == GenerationStatus.SUCCEEDED


def test_generate_rejects_a_product_from_another_workspace(
    auth_client: Any, workspace: Any, generation_costs: Any
) -> None:
    from django.contrib.auth import get_user_model

    from products.services.products import create_product
    from workspaces.services.provisioning import provision_workspace

    other_owner = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_workspace = provision_workspace(other_owner, name="Not Yours")
    other_product = create_product(workspace=other_workspace, name="Not yours")

    response = auth_client.post(
        GENERATE_URL,
        {"kind": "TEXT", "mode": "IDEA", "prompt": "hi", "product": other_product.id},
        format="json",
    )

    assert response.status_code == 400


def test_generate_video_on_free_plan_is_a_402(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {"kind": "VIDEO", "mode": "PRODUCT", "prompt": "a short clip"},
        format="json",
    )

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]["suggested_plan"] == "pro"


def test_retrieve_generation(auth_client: Any, workspace: Any, generation_costs: Any) -> None:
    create_response = auth_client.post(
        GENERATE_URL, {"kind": "TEXT", "mode": "IDEA", "prompt": "hi"}, format="json"
    )
    generation_id = create_response.json()["id"]

    response = auth_client.get(f"{GENERATIONS_URL}{generation_id}/")

    assert response.status_code == 200
    assert response.json()["id"] == generation_id


def test_revise_a_succeeded_generation(
    auth_client: Any, workspace: Any, generation_costs: Any
) -> None:
    create_response = auth_client.post(
        GENERATE_URL, {"kind": "TEXT", "mode": "IDEA", "prompt": "hi"}, format="json"
    )
    generation_id = create_response.json()["id"]

    response = auth_client.post(
        f"{GENERATIONS_URL}{generation_id}/revise/",
        {"instructions": "make it punchier"},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "REVISION"
    assert body["credits_charged"] == 1
    assert body["parent_generation"] == generation_id


def test_revise_a_pending_or_failed_generation_is_rejected(
    auth_client: Any, workspace: Any, user: Any, generation_costs: Any
) -> None:
    from ai.models import Generation, GenerationKind, GenerationMode

    pending = Generation.objects.create(
        workspace=workspace, user=user, kind=GenerationKind.TEXT, mode=GenerationMode.IDEA
    )

    response = auth_client.post(
        f"{GENERATIONS_URL}{pending.id}/revise/", {"instructions": "anything"}, format="json"
    )

    assert response.status_code == 400


def test_voice_profile_list_create_and_retrieve(auth_client: Any, workspace: Any) -> None:
    create_response = auth_client.post(
        VOICE_PROFILES_URL,
        {"name": "Default", "tone_descriptors": ["warm"], "banned_phrases": []},
        format="json",
    )
    assert create_response.status_code == 201
    profile_id = create_response.json()["id"]

    list_response = auth_client.get(VOICE_PROFILES_URL)
    assert list_response.status_code == 200
    assert list_response.json()["count"] == 1

    detail_response = auth_client.get(f"{VOICE_PROFILES_URL}{profile_id}/")
    assert detail_response.status_code == 200
    assert detail_response.json()["name"] == "Default"


def test_generation_endpoints_require_authentication() -> None:
    """`GenerationViewSet` has no list route (design.md §7 names only the
    detail and revise routes) — `/1/` is the one that exists to be
    unauthenticated against."""
    from rest_framework.test import APIClient

    api = APIClient()
    assert api.post(GENERATE_URL, {}, format="json").status_code == 401
    assert api.get(f"{GENERATIONS_URL}1/").status_code == 401
