"""Autopilot (design.md §8.7, I3; implementation.md Phase 12).

Every test here drives `products.services.autopilot` directly rather than
through the Beat task (A4) — the task body is one call, and eager mode would
only add a broker to the thing under test.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from ai.models import Generation, GenerationMode
from billing.models import CreditLedger, VideoLedger, VideoReason
from billing.services import ledger
from content.models import Post, PostSource, PostStatus
from products.models import (
    AutopilotDraft,
    AutopilotDraftKind,
    AutopilotDraftStatus,
    AutopilotJobStatus,
    AutopilotLanding,
    AutopilotStrategy,
)
from products.services import autopilot

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# The five named Phase 12 tests
# -----------------------------------------------------------------------------
def test_autopilot_stops_at_included_video_allowance(
    autopilot_config: Any, autopilot_workspace: Any
) -> None:
    """I3. Pro includes 4 videos; a video-only mix asking for more than that
    stops at the wall rather than spending past it."""
    autopilot_config.format_mix = {"VIDEO": 1}
    autopilot_config.cadence_days = 1
    autopilot_config.lookahead_days = 10  # ten video slots against an allowance of four
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.BLOCKED_QUOTA
    assert job.detail["reason"] == "included_video_allowance"
    assert job.detail["videos_deferred"] == autopilot_workspace.plan.included_videos == 4


def test_autopilot_never_debits_prepaid_video_packs(
    autopilot_config: Any, autopilot_workspace: Any
) -> None:
    """I3. A workspace that has spent its allowance and holds a bought pack has
    nothing autopilot may touch — the pack is the user's to spend, not the
    engine's."""
    ledger.debit_video_units(autopilot_workspace, 4, quota=4, note="spent the included allowance")
    ledger.purchase_video_units(autopilot_workspace, 10, unit_cost_cents=320, note="pack of 10")
    assert ledger.video_balance(autopilot_workspace) == 10
    assert ledger.included_video_balance(autopilot_workspace) == 0

    rows_before = VideoLedger.objects.filter(workspace=autopilot_workspace).count()
    autopilot_config.format_mix = {"VIDEO": 1}
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.BLOCKED_QUOTA
    assert job.detail["videos_deferred"] == 0
    assert VideoLedger.objects.filter(workspace=autopilot_workspace).count() == rows_before
    assert ledger.video_balance(autopilot_workspace) == 10


def test_auto_approve_gated_to_advanced_plan(
    auth_client: Any, autopilot_product: Any, plans: dict[str, Any], autopilot_workspace: Any
) -> None:
    """§4.1 — `autopilot_auto_approve` is Advanced-only, and the gate is a 402
    with an upgrade payload, not a 403."""
    url = f"/api/v1/products/{autopilot_product.pk}/autopilot/"

    response = auth_client.patch(url, {"auto_approve": True}, format="json")

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]["suggested_plan"] == "advanced"

    autopilot_workspace.plan = plans["advanced"]
    autopilot_workspace.save(update_fields=["plan"])

    assert auth_client.patch(url, {"auto_approve": True}, format="json").status_code == 200


def test_cadence_generates_lookahead_days_ahead(autopilot_config: Any) -> None:
    """The calendar is filled to the horizon on the cadence grid, and a second
    run inside the same cadence window neither re-runs nor re-drafts."""
    autopilot_config.cadence_days = 2
    autopilot_config.lookahead_days = 6
    autopilot_config.save()

    start = timezone.now()
    with time_machine.travel(start, tick=False):
        job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.QUEUED
    drafts = list(AutopilotDraft.objects.filter(product=autopilot_config.product))
    # Three slots fit in six days at a two-day cadence.
    assert len(drafts) == 3
    assert all(start < d.scheduled_for <= start + dt.timedelta(days=6) for d in drafts)
    assert [(d.scheduled_for - drafts[0].scheduled_for).days for d in drafts] == [0, 2, 4]

    # Still inside the cadence window: not due, so nothing runs.
    with time_machine.travel(start + dt.timedelta(days=1), tick=False):
        assert autopilot.due_configs(now=timezone.now()) == []

    # Past it: due again, and the horizon has rolled forward by two days, so
    # exactly one new slot is drafted and the three existing ones are left alone.
    with time_machine.travel(start + dt.timedelta(days=2, hours=1), tick=False):
        assert autopilot.run_due(now=timezone.now()) == 1

    assert AutopilotDraft.objects.filter(product=autopilot_config.product).count() == 4


