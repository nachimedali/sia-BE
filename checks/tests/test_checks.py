"""Pre-publish checks (steps-plan S4): what each check reports, the queue badge
read from the same request, and the gate that refuses to approve or schedule a
blocked post."""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from billing.models import FeatureFlag
from billing.services.flags import CHECKS_S4
from brand.models import BrandCore
from checks import services
from checks.fakes import ScriptedOcr
from checks.models import CheckRun
from checks.ports import TextBox, set_ocr_override, set_policy_override
from common.exceptions import StateConflict
from content.models import Platform, PostFormat, PostStatus, PostTarget
from content.services.media import ingest_media
from content.services.posts import create_post, edit_post
from workspaces.services import approvals
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

CHECKS = "/api/v1/posts/{}/checks/"


@pytest.fixture(autouse=True)
def _clean_ports() -> Iterator[None]:
    yield
    set_ocr_override(None)
    set_policy_override(None)


def image(workspace: Any, size: tuple[int, int] = (1080, 1350), color: str = "#3050c0") -> Any:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    upload = SimpleUploadedFile("photo.png", buffer.getvalue(), content_type="image/png")
    return ingest_media(workspace=workspace, upload=upload)


def post_with(
    workspace: Any,
    user: Any,
    *,
    body: str = "Fresh from the kiln.",
    media: list[Any] | None = None,
    platforms: tuple[str, ...] = ("instagram",),
) -> Any:
    return create_post(
        workspace=workspace,
        author=user,
        master_body=body,
        media_assets=media or [],
        planned_platforms=platforms,
    )


def results(run: CheckRun, key: str) -> list[dict[str, Any]]:
    return [r for r in run.results if r["key"] == key]


# --- the checks ----------------------------------------------------------------------------
def test_media_is_checked_against_each_platforms_frame(workspace: Any, user: Any) -> None:
    small = image(workspace, (600, 600))
    tall = image(workspace, (1080, 1920))
    post = post_with(workspace, user, media=[small, tall], platforms=("instagram", "linkedin"))

    run = services.run_checks(post)

    format_results = results(run, "format")
    messages = " ".join(r["message"] for r in format_results)
    assert f"A-{small.pk} is 600 px wide; Instagram feed post is shown at 1080 px" in messages
    assert f"A-{tall.pk} is 1080 x 1920 (0.56)" in messages
    assert {r["status"] for r in format_results} == {"FIX"}
    assert run.verdict == "FIX"


def test_a_story_in_its_own_frame_passes(workspace: Any, user: Any) -> None:
    tall = image(workspace, (1080, 1920))
    post = post_with(workspace, user, media=[tall])
    PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM, post_format=PostFormat.STORY)

    run = services.run_checks(post)

    assert [r["status"] for r in results(run, "format")] == ["PASS"]


def test_small_text_is_reported_never_passed(workspace: Any, user: Any) -> None:
    set_ocr_override(ScriptedOcr([TextBox("50% off today", 0.3, 0.5, 0.4, 0.012)]))
    post = post_with(workspace, user, media=[image(workspace)])

    run = services.run_checks(post)

    (legibility,) = results(run, "legibility")
    assert legibility["status"] == "FIX"
    assert "“50% off today” is 1.2% of the image's height" in legibility["message"]


def test_readable_text_passes_and_no_reader_is_unavailable_not_a_pass(
    workspace: Any, user: Any, settings: Any
) -> None:
    set_ocr_override(ScriptedOcr([TextBox("Harvest week", 0.2, 0.4, 0.6, 0.08)]))
    post = post_with(workspace, user, media=[image(workspace)])
    assert [r["status"] for r in results(services.run_checks(post), "legibility")] == ["PASS"]

    set_ocr_override(None)
    settings.USE_FAKE_AI_PROVIDERS = False
    run = services.run_checks(post)

    assert [r["status"] for r in results(run, "legibility")] == ["UNAVAILABLE"]
    assert run.counts["UNAVAILABLE"] >= 1


def test_text_under_a_vertical_formats_interface_is_flagged(workspace: Any, user: Any) -> None:
    set_ocr_override(ScriptedOcr([TextBox("Swipe up", 0.3, 0.9, 0.4, 0.05)]))
    post = post_with(workspace, user, media=[image(workspace, (1080, 1920))])
    PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM, post_format=PostFormat.STORY)

    (safe,) = results(services.run_checks(post), "safe_zone")

    assert safe["status"] == "FIX" and "bottom band" in safe["message"]


def test_a_feed_post_has_no_safe_zone_check(workspace: Any, user: Any) -> None:
    post = post_with(workspace, user, media=[image(workspace)])
    assert results(services.run_checks(post), "safe_zone") == []


