"""Plan model and seed (design.md §4.1, I6, I8, D13)."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from billing.models import UNLIMITED, Plan

pytestmark = pytest.mark.django_db


def test_seed_matches_the_plan_matrix(plans) -> None:
    free, pro, advanced = plans["free"], plans["pro"], plans["advanced"]

    assert (free.price_monthly_cents, pro.price_monthly_cents, advanced.price_monthly_cents) == (
        0,
        3700,
        9700,
    )
    assert (pro.price_annual_cents, advanced.price_annual_cents) == (34800, 92400)
    assert (free.monthly_ai_credits, pro.monthly_ai_credits, advanced.monthly_ai_credits) == (
        30,
        150,
        400,
    )
    assert (free.included_videos, pro.included_videos, advanced.included_videos) == (0, 4, 12)
    assert (free.max_social_accounts, pro.max_social_accounts, advanced.max_social_accounts) == (
        1,
        5,
        10,
    )


def test_free_tier_cannot_auto_publish_or_generate_video(plans) -> None:
    """D4: publishing is per-account COGS, so free users must not incur it."""
    free = plans["free"]
    assert free.feature("auto_publish") is False
    assert free.feature("video_generation") is False
    assert free.included_videos == 0


def test_approval_workflow_and_api_access_are_advanced_only(plans) -> None:
    assert plans["pro"].feature("approval_workflow") is False
    assert plans["advanced"].feature("approval_workflow") is True
    assert plans["pro"].feature("api_access") is False
    assert plans["advanced"].feature("api_access") is True


def test_seed_is_idempotent(plans) -> None:
    call_command("seed_plans", verbosity=0)
    assert Plan.objects.count() == 3


def test_social_account_cap_never_unlimited(plans) -> None:
    """I6 — every connected account is per-seat COGS with the publishing
    provider, so an unbounded cap makes one workspace arbitrarily expensive."""
    advanced = plans["advanced"]
    advanced.max_social_accounts = UNLIMITED

    with pytest.raises(ValidationError) as excinfo:
        advanced.save()
    assert "max_social_accounts" in excinfo.value.message_dict


def test_other_quotas_may_be_unlimited(plans) -> None:
    assert plans["advanced"].max_scheduled_posts == UNLIMITED
    assert plans["advanced"].max_products == UNLIMITED


def test_plan_code_immutable_after_creation(plans) -> None:
    """D13 — the code is the join key for Stripe mapping and analytics."""
    pro = plans["pro"]
    pro.code = "pro-v2"

    with pytest.raises(ValidationError) as excinfo:
        pro.save()
    assert "code" in excinfo.value.message_dict


def test_unknown_feature_keys_are_rejected(plans) -> None:
    """A typo'd flag would read as False and silently disable a gate."""
    pro = plans["pro"]
    pro.features = {**pro.features, "playbok": True}

    with pytest.raises(ValidationError) as excinfo:
        pro.save()
    assert "features" in excinfo.value.message_dict