def test_restrictions_enforced_in_prompt_and_quality_gate(autopilot_config: Any) -> None:
    """design.md §8.7: product `restrictions` are hard constraints in every
    autopilot prompt *and* re-verified by the quality gate.

    Both halves, because a prompt the model ignores is exactly what the gate
    exists to catch — and the second half is asserted by making the gate
    actually report a violation (the fake's `FORCE_VIOLATION` sentinel) rather
    than by looking for the words in a string.
    """
    from ai.models import GenerationStatus
    from ai.providers.fake import _fake_image_provider

    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    product = autopilot_config.product
    restriction = product.restrictions[0]

    autopilot.run_config(autopilot_config)

    prompts = [call["prompt"] for call in _fake_image_provider.calls]
    assert prompts and all(restriction in prompt for prompt in prompts)

    # Now a restriction the gate reports as violated. Nothing may reach the
    # queue, and nothing may be charged for it (I2).
    AutopilotDraft.objects.all().delete()
    product.restrictions = ["Never show the mug empty. FORCE_VIOLATION"]
    product.save(update_fields=["restrictions"])
    balance_before = ledger.credit_balance(product.workspace)

    autopilot.run_config(autopilot_config, now=timezone.now() + dt.timedelta(hours=1))

    assert not AutopilotDraft.objects.exists()
    assert Generation.objects.filter(status=GenerationStatus.FAILED).exists()
    assert ledger.credit_balance(product.workspace) == balance_before


# -----------------------------------------------------------------------------
# Planning and cadence
# -----------------------------------------------------------------------------
def test_slots_are_a_stable_grid_regardless_of_when_the_run_happens(
    autopilot_config: Any,
) -> None:
    """Anchored on `created_at`: a late run must not shift the calendar, or the
    uniqueness constraint could no longer recognise a slot as already drafted."""
    autopilot_config.cadence_days = 1
    now = timezone.now()

    on_time = autopilot.planned_slots(autopilot_config, now=now, horizon_days=5)
    late = autopilot.planned_slots(
        autopilot_config, now=now + dt.timedelta(hours=3), horizon_days=5
    )

    # The three-hour delay does not move a single slot; it only rolls the far
    # end of the horizon forward, which is what lets it admit one more.
    assert late[: len(on_time)] == on_time