def test_policy_blocks_a_cure_claim_with_the_clause_and_a_rewrite(
    workspace: Any, user: Any
) -> None:
    post = post_with(workspace, user, body="This herbal tea cures cancer. Order today.")

    run = services.run_checks(post)

    (policy,) = results(run, "policy")
    assert policy["status"] == "BLOCK"
    assert policy["clause"].startswith("Ads may not claim")
    assert policy["source"].startswith("https://")
    assert policy["excerpt"] == "cures cancer"
    assert "cures cancer" not in policy["rewrite"] and "Order today." in policy["rewrite"]
    assert policy["platforms"] == ["instagram"]
    assert run.verdict == "BLOCK"


def test_a_superlative_needs_fixing_and_a_clean_caption_passes(workspace: Any, user: Any) -> None:
    fix = services.run_checks(post_with(workspace, user, body="The world's best olive oil."))
    clean = services.run_checks(post_with(workspace, user, body="Pressed this morning."))

    assert [r["status"] for r in results(fix, "policy")] == ["FIX"]
    assert [r["status"] for r in results(clean, "policy")] == ["PASS"]


def test_no_policy_reader_means_not_checked(workspace: Any, user: Any, settings: Any) -> None:
    settings.USE_FAKE_AI_PROVIDERS = False
    run = services.run_checks(post_with(workspace, user, body="This tea cures cancer."))

    assert [r["status"] for r in results(run, "policy")] == ["UNAVAILABLE"]
    assert run.verdict != "BLOCK"  # nothing *known* blocks it; the panel says it was not read


def test_palette_is_read_against_the_brand_kit(workspace: Any, user: Any) -> None:
    blue = image(workspace, color="#3050c0")
    post = post_with(workspace, user, media=[blue])
    assert [r["status"] for r in results(services.run_checks(post), "palette")] == ["UNAVAILABLE"]

    BrandCore.objects.create(
        workspace=workspace,
        version=1,
        sections={"palette": {"colors": [{"role": "Primary", "hex": "#c03020"}]}},
    )
    assert [r["status"] for r in results(services.run_checks(post), "palette")] == ["FIX"]

    red = image(workspace, color="#c23122")
    on_brand = post_with(workspace, user, media=[red])
    assert [r["status"] for r in results(services.run_checks(on_brand), "palette")] == ["PASS"]


def test_video_frames_are_reported_unchecked(workspace: Any, user: Any) -> None:
    clip = ingest_media(
        workspace=workspace,
        upload=SimpleUploadedFile("a.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"),
    )
    run = services.run_checks(post_with(workspace, user, media=[clip]))

    assert [r["status"] for r in results(run, "video")] == ["UNAVAILABLE"]


# --- the gate ------------------------------------------------------------------------------
@pytest.fixture
def blocked(advanced_workspace: Any, user: Any) -> Any:
    post = post_with(advanced_workspace, user, body="This tea cures cancer.")
    approvals.submit_for_review(post, actor=user)
    return post


def test_a_block_refuses_approval_with_a_409(client_as: Any, admin_user: Any, blocked: Any) -> None:
    response = client_as(admin_user).post(f"/api/v1/posts/{blocked.pk}/approve/", {}, format="json")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "checks_blocked"
    assert error["detail"]["blocking"][0]["label"] == "Platform policy"
    blocked.refresh_from_db()
    assert blocked.status == PostStatus.PENDING_REVIEW


def test_a_block_refuses_scheduling(workspace: Any, user: Any) -> None:
    from scheduling.services import schedule_post

    post = post_with(workspace, user, body="Guaranteed returns on every bottle.")

    with pytest.raises(services.ChecksBlockedError):
        schedule_post(
            post=post,
            delivery_mode="REMINDER",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            actor=user,
        )
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT and post.scheduled_at is None


def test_a_guest_cannot_approve_a_blocked_post_either(blocked: Any, user: Any) -> None:
    from collaboration.models import ReviewLink

    link = ReviewLink(post=blocked, created_by=user, email="client@example.com")
    with pytest.raises(StateConflict):
        approvals.approve_as_guest(blocked, link=link)


def test_fix_required_warns_but_approval_goes_through(
    advanced_workspace: Any, user: Any, admin_user: Any
) -> None:
    post = post_with(advanced_workspace, user, body="The world's best mug.")
    approvals.submit_for_review(post, actor=user)

    approved = approvals.approve(post, actor=admin_user)

    assert approved.status == PostStatus.APPROVED
    assert services.latest(post).verdict == "FIX"  # type: ignore[union-attr]


def test_the_gate_reads_the_post_as_it_is_now(
    advanced_workspace: Any, user: Any, admin_user: Any
) -> None:
    post = post_with(advanced_workspace, user, body="This tea cures cancer.")
    services.run_checks(post)  # a blocking run on file
    edit_post(post, author=user, master_body="This tea is a calm way to end the day.")
    approvals.submit_for_review(post, actor=user)

    assert approvals.approve(post, actor=admin_user).status == PostStatus.APPROVED
    assert services.latest(post).verdict == "PASS"  # type: ignore[union-attr]


