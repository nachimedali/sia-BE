"""S17-37 — template results (`studio_content_audit.md` §7, BUILD-PLAN L-7).

Every post sent from a template render keeps its template, and the render its
angle, hook style and hook. Per template the gallery shows how many posts went
out, how many were measured, and their median engagement — **computed in code,
from this brand's own measured, real captures only** (rule 4: unavailable is
null, never zero; rule 5: statistics in code; fakes are never insight). Below
eight measured posts a result says it is not enough to go on, and only a
template with enough of them can lift its place in the recommendations.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from ai.models import VideoRender
from ai.services import template_results
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    GALLERY,
    RENDERS,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    make_product,
)
from analytics.models import Availability, MetricSource, PostMetric
from content.models import PostStatus, PostTarget, PostTargetState
from content.services.posts import create_post
from workspaces.services.provisioning import provision_extra_workspace

pytestmark = pytest.mark.django_db


def sent(
    workspace: Any, user: Any, template: str, rates: list[float | None], **metric: Any
) -> None:
    """One post per rate, sent from a render of `template`, each captured once."""
    for rate in rates:
        post = create_post(workspace=workspace, author=user, master_body="Hello")
        post.status = PostStatus.PUBLISHED
        post.save(update_fields=["status"])
        VideoRender.objects.create(
            workspace=workspace,
            created_by=user,
            mode="clip",
            spec={"seconds": 10},
            template=template,
            post=post,
            status="SUCCEEDED",
            credits=0,
        )
        target = PostTarget.objects.create(
            post=post,
            platform="instagram",
            state=PostTargetState.PUBLISHED,
            published_at=timezone.now() - dt.timedelta(days=3),
        )
        PostMetric.objects.create(
            post_target=target,
            captured_at=timezone.now() - dt.timedelta(days=2),
            engagement_rate=rate,
            impressions=1000 if rate is not None else None,
            availability=Availability.MEASURED if rate is not None else Availability.UNAVAILABLE,
            provider_key="socialapi",
            **metric,
        )


def test_the_median_of_measured_posts_per_template(growth: Any, user: Any) -> None:  # noqa: F811
    sent(growth, user, "pour-shot", [0.01, 0.03, 0.02, None])

    result = template_results.results_for(growth)["pour-shot"]

    assert result == {"posts": 4, "measured": 3, "median_engagement": 0.02, "enough": False}


def test_fake_captures_are_never_counted(growth: Any, user: Any) -> None:  # noqa: F811
    sent(growth, user, "pour-shot", [0.5, 0.5], source=MetricSource.FAKE)
    result = template_results.results_for(growth)["pour-shot"]
    assert result["measured"] == 0 and result["median_engagement"] is None


def test_eight_measured_posts_are_enough(growth: Any, user: Any) -> None:  # noqa: F811
    sent(growth, user, "levitate", [0.01 * index for index in range(1, 9)])
    assert template_results.results_for(growth)["levitate"]["enough"] is True


def test_another_brands_posts_never_count(growth: Any, user: Any) -> None:  # noqa: F811
    sibling = provision_extra_workspace(user=user, name="Sibling")
    sent(sibling, user, "pour-shot", [0.9] * 8)
    assert "pour-shot" not in template_results.results_for(growth)


def test_the_gallery_shows_each_templates_results(
    auth_client: Any,
    growth: Any,  # noqa: F811
    user: Any,
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    sent(growth, user, "pour-shot", [0.02, 0.04])
    rows = {
        row["key"]: row
        for row in auth_client.get(f"{GALLERY}?product={product.pk}").json()["templates"]
    }
    assert rows["pour-shot"]["results"] == {
        "posts": 2,
        "measured": 2,
        "median_engagement": 0.03,
        "enough": False,
    }
    assert rows["levitate"]["results"] is None


def test_a_proven_template_is_recommended_first_within_its_goal(
    auth_client: Any,
    growth: Any,  # noqa: F811
    user: Any,
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    product.campaign_goal = "awareness"
    product.save(update_fields=["campaign_goal"])
    before = auth_client.get(f"{GALLERY}?product={product.pk}").json()["recommended"]
    rows = {
        row["key"]: row
        for row in auth_client.get(f"{GALLERY}?product={product.pk}").json()["templates"]
    }
    # A "show" template the gallery did not put first, given strong results.
    candidate = next(
        key
        for key, row in rows.items()
        if row["goal"] == "show"
        and key != before[0]
        and row["available"]
        and row["fit"] is None
        and row["lock"] is None
    )
    sent(growth, user, candidate, [0.08] * 8)

    after = auth_client.get(f"{GALLERY}?product={product.pk}").json()["recommended"]
    assert after[0] == candidate


def test_a_render_keeps_its_angle_and_hook_style(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "levitate",
        "product": product.pk,
        "language": "en",
        "angle": "proof",
        "family": "number",
    }
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()
    assert render["status"] == "SUCCEEDED", render
    spec = VideoRender.objects.get(pk=render["id"]).spec
    assert (spec["angle"], spec["family"]) == ("proof", "number")
    bad = auth_client.post(ESTIMATE, {**body, "angle": "magic"}, format="json")
    assert bad.status_code == 400