def test_horizon_is_clamped_to_what_the_plan_can_schedule(
    autopilot_config: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    """A Free horizon is 7 days; drafting 30 days out would produce drafts that
    `require_scheduling_horizon` could only ever refuse."""
    autopilot_workspace.plan = plans["free"]
    autopilot_workspace.save(update_fields=["plan"])
    autopilot_config.lookahead_days = 30
    autopilot_config.cadence_days = 1
    autopilot_config.save()

    from billing.services.entitlements import entitlements_for

    horizon = autopilot._horizon_days(autopilot_config, entitlements_for(autopilot_workspace))

    assert horizon == plans["free"].scheduling_horizon_days == 7


def test_a_blocked_run_still_consumes_its_cadence_tick(autopilot_config: Any) -> None:
    """Otherwise a config that cannot proceed is retried on every scan — the
    failure mode the Phase 9 cleanup pass flagged for `max_autopublish_posts`."""
    autopilot_config.format_mix = {"VIDEO": 1}
    autopilot_config.save()

    assert autopilot.run_due(now=timezone.now()) == 1
    assert autopilot.due_configs(now=timezone.now()) == []


def test_disabled_config_is_never_due(autopilot_config: Any) -> None:
    autopilot_config.enabled = False
    autopilot_config.save(update_fields=["enabled"])

    assert autopilot.due_configs(now=timezone.now()) == []


def test_one_broken_config_does_not_stop_the_rest(autopilot_config: Any, monkeypatch: Any) -> None:
    """`run_due` is the only loop spanning workspaces, so it is the only place
    swallowing an exception is right."""
    monkeypatch.setattr(
        autopilot, "run_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert autopilot.run_due(now=timezone.now()) == 0


def test_a_run_without_the_autopilot_feature_fails_cleanly(
    autopilot_config: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    """The plan can be downgraded after autopilot was configured; the engine
    must not act on a workspace that no longer has it."""
    autopilot_workspace.plan = plans["free"]
    autopilot_workspace.save(update_fields=["plan"])

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.FAILED
    assert job.detail["reason"] == "feature_not_available"
    assert not AutopilotDraft.objects.exists()


def test_a_product_without_a_reference_image_fails_cleanly(autopilot_config: Any) -> None:
    """I7 applies to the engine exactly as it does to `POST /ai/generate/`."""
    autopilot_config.product.reference_images.clear()
    from products.services.completeness import recompute_completeness

    recompute_completeness(autopilot_config.product)

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.FAILED
    assert job.detail["reason"] == "product_not_generation_ready"


def test_running_out_of_credits_blocks_rather_than_fails(
    autopilot_config: Any, autopilot_workspace: Any
) -> None:
    """A half-filled calendar is a correct outcome; a lost one is not."""
    ledger.adjust_credits(
        autopilot_workspace,
        -ledger.credit_balance(autopilot_workspace) + 3,
        actor=None,
        note="enough for exactly one draft",
    )
    autopilot_config.cadence_days = 1
    autopilot_config.lookahead_days = 5
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.BLOCKED_QUOTA
    assert job.detail["reason"] == "credits"
    assert job.detail["drafts_created"] == 1


# -----------------------------------------------------------------------------
# The generated draft
# -----------------------------------------------------------------------------
def test_a_draft_carries_a_visual_and_a_caption_charged_separately(
    autopilot_config: Any, autopilot_workspace: Any
) -> None:
    """§4.2 prices an autopilot image (2) and a text generation (1) separately,
    which is exactly why the draft holds one of each."""
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    autopilot.run_config(autopilot_config)

    draft = AutopilotDraft.objects.get()
    assert draft.kind == AutopilotDraftKind.IMAGE
    assert draft.caption
    variant = draft.generation.variants.first()
    assert variant is not None and variant.media_asset is not None
    assert draft.platform == "instagram"

    generations = Generation.objects.filter(workspace=autopilot_workspace)
    assert generations.count() == 2
    assert {g.mode for g in generations} == {GenerationMode.AUTOPILOT}
    # D8: autopilot is on the image Batch API, which is half the price.
    assert all(g.is_batch for g in generations)
    assert sum(g.credits_charged for g in generations) == 3


def test_strategy_weights_bias_the_mix_deterministically(autopilot_config: Any) -> None:
    """Deterministic, not random — an operator asking "why did it post that"
    deserves an answer that reproduces."""
    autopilot_config.strategy_weights = {
        AutopilotStrategy.TREND_LED: 2,
        AutopilotStrategy.PROMOTIONAL: 1,
    }
    autopilot_config.cadence_days = 1
    autopilot_config.lookahead_days = 3
    autopilot_config.save()

    autopilot.run_config(autopilot_config)

    strategies = list(
        AutopilotDraft.objects.filter(product=autopilot_config.product).values_list(
            "strategy", flat=True
        )
    )
    assert strategies.count(AutopilotStrategy.TREND_LED) == 2
    assert strategies.count(AutopilotStrategy.PROMOTIONAL) == 1


def test_malformed_weights_fall_back_to_the_single_strategy(autopilot_config: Any) -> None:
    """A bad JSON blob in admin must not crash a Beat task."""
    autopilot_config.strategy_weights = {"TREND_LED": "lots", "NOT_A_STRATEGY": 4}
    autopilot_config.strategy = AutopilotStrategy.EVERGREEN
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    autopilot.run_config(autopilot_config)

    assert AutopilotDraft.objects.get().strategy == AutopilotStrategy.EVERGREEN


def test_latitude_changes_the_brief(autopilot_config: Any) -> None:
    assert autopilot._latitude_line(0) != autopilot._latitude_line(50)
    assert autopilot._latitude_line(50) != autopilot._latitude_line(100)


# -----------------------------------------------------------------------------
# Routing and the review queue
# -----------------------------------------------------------------------------
def test_auto_approve_lands_drafts_on_the_calendar(
    autopilot_config: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    autopilot_workspace.plan = plans["advanced"]
    autopilot_workspace.save(update_fields=["plan"])
    autopilot_config.auto_approve = True
    autopilot_config.landing = AutopilotLanding.AUTO_CALENDAR
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.GENERATED
    draft = AutopilotDraft.objects.get()
    assert draft.status == AutopilotDraftStatus.SCHEDULED
    assert draft.post is not None
    assert draft.post.status == PostStatus.SCHEDULED
    assert draft.post.source == PostSource.AUTOPILOT


def test_auto_approve_without_a_connected_account_falls_back_to_the_queue(
    autopilot_config: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    """The drafts are generated and paid for either way, so they land in the
    queue rather than being lost to a Phase 9 precondition."""
    from channels.models import SocialAccount

    SocialAccount.objects.all().delete()
    autopilot_workspace.plan = plans["advanced"]
    autopilot_workspace.save(update_fields=["plan"])
    autopilot_config.auto_approve = True
    autopilot_config.landing = AutopilotLanding.AUTO_CALENDAR
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.QUEUED
    assert job.detail["reason"] == "no_connected_accounts"
    assert job.detail["drafts_scheduled"] == 0
    assert AutopilotDraft.objects.get().status == AutopilotDraftStatus.PENDING


def test_auto_approve_is_ignored_when_the_plan_no_longer_allows_it(
    autopilot_config: Any,
) -> None:
    """The write-time gate ran on Advanced; the plan is Pro by the time the
    engine acts. The run-time re-check is the one that decides (I5's shape)."""
    autopilot_config.auto_approve = True
    autopilot_config.landing = AutopilotLanding.AUTO_CALENDAR
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.QUEUED
    assert AutopilotDraft.objects.get().status == AutopilotDraftStatus.PENDING


def test_approving_a_draft_creates_and_schedules_its_post(autopilot_config: Any) -> None:
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    autopilot.run_config(autopilot_config)
    draft = AutopilotDraft.objects.get()

    autopilot.approve_draft(draft)

    post = Post.objects.get()
    assert post.master_body == draft.caption
    assert post.product_id == autopilot_config.product_id
    assert post.generation_id == draft.generation_id
    assert post.scheduled_at == draft.scheduled_for
    assert post.ordered_media()
    draft.refresh_from_db()
    assert draft.status == AutopilotDraftStatus.SCHEDULED


def test_a_draft_cannot_be_acted_on_twice(autopilot_config: Any) -> None:
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    autopilot.run_config(autopilot_config)
    draft = autopilot.reject_draft(AutopilotDraft.objects.get())

    with pytest.raises(autopilot.AutopilotNotConfigurableError):
        autopilot.reject_draft(draft)
    with pytest.raises(autopilot.AutopilotNotConfigurableError):
        autopilot.approve_draft(draft)


def test_a_rejected_slot_is_retired_not_redrafted(autopilot_config: Any) -> None:
    """The user said no to that slot, not to that attempt — re-drafting it
    would spend the credits again on something already refused."""
    autopilot_config.cadence_days = 2
    autopilot_config.lookahead_days = 4
    autopilot_config.save()
    start = timezone.now()
    with time_machine.travel(start, tick=False):
        autopilot.run_config(autopilot_config)
    autopilot.reject_draft(AutopilotDraft.objects.order_by("scheduled_for")[0])
    spent = CreditLedger.objects.filter(delta__lt=0).count()

    with time_machine.travel(start + dt.timedelta(hours=1), tick=False):
        autopilot.run_config(autopilot_config, now=timezone.now())

    slots = list(
        AutopilotDraft.objects.filter(product=autopilot_config.product)
        .order_by("scheduled_for")
        .values_list("scheduled_for", "status")
    )
    assert len(slots) == 2
    assert slots[0][1] == AutopilotDraftStatus.REJECTED
    assert CreditLedger.objects.filter(delta__lt=0).count() == spent


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
def test_queue_returns_pending_drafts_for_this_workspace_only(
    auth_client: Any, autopilot_config: Any
) -> None:
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    autopilot.run_config(autopilot_config)

    body = auth_client.get("/api/v1/autopilot/queue/").json()

    assert len(body) == 1
    assert body[0]["status"] == AutopilotDraftStatus.PENDING
    assert body[0]["media"]["url"]
    assert body[0]["product_name"] == autopilot_config.product.name


def test_queue_is_gated_to_plans_with_autopilot(
    auth_client: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    autopilot_workspace.plan = plans["free"]
    autopilot_workspace.save(update_fields=["plan"])

    response = auth_client.get("/api/v1/autopilot/queue/")

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_available"


def test_approve_and_reject_endpoints_move_the_draft(
    auth_client: Any, autopilot_config: Any
) -> None:
    autopilot_config.cadence_days = 2
    autopilot_config.lookahead_days = 4
    autopilot_config.save()
    autopilot.run_config(autopilot_config)
    first, second = AutopilotDraft.objects.order_by("scheduled_for")

    approved = auth_client.post(f"/api/v1/autopilot/{first.pk}/approve/").json()
    rejected = auth_client.post(f"/api/v1/autopilot/{second.pk}/reject/").json()

    assert approved["status"] == AutopilotDraftStatus.SCHEDULED
    assert approved["post"] is not None
    assert rejected["status"] == AutopilotDraftStatus.REJECTED


def test_another_workspaces_draft_is_a_404_not_a_403(
    auth_client: Any, autopilot_config: Any, plans: dict[str, Any], make_png_upload: Any
) -> None:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    other_owner = get_user_model().objects.create_user(email="other@example.com", password="x")
    other = provision_workspace(other_owner, name="Someone Else")
    other.plan = plans["pro"]
    other.save(update_fields=["plan"])
    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()
    autopilot.run_config(autopilot_config)
    draft = AutopilotDraft.objects.get()

    from rest_framework.test import APIClient

    intruder = APIClient()
    intruder.force_authenticate(other_owner)

    assert intruder.post(f"/api/v1/autopilot/{draft.pk}/approve/").status_code == 404


def test_the_config_endpoint_creates_defaults_on_first_read(
    auth_client: Any, autopilot_product: Any
) -> None:
    """So the product page always has something to render."""
    from products.models import AutopilotConfig

    AutopilotConfig.objects.all().delete()

    body = auth_client.get(f"/api/v1/products/{autopilot_product.pk}/autopilot/").json()

    assert body["enabled"] is False
    assert body["product"] == autopilot_product.pk
    assert AutopilotConfig.objects.filter(product=autopilot_product).exists()


def test_the_config_endpoint_retunes_the_cadence(
    auth_client: Any, autopilot_product: Any, autopilot_config: Any
) -> None:
    url = f"/api/v1/products/{autopilot_product.pk}/autopilot/"

    body = auth_client.patch(
        url, {"enabled": True, "cadence_days": 7, "latitude": 90}, format="json"
    ).json()

    assert (body["enabled"], body["cadence_days"], body["latitude"]) == (True, 7, 90)


# -----------------------------------------------------------------------------
# The Beat wrapper
# -----------------------------------------------------------------------------
def test_the_beat_task_runs_the_scan(autopilot_config: Any) -> None:
    from products.tasks import run_due_autopilot

    autopilot_config.lookahead_days = 3
    autopilot_config.cadence_days = 3
    autopilot_config.save()

    assert run_due_autopilot() == 1


# -----------------------------------------------------------------------------
# The ledger split I3 rests on
# -----------------------------------------------------------------------------
def test_included_balance_excludes_purchases(workspace: Any) -> None:
    ledger.grant_video_units(workspace, 4)
    ledger.purchase_video_units(workspace, 10, note="pack")

    assert ledger.video_balance(workspace) == 14
    assert ledger.included_video_balance(workspace) == 4


def test_included_balance_is_consumed_before_the_pack(workspace: Any) -> None:
    ledger.grant_video_units(workspace, 4)
    ledger.purchase_video_units(workspace, 10, note="pack")

    ledger.debit_video_units(workspace, 6, quota=4)

    assert ledger.video_balance(workspace) == 8
    assert ledger.included_video_balance(workspace) == 0


def test_the_monthly_reset_only_takes_back_unspent_allowance(workspace: Any) -> None:
    """A workspace granted 4 that spent 3 has one included unit to expire, not
    four — expiring four would silently eat three units of pack."""
    ledger.grant_video_units(workspace, 4)
    ledger.purchase_video_units(workspace, 10, note="pack")
    ledger.debit_video_units(workspace, 3, quota=4)

    ledger.grant_video_units(workspace, 4, note="next period")

    assert ledger.video_balance(workspace) == 14
    assert ledger.included_video_balance(workspace) == 4


def test_unlimited_plans_report_unlimited_included_videos(
    workspace: Any, plans: dict[str, Any]
) -> None:
    from billing.models import UNLIMITED
    from billing.services.entitlements import entitlements_for

    plan = plans["advanced"]
    plan.included_videos = UNLIMITED
    plan.save(update_fields=["included_videos"])
    workspace.plan = plan
    workspace.save(update_fields=["plan"])

    assert entitlements_for(workspace).included_video_units_remaining() == UNLIMITED


def test_unlimited_video_allowance_never_blocks_the_run(
    autopilot_config: Any, autopilot_workspace: Any, plans: dict[str, Any]
) -> None:
    from billing.models import UNLIMITED

    plan = plans["advanced"]
    plan.included_videos = UNLIMITED
    plan.save(update_fields=["included_videos"])
    autopilot_workspace.plan = plan
    autopilot_workspace.save(update_fields=["plan"])
    autopilot_config.format_mix = {"VIDEO": 1}
    autopilot_config.cadence_days = 1
    autopilot_config.lookahead_days = 5
    autopilot_config.save()

    job = autopilot.run_config(autopilot_config)

    assert job.status == AutopilotJobStatus.QUEUED
    assert job.detail["videos_deferred"] == 5
    assert not VideoLedger.objects.filter(reason=VideoReason.GENERATION).exists()


def test_autopilot_mode_is_not_directly_creatable(autopilot_workspace: Any) -> None:
    """Reachable only through the engine, the same way `REVISION` is reachable
    only through `ai.services.revisions` (design.md A69)."""
    from ai.services import pipeline

    assert GenerationMode.AUTOPILOT in pipeline.ALLOWED_MODES
    assert GenerationMode.AUTOPILOT not in pipeline.DIRECTLY_CREATABLE_MODES