def test_flag_off_is_pre_step_behaviour(
    advanced_workspace: Any, user: Any, admin_user: Any
) -> None:
    FeatureFlag.objects.create(
        organization=advanced_workspace.organization, key=CHECKS_S4, enabled=False
    )
    post = post_with(advanced_workspace, user, body="This tea cures cancer.")
    approvals.submit_for_review(post, actor=user)

    assert approvals.approve(post, actor=admin_user).status == PostStatus.APPROVED
    assert not CheckRun.objects.exists()


def test_submitting_runs_the_checks_after_commit(
    workspace: Any, user: Any, django_capture_on_commit_callbacks: Any
) -> None:
    post = post_with(workspace, user, body="The world's best mug.")
    with django_capture_on_commit_callbacks(execute=True):
        approvals.submit_for_review(post, actor=user)

    assert services.latest(post).verdict == "FIX"  # type: ignore[union-attr]


# --- the queue badge -----------------------------------------------------------------------
def queue(client: Any, **headers: Any) -> list[dict[str, Any]]:
    response = client.get("/api/v1/posts/?status=PENDING_REVIEW", **headers)
    assert response.status_code == 200
    return response.json()["results"]


def test_the_queue_carries_each_posts_badge(auth_client: Any, workspace: Any, user: Any) -> None:
    blocked = post_with(workspace, user, body="This tea cures cancer.")
    unchecked = post_with(workspace, user, body="Pressed this morning.")
    for post in (blocked, unchecked):
        approvals.submit_for_review(post, actor=user)
    services.run_checks(blocked)

    rows = {row["id"]: row for row in queue(auth_client)}

    badge = rows[blocked.pk]["checks"]
    assert badge["verdict"] == "BLOCK" and badge["counts"]["BLOCK"] == 1 and badge["stale"] is False
    assert rows[unchecked.pk]["checks"]["verdict"] == "NONE"  # said, not hidden


def test_the_badge_goes_stale_when_the_post_changes(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = post_with(workspace, user, body="This tea cures cancer.")
    services.run_checks(post)
    edit_post(post, author=user, master_body="A calm tea for the evening.")
    approvals.submit_for_review(post, actor=user)

    (row,) = queue(auth_client)

    assert row["checks"]["verdict"] == "BLOCK" and row["checks"]["stale"] is True


def test_badges_cost_no_query_per_row(
    auth_client: Any, workspace: Any, user: Any, django_assert_num_queries: Any
) -> None:
    def submit(n: int) -> None:
        for _ in range(n):
            post = post_with(workspace, user, media=[image(workspace)])
            approvals.submit_for_review(post, actor=user)
            services.run_checks(post)

    submit(1)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as one:
        queue(auth_client)
    submit(5)
    with CaptureQueriesContext(connection) as six:
        rows = queue(auth_client)

    assert len(rows) == 6 and all(row["checks"] for row in rows)
    assert len(six.captured_queries) == len(one.captured_queries)


def test_flag_off_leaves_the_badge_null(auth_client: Any, workspace: Any, user: Any) -> None:
    post = post_with(workspace, user, body="This tea cures cancer.")
    approvals.submit_for_review(post, actor=user)
    services.run_checks(post)
    FeatureFlag.objects.create(organization=workspace.organization, key=CHECKS_S4, enabled=False)

    (row,) = queue(auth_client)

    assert row["checks"] is None
    assert auth_client.get(CHECKS.format(post.pk)).status_code == 404


# --- the endpoint --------------------------------------------------------------------------
def test_run_and_read_the_checks(auth_client: Any, workspace: Any, user: Any) -> None:
    post = post_with(workspace, user, body="This tea cures cancer.")
    assert auth_client.get(CHECKS.format(post.pk)).status_code == 204

    ran = auth_client.post(CHECKS.format(post.pk))
    read = auth_client.get(CHECKS.format(post.pk)).json()

    assert ran.status_code == 202 and ran.json()["state"] == "DONE"
    assert read["verdict"] == "BLOCK" and read["stale"] is False
    assert {r["key"] for r in read["results"]} >= {"format", "policy"}


def test_a_viewer_reads_checks_but_cannot_run_them(
    client_as: Any, viewer_user: Any, advanced_workspace: Any, user: Any
) -> None:
    post = post_with(advanced_workspace, user)
    viewer = client_as(viewer_user)

    assert viewer.get(CHECKS.format(post.pk)).status_code == 204
    assert viewer.post(CHECKS.format(post.pk)).status_code == 403


def test_another_organizations_post_is_404(
    auth_client: Any, workspace: Any, other_user: Any
) -> None:
    theirs = post_with(provision_workspace(other_user, name="Theirs"), other_user)

    assert auth_client.get(CHECKS.format(theirs.pk)).status_code == 404
    assert auth_client.post(CHECKS.format(theirs.pk)).status_code == 404
    assert auth_client.get(f"/api/v1/posts/{theirs.pk}/").status_code == 404


def test_a_sibling_workspaces_post_is_404(auth_client: Any, workspace: Any, user: Any) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    theirs = post_with(sibling, user)
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}

    assert auth_client.get(CHECKS.format(theirs.pk), **here).status_code == 404
    assert theirs.pk not in [row["id"] for row in queue(auth_client, **here)]
