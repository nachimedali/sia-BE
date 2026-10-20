from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import AudienceComment, Availability
from analytics.tests.conftest import make_target
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


def make_comment(
    target: Any,
    body: str = "Where can I buy this?",
    *,
    external_id: str | None = None,
    author: str = "maria",
    sentiment: str = "NEU",
    capabilities: dict[str, bool] | None = None,
) -> AudienceComment:
    return AudienceComment.objects.create(
        post_target=target,
        external_id=external_id or f"c-{AudienceComment.objects.count() + 1}",
        author=author,
        body=body,
        sentiment=sentiment,
        posted_at=timezone.now(),
        capabilities=capabilities,
        availability=Availability.MEASURED,
    )


@pytest.fixture
def target(paid_workspace: Any, user: Any, social_account: Any) -> Any:
    return make_target(paid_workspace, user, social_account, age_days=1)


@pytest.fixture
def teammate(paid_workspace: Any) -> Any:
    from workspaces.models import Membership, Role

    person = get_user_model().objects.create_user(email="lea@example.com", password=PASSWORD)
    Membership.objects.create(user=person, workspace=paid_workspace, role=Role.EDITOR)
    return person
