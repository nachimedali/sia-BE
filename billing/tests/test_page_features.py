"""Whole pages per plan (2026-10-26, owner): Spark opens no campaigns, no
refrains (templates and recurrence rules) and no analytics page.

A 402 with an upgrade path, never a 403: the caller may use the page, their
plan does not include it. The reads home, the post page and the inbox share
(overview, posts, sentiment, comments) stay open on every plan.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from django.apps import apps

from billing.models import FeatureFlag, Plan
from billing.services.flags import PLANNING_V3

pytestmark = pytest.mark.django_db

LOCKED = [
    "/api/v1/campaigns/",
    "/api/v1/post-templates/",
    "/api/v1/recurrence-rules/",
    "/api/v1/reports/",
    "/api/v1/analytics/summary/",
    "/api/v1/analytics/best-times/",
    "/api/v1/analytics/demographics/",
    "/api/v1/analytics/competitors/",
]
SHARED = [
    "/api/v1/analytics/overview/",
    "/api/v1/analytics/posts/",
    "/api/v1/analytics/sentiment/",
    "/api/v1/analytics/comments/",
]


@pytest.mark.parametrize("url", LOCKED)
def test_spark_is_refused_with_an_upgrade_path(auth_client: Any, workspace: Any, url: str) -> None:
    assert workspace.organization.plan.code == "spark"

    response = auth_client.get(url)

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]["suggested_plan"] == "essential"


@pytest.mark.parametrize("url", LOCKED)
def test_a_paid_package_opens_the_page(
    auth_client: Any, workspace: Any, plans: dict[str, Any], url: str
) -> None:
    workspace.organization.plan = plans["essential"]
    workspace.organization.save(update_fields=["plan"])

    assert auth_client.get(url).status_code == 200


@pytest.mark.parametrize("url", SHARED)
def test_the_shared_reads_stay_open_on_spark(auth_client: Any, workspace: Any, url: str) -> None:
    assert auth_client.get(url).status_code == 200


def test_a_flag_that_is_off_still_answers_404_before_the_402(
    auth_client: Any, workspace: Any
) -> None:
    FeatureFlag.objects.create(organization=workspace.organization, key=PLANNING_V3, enabled=False)

    assert auth_client.get("/api/v1/campaigns/").status_code == 404


def test_the_migration_keeps_every_other_row_open_and_never_overwrites(plans: Any) -> None:
    forwards = import_module("billing.migrations.0025_plan_page_features").forwards
    spark, pro = Plan.objects.get(code="spark"), Plan.objects.get(code="pro")
    custom = Plan.objects.create(code="custom", display_name="Custom", features={})
    for plan in (spark, pro):
        plan.features = {k: v for k, v in plan.features.items() if k != "campaigns"}
        plan.save(update_fields=["features"])
    pro.features = {**pro.features, "analytics": False}  # an operator's edit
    pro.save(update_fields=["features"])

    forwards(apps, None)

    for plan in (spark, pro, custom):
        plan.refresh_from_db()
    assert spark.features["campaigns"] is False
    assert pro.features["campaigns"] is True
    assert pro.features["analytics"] is False
    assert custom.features == {"campaigns": True, "templates": True, "analytics": True}
