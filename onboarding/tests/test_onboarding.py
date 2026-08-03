"""The six-step wizard (design.md §10.4, implementation.md Phase 2.4)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from workspaces.models import BrandVoice, BusinessType, Workspace
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()
ONBOARDING_URL = "/api/v1/onboarding/"
COMPLETE_URL = "/api/v1/onboarding/complete/"


@pytest.fixture
def account(plans):
    user = User.objects.create_user(email="jordan@example.com", password="x")
    workspace = provision_workspace(user, name="Acme Studio")
    return user, workspace


@pytest.fixture
def client(account):
    user, _ = account
    api = APIClient()
    api.force_authenticate(user)
    return api


def verify(user):
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])


def fill_steps_2_to_4(client, category):
    client.patch(
        ONBOARDING_URL,
        {
            "name": "Acme Studio",
            "website": "https://acmestudio.com",
            "description": "Hand-glazed ceramics.",
            "brand_voice_default": BrandVoice.EDITORIAL,
        },
        format="json",
    )
    client.patch(
        ONBOARDING_URL,
        {
            "category": category.id,
            "business_type": BusinessType.D2C,
            "target_audience": "Design-minded homeowners, 28-45.",
        },
        format="json",
    )
    client.patch(
        ONBOARDING_URL,
        {
            "timezone": "Europe/Lisbon",
            "regions": ["PT", "ES"],
            "platforms": ["instagram", "tiktok"],
        },
        format="json",
    )


def test_cannot_proceed_past_step_1_unverified(client, account, category) -> None:
    """Step 1 is a gate, not a formality (design.md §10.4)."""
    user, _ = account

    assert client.get(ONBOARDING_URL).json()["current_step"] == 1

    fill_steps_2_to_4(client, category)
    response = client.post(COMPLETE_URL)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "email_not_verified"
    assert Workspace.objects.get().onboarding_complete is False

    verify(user)
    assert client.post(COMPLETE_URL).status_code == 200


def test_current_step_advances_as_fields_are_filled(client, account, category) -> None:
    user, _ = account
    verify(user)

    assert client.get(ONBOARDING_URL).json()["current_step"] == 2

    # Only the field the step exists to collect is required; website and
    # target audience are optional and must not hold the wizard back.
    client.patch(ONBOARDING_URL, {"description": "Hand-glazed ceramics."}, format="json")
    assert client.get(ONBOARDING_URL).json()["current_step"] == 3

    client.patch(ONBOARDING_URL, {"category": category.id}, format="json")
    assert client.get(ONBOARDING_URL).json()["current_step"] == 4

    client.patch(ONBOARDING_URL, {"platforms": ["instagram"]}, format="json")
    assert client.get(ONBOARDING_URL).json()["current_step"] == 6


def test_optional_fields_do_not_block_progress(client, account, category) -> None:
    """Pressing Continue advanced the user while current_step stayed put, so
    resuming sent them backwards. A step is done when its own question is
    answered."""
    user, _ = account
    verify(user)

    client.patch(ONBOARDING_URL, {"description": "Ceramics."}, format="json")

    state = client.get(ONBOARDING_URL).json()
    assert state["website"] == ""
    assert state["target_audience"] == ""
    assert 2 in state["completed_steps"]


def test_a_placeholder_workspace_name_does_not_skip_the_brand_step(client, account) -> None:
    """Registration derives a name from the email address; that is a
    placeholder, not a brand the user has confirmed."""
    user, _ = account
    verify(user)

    state = client.get(ONBOARDING_URL).json()
    assert state["name"] == "Acme Studio"
    assert state["current_step"] == 2


def test_onboarding_is_resumable_across_sessions(client, account, category) -> None:
    """A wizard that loses progress on a refresh is a wizard people abandon."""
    user, _ = account
    verify(user)

    client.patch(ONBOARDING_URL, {"name": "Acme Studio", "description": "Ceramics."}, format="json")

    # A brand-new client, as though the user closed the tab and came back.
    fresh = APIClient()
    fresh.force_authenticate(User.objects.get(pk=user.pk))
    state = fresh.get(ONBOARDING_URL).json()

    assert state["name"] == "Acme Studio"
    assert state["description"] == "Ceramics."
    # Brand is answered, so resuming picks up at Market rather than replaying it.
    assert state["current_step"] == 3


def test_patches_are_partial_and_do_not_clear_other_steps(client, account, category) -> None:
    user, _ = account
    verify(user)
    fill_steps_2_to_4(client, category)

    client.patch(ONBOARDING_URL, {"target_audience": "Updated."}, format="json")

    state = client.get(ONBOARDING_URL).json()
    assert state["target_audience"] == "Updated."
    assert state["name"] == "Acme Studio"
    assert state["platforms"] == ["instagram", "tiktok"]


def test_complete_flips_the_flag_and_reports_it(client, account, category) -> None:
    user, _ = account
    verify(user)
    fill_steps_2_to_4(client, category)

    response = client.post(COMPLETE_URL)

    assert response.status_code == 200
    assert response.json()["onboarding_complete"] is True
    assert Workspace.objects.get().onboarding_complete is True


def test_complete_reports_exactly_what_is_missing(client, account) -> None:
    user, _ = account
    verify(user)
    Workspace.objects.update(name="")

    response = client.post(COMPLETE_URL)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "onboarding_incomplete"
    assert set(error["detail"]["missing"]) == {"name", "category"}


def test_step_5_soft_defaults_to_free(client, account) -> None:
    """D14: nobody hits a paywall before seeing the product."""
    state = client.get(ONBOARDING_URL).json()
    assert state["plan_code"] == "free"
    assert 5 in state["completed_steps"]


def test_timezone_must_be_a_real_iana_zone(client, account) -> None:
    """Everything downstream schedules against this; a bad zone would silently
    publish at the wrong hour (implementation.md §4.7)."""
    bad = client.patch(ONBOARDING_URL, {"timezone": "Mars/Olympus"}, format="json")
    assert bad.status_code == 400

    assert (
        client.patch(ONBOARDING_URL, {"timezone": "Asia/Tokyo"}, format="json").status_code == 200
    )


def test_list_fields_are_deduplicated(client, account) -> None:
    response = client.patch(
        ONBOARDING_URL,
        {"platforms": ["instagram", "instagram", " tiktok ", ""]},
        format="json",
    )
    assert response.json()["platforms"] == ["instagram", "tiktok"]


def test_list_fields_reject_non_string_members(client, account) -> None:
    response = client.patch(ONBOARDING_URL, {"regions": [{"country": "PT"}]}, format="json")
    assert response.status_code == 400


def test_onboarding_requires_authentication() -> None:
    assert APIClient().get(ONBOARDING_URL).status_code == 401


def test_a_user_only_sees_their_own_workspace(client, account, plans) -> None:
    """Tenancy: the wizard must never resolve to somebody else's workspace."""
    other = User.objects.create_user(email="other@example.com", password="x")
    provision_workspace(other, name="Someone Else")

    assert client.get(ONBOARDING_URL).json()["name"] == "Acme Studio"
