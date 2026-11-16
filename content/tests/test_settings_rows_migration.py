"""`0022_settings_rows_not_failed`: only the wrongly failed settings rows reset."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps

from content.models import PostTarget, PostTargetState
from content.services.posts import create_post

MIGRATION = importlib.import_module("content.migrations.0022_settings_rows_not_failed")
pytestmark = pytest.mark.django_db

FAILED = {"code": "platform_error", "message": MIGRATION.MESSAGE, "retryable": False}


def test_a_failed_settings_row_resets_and_a_failed_delivery_target_stays(
    paid_workspace: Any, user: Any
) -> None:
    post = create_post(workspace=paid_workspace, author=user, master_body="Drop")
    settings_row = PostTarget.objects.create(
        post=post, platform="instagram", state="FAILED", error_detail=FAILED, attempt_count=1
    )
    orphan = PostTarget.objects.create(
        post=post,
        platform="facebook",
        state="FAILED",
        error_detail=FAILED,
        attempt_count=1,
        idempotency_key="minted-at-schedule",
    )

    MIGRATION.reset(apps, None)

    settings_row.refresh_from_db()
    orphan.refresh_from_db()
    assert (settings_row.state, settings_row.error_detail, settings_row.attempt_count) == (
        PostTargetState.PENDING,
        {},
        0,
    )
    assert orphan.state == PostTargetState.FAILED
