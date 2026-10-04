"""The wizard with the website import on: Account 1, Import 2, Review 3,
Brand 4, Market 5, Operate 6, Plan 7, Finish 8."""

from __future__ import annotations

from typing import Any

import pytest

from brand.models import BrandImport, ImportStatus
from onboarding.services import wizard
from workspaces.services.provisioning import provision_extra_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def verified(user: Any, workspace: Any) -> Any:
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    return workspace


def run(workspace: Any, status: str) -> BrandImport:
    return BrandImport.objects.create(
        workspace=workspace, url="https://x.tn/", domain="x.tn", status=status
    )


def test_the_import_steps_are_offered(verified: Any) -> None:
    assert wizard.steps_for(verified) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert wizard.current_step(verified, verified.organization.owner) == 2


@pytest.mark.parametrize(
    ("status", "step"),
    [
        (ImportStatus.RUNNING, 2),
        (ImportStatus.FAILED, 2),
        (ImportStatus.SUCCEEDED, 3),
        (ImportStatus.APPLIED, 4),
        (ImportStatus.SKIPPED, 4),
    ],
)
def test_the_import_moves_the_wizard_on_only_once_it_is_answered(
    verified: Any, status: str, step: int
) -> None:
    run(verified, status)

    assert wizard.current_step(verified, verified.organization.owner) == step


def test_the_api_reports_the_import_steps(auth_client: Any, verified: Any) -> None:
    body = auth_client.get("/api/v1/onboarding/").json()

    assert body["current_step"] == 2
    assert body["applicable_steps"] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_a_second_brand_gets_its_own_import(auth_client: Any, verified: Any, user: Any) -> None:
    run(verified, ImportStatus.APPLIED)
    second = provision_extra_workspace(user=user, name="Second Brand")

    # The first workspace's import does not answer the second's.
    assert 2 in wizard.steps_for(second)
    assert wizard.current_step(second, user) == 2
