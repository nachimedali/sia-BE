"""Seeds the three plans with the design.md §4.1 matrix.

Idempotent: re-running updates values in place rather than duplicating rows, so
it is safe on every deploy. Operators may edit any of these in admin afterwards
(I8) — this command only establishes the starting point.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import Currency, Plan, PlanPrice

#: Whole pages a plan may open (2026-10-26, owner): Spark is locked out of
#: campaigns, refrains (templates and recurrence) and the analytics page; every
#: other plan, legacy rows included, keeps them. Absent reads as "not in the
#: plan", so each plan states them.
_PAGES_INCLUDED: dict[str, Any] = {"campaigns": True, "templates": True, "analytics": True}

# Values are the §4.1 matrix verbatim. This module and the migrations are the
# only places these numbers may appear (I8 / implementation.md §4.2).
PLANS: list[dict[str, Any]] = [
    {
        # L-4: Free-forever is replaced by a **quota trial** — one workspace,
        # N posts, no expiry, no card. A real seeded row rather than a special
        # case in code, so an operator retunes the quota in admin like every
        # other commercial number (Part 7 rule 10).
        #
        # `free` survives below it, unchanged, because existing accounts sit on
        # it and migrating them is a commercial decision, not a seed.
        "code": "trial",
        "display_name": "Trial",
        # Retired from the public list by the 2026-10-21 packages (Spark is
        # the free start now); the row stays for the accounts that sit on it.
        "is_public": False,
        "tagline": "Six posts to see whether this works for you. No card.",
        "price_monthly_cents": 0,
        "price_annual_cents": 0,
        "sort_order": -1,
        "monthly_ai_credits": 30,
        "included_videos": 0,
        "max_social_accounts": 1,
        "max_autopublish_posts": 0,
        "max_scheduled_posts": 6,
        "scheduling_horizon_days": 30,
        "max_workspace_members": 1,
        "max_products": 1,
        # No clock. The quota *is* the trial (L-4), which is why this is 0 and
        # `trial_post_quota` is not.
        "trial_days": 0,
        "trial_post_quota": 6,
        "max_workspaces": 1,
        # --- planning surfaces (Phase 3, P3-03/P3-04/P3-10) ---------------
        # Columns landed in Phase 0 and sat at their model defaults until
        # Phase 3 read them. `max_campaigns: 0` meant *nobody could create a
        # campaign* the moment the quota check went live, which is what
        # seeding these is fixing. Admin-editable like every other commercial
        # number here (Part 7 rule 10).
        "counts_docs_against_quota": False,
        "max_campaigns": 1,
        "max_tracked_competitors": 0,
        "max_labels": 5,
        "included_views": 2,
        "price_per_workspace_cents": 0,
        "features": {
            **_PAGES_INCLUDED,
            "trend_engine": False,
            "repurposing": False,
            "playbook": False,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "video_generation": False,
            "autopilot": False,
            "autopilot_auto_approve": False,
            # REMINDER is not a feature flag and never was — it is the delivery
            # mode every plan gets (L-4, P0-22). This is auto-publish only.
            "auto_publish": False,
            "analytics_history_days": 7,
            "credits_rollover": False,
            "reply_to_comments": False,
        },
        "comment_capture_interval_minutes": 1440,
        "reaction_detail": "TOTAL",
    },
    {
        "code": "free",
        "display_name": "Free",
        # L-4: the quota trial replaced free-forever on the public page. The
        # row stays for the accounts that sit on it.
        "is_public": False,
        "tagline": "Plan and get reminded. No card required.",
        "price_monthly_cents": 0,
        "price_annual_cents": 0,
        "sort_order": 0,
        "monthly_ai_credits": 30,
        "included_videos": 0,
        "max_social_accounts": 1,
        "max_autopublish_posts": 0,
        "max_scheduled_posts": 10,
        "scheduling_horizon_days": 7,
        "max_workspace_members": 1,
        "max_products": 1,
        "trial_days": 0,
        # --- planning surfaces (Phase 3, P3-03/P3-04/P3-10) ---------------
        # Columns landed in Phase 0 and sat at their model defaults until
        # Phase 3 read them. `max_campaigns: 0` meant *nobody could create a
        # campaign* the moment the quota check went live, which is what
        # seeding these is fixing. Admin-editable like every other commercial
        # number here (Part 7 rule 10).
        "counts_docs_against_quota": False,
        "max_campaigns": 2,
        "max_tracked_competitors": 3,
        "max_labels": 10,
        "included_views": 3,
        "features": {
            **_PAGES_INCLUDED,
            "trend_engine": False,
            "repurposing": False,
            "playbook": False,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "video_generation": False,
            "autopilot": False,
            "autopilot_auto_approve": False,
            "auto_publish": False,
            "analytics_history_days": 7,
            "credits_rollover": False,
            "reply_to_comments": False,
        },
        # L-4a: daily capture, totals only, no reply. Reading is free from the
        # provider, so the ladder is freshness and depth, not cost.
        "comment_capture_interval_minutes": 1440,
        "reaction_detail": "TOTAL",
    },
    {
        "code": "pro",
        "display_name": "Pro",
        # Retired from the public list by the 2026-10-21 packages; kept for
        # the subscriptions that point at it.
        "is_public": False,
        "tagline": "Auto-publish, trends and autopilot.",
        "price_monthly_cents": 3700,
        "price_annual_cents": 34800,
        # Priced per workspace, tiered above `max_workspaces` (L-4, P0-17).
        # The subscription quantity is the workspace count.
        "price_per_workspace_cents": 3700,
        "max_workspaces": 3,
        "sort_order": 1,
        # --- planning surfaces (Phase 3, P3-03/P3-04/P3-10) ---------------
        # Columns landed in Phase 0 and sat at their model defaults until
        # Phase 3 read them. `max_campaigns: 0` meant *nobody could create a
        # campaign* the moment the quota check went live, which is what
        # seeding these is fixing. Admin-editable like every other commercial
        # number here (Part 7 rule 10).
        "counts_docs_against_quota": False,
        "max_campaigns": 20,
        "max_tracked_competitors": 10,
        "max_labels": 40,
        "included_views": 20,
        "monthly_ai_credits": 150,
        "included_videos": 4,
        "max_social_accounts": 5,
        "max_autopublish_posts": 500,
        "max_scheduled_posts": 500,
        "scheduling_horizon_days": 180,
        "max_workspace_members": 3,
        "max_products": 10,
        "trial_days": 7,
        "features": {
            **_PAGES_INCLUDED,
            "trend_engine": True,
            "repurposing": True,
            "playbook": True,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "video_generation": True,
            "autopilot": True,
            "autopilot_auto_approve": False,
            "auto_publish": True,
            "analytics_history_days": 90,
            "credits_rollover": False,
            "reply_to_comments": False,
        },
        # L-4a: every six hours, per-type breakdown, still no reply.
        "comment_capture_interval_minutes": 360,
        "reaction_detail": "PER_TYPE",
    },
    {
        "code": "advanced",
        "display_name": "Advanced",
        # Retired from the public list by the 2026-10-21 packages; kept for
        # the subscriptions that point at it.
        "is_public": False,
        "tagline": "Approval chains, API access and the most video.",
        "price_monthly_cents": 9700,
        "price_annual_cents": 92400,
        "price_per_workspace_cents": 9700,
        "max_workspaces": 10,
        "sort_order": 2,
        # --- planning surfaces (Phase 3, P3-03/P3-04/P3-10) ---------------
        # Columns landed in Phase 0 and sat at their model defaults until
        # Phase 3 read them. `max_campaigns: 0` meant *nobody could create a
        # campaign* the moment the quota check went live, which is what
        # seeding these is fixing. Admin-editable like every other commercial
        # number here (Part 7 rule 10).
        "counts_docs_against_quota": False,
        "max_campaigns": -1,
        "max_tracked_competitors": -1,
        "max_labels": -1,
        "included_views": -1,
        "monthly_ai_credits": 400,
        "included_videos": 12,
        "max_social_accounts": 10,
        "max_autopublish_posts": 5000,
        # "unlimited" everywhere it is allowed; never for social accounts (I6).
        "max_scheduled_posts": -1,
        "scheduling_horizon_days": -1,
        "max_workspace_members": 25,
        "max_products": -1,
        "trial_days": 7,
        "features": {
            **_PAGES_INCLUDED,
            "trend_engine": True,
            "repurposing": True,
            "playbook": True,
            "approval_workflow": True,
            "approval_chain_depth": 10,
            "api_access": True,
            "video_generation": True,
            "autopilot": True,
            # L-2: nothing publishes without a human, on any plan. The flag
            # stays in the matrix, false everywhere, so no plan can sell it.
            "autopilot_auto_approve": False,
            "auto_publish": True,
            "analytics_history_days": 730,
            "credits_rollover": False,
            "reply_to_comments": True,
        },
        # L-4a: `0` means webhook-driven, not "poll constantly" — the provider
        # caches both read endpoints for ten minutes and says not to poll.
        "comment_capture_interval_minutes": 0,
        "reaction_detail": "REACTORS",
    },
]


# The packages of 2026-10-21 (pricing.audit.md §7, pricing.plan.md Phase 2),
# and Discovery below Essential (2026-10-27).
# Kept out of `PLANS` for the reason `PRESENTATION` is: `billing/0018` imports
# that list and creates rows from it before `is_default` exists (`0021`).
# `billing/0022` creates these on databases that already ran 0018.
#
# One credit sells for $0.10 (the `credits-100` pack): a task costs
# `ceil(cost / (0.10 * 0.3))` credits, so AI cost stays under 30% of what a
# credit sells for. Monthly credits are sized to each package's cadence
# (1, 3, 6 and 12 posts a week, from the GooseWorks calendar rules) and cover
# its videos — a clip render debits credits (`ai.services.video`). With the S17
# flag on, renders debit video units instead, and `included_videos` is the
# monthly allowance (4 / 9 / 17, `avatar_upgrade/cost_analysis.md`).
_PAID_FEATURES: dict[str, Any] = {
    **_PAGES_INCLUDED,
    "trend_engine": True,
    "repurposing": True,
    "playbook": True,
    "video_generation": True,
    "auto_publish": True,
    "autopilot_auto_approve": False,
    "credits_rollover": False,
}

PACKAGES: list[dict[str, Any]] = [
    {
        "code": "spark",
        "display_name": "Spark",
        "tagline": "One post a week, planned and reminded. Free, no card.",
        "is_default": True,
        "price_monthly_cents": 0,
        "price_annual_cents": 0,
        "price_per_workspace_cents": 0,
        "sort_order": 10,
        "monthly_ai_credits": 60,
        "included_videos": 0,
        "max_social_accounts": 1,
        "max_autopublish_posts": 0,
        "max_scheduled_posts": 4,
        "scheduling_horizon_days": 30,
        "max_workspace_members": 1,
        "max_products": 1,
        "trial_days": 0,
        "trial_post_quota": 0,
        "max_workspaces": 1,
        "counts_docs_against_quota": False,
        "max_campaigns": 1,
        "max_tracked_competitors": 0,
        "max_labels": 5,
        "included_views": 2,
        "features": {
            # Spark: the free package opens none of the paid pages.
            "campaigns": False,
            "templates": False,
            "analytics": False,
            "trend_engine": False,
            "repurposing": False,
            "playbook": False,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "video_generation": False,
            # Four 5-second clips per organization, ever, free (`ai.services.video`).
            "free_video_clips": 4,
            "free_video_max_seconds": 5,
            "autopilot": False,
            "autopilot_auto_approve": False,
            "auto_publish": False,
            "analytics_history_days": 7,
            "credits_rollover": False,
            "reply_to_comments": False,
            "ad_templates": True,
            "ad_templates_people": False,
            "ad_templates_talking": False,
            "max_avatars": 0,
            "avatar_drafts_monthly": 0,
            "ad_rerolls": 1,
            "max_references": 1,
            "ad_templates_premium": False,
            "ad_avatars_per_ad": 1,
            "ad_products_per_ad": 1,
            "real_avatars": False,
        },
        "comment_capture_interval_minutes": 1440,
        "reaction_detail": "TOTAL",
        "support_response": "2 working days",
    },
    {
        # 2026-10-27, owner (zeely-comparison.md): a paid first step, so a
        # brand tries publishing and video before Essential. Five 720p units,
        # two channels; no trends, one seat and 30 days of history keep
        # Essential the bigger step. `billing/0026` creates it.
        "code": "discovery",
        "display_name": "Discovery",
        "tagline": "Two posts a week on two channels, with five short videos.",
        "price_monthly_cents": 2900,
        "price_annual_cents": 29000,
        "price_per_workspace_cents": 2900,
        "sort_order": 11,
        "monthly_ai_credits": 200,
        "included_videos": 5,
        "max_social_accounts": 2,
        "max_autopublish_posts": 30,
        "max_scheduled_posts": 50,
        "scheduling_horizon_days": 60,
        "max_workspace_members": 1,
        "max_products": 2,
        "trial_days": 7,
        "trial_post_quota": 0,
        "max_workspaces": 1,
        "counts_docs_against_quota": False,
        "max_campaigns": 2,
        "max_tracked_competitors": 0,
        "max_labels": 5,
        "included_views": 3,
        "features": {
            **_PAID_FEATURES,
            "trend_engine": False,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "autopilot": False,
            "analytics_history_days": 30,
            "reply_to_comments": False,
            "ad_templates": True,
            "ad_templates_people": True,
            "ad_templates_talking": False,
            "max_avatars": 1,
            "avatar_drafts_monthly": 1,
            "ad_rerolls": 2,
            "max_references": 2,
            "ad_templates_premium": False,
            "ad_avatars_per_ad": 1,
            "ad_products_per_ad": 1,
            "real_avatars": False,
        },
        "comment_capture_interval_minutes": 1440,
        "reaction_detail": "TOTAL",
        "support_response": "2 working days",
    },
    {
        "code": "essential",
        "display_name": "Essential",
        "tagline": "Three posts a week on three channels, with short videos.",
        "price_monthly_cents": 4900,
        "price_annual_cents": 49000,
        "price_per_workspace_cents": 4900,
        "sort_order": 12,
        "monthly_ai_credits": 500,
        "included_videos": 4,
        "max_social_accounts": 3,
        "max_autopublish_posts": 60,
        "max_scheduled_posts": 100,
        "scheduling_horizon_days": 90,
        "max_workspace_members": 2,
        "max_products": 3,
        "trial_days": 7,
        "trial_post_quota": 0,
        "max_workspaces": 1,
        "counts_docs_against_quota": False,
        "max_campaigns": 5,
        "max_tracked_competitors": 0,
        "max_labels": 10,
        "included_views": 5,
        "features": {
            **_PAID_FEATURES,
            "approval_workflow": False,
            "approval_chain_depth": 1,
            "api_access": False,
            "autopilot": False,
            "analytics_history_days": 90,
            "reply_to_comments": False,
            "ad_templates": True,
            "ad_templates_people": True,
            "ad_templates_talking": False,
            "max_avatars": 1,
            "avatar_drafts_monthly": 2,
            "ad_rerolls": 3,
            "max_references": 2,
            "ad_templates_premium": False,
            "ad_avatars_per_ad": 1,
            "ad_products_per_ad": 2,
            "real_avatars": False,
        },
        "comment_capture_interval_minutes": 1440,
        "reaction_detail": "TOTAL",
        "support_response": "1 working day",
    },
    {
        "code": "growth",
        "display_name": "Growth",
        "tagline": "Six posts a week on five channels, autopilot and 1080p video.",
        "is_recommended": True,
        "price_monthly_cents": 9900,
        "price_annual_cents": 99000,
        "price_per_workspace_cents": 9900,
        "sort_order": 13,
        "monthly_ai_credits": 1100,
        "included_videos": 9,
        "max_social_accounts": 5,
        "max_autopublish_posts": 150,
        "max_scheduled_posts": 300,
        "scheduling_horizon_days": 180,
        "max_workspace_members": 5,
        "max_products": 10,
        "trial_days": 7,
        "trial_post_quota": 0,
        "max_workspaces": 3,
        "counts_docs_against_quota": False,
        "max_campaigns": 20,
        "max_tracked_competitors": 10,
        "max_labels": 40,
        "included_views": 20,
        "features": {
            **_PAID_FEATURES,
            "approval_workflow": True,
            "approval_chain_depth": 2,
            "api_access": False,
            "autopilot": True,
            "analytics_history_days": 365,
            "reply_to_comments": False,
            "ad_templates": True,
            "ad_templates_people": True,
            "ad_templates_talking": True,
            "max_avatars": 3,
            "avatar_drafts_monthly": 6,
            "ad_rerolls": 5,
            "max_references": 4,
            "ad_templates_premium": True,
            "ad_avatars_per_ad": 2,
            "ad_products_per_ad": 3,
            "real_avatars": False,
        },
        "comment_capture_interval_minutes": 360,
        "reaction_detail": "PER_TYPE",
        "support_response": "1 working day",
    },
    {
        "code": "scale",
        "display_name": "Scale",
        "tagline": "Twelve posts a week, every channel, approval chains and replies.",
        "price_monthly_cents": 24900,
        "price_annual_cents": 249000,
        "price_per_workspace_cents": 24900,
        "sort_order": 14,
        "monthly_ai_credits": 2200,
        "included_videos": 17,
        "max_social_accounts": 10,
        "max_autopublish_posts": 500,
        "max_scheduled_posts": -1,
        "scheduling_horizon_days": -1,
        "max_workspace_members": 15,
        "max_products": -1,
        "trial_days": 7,
        "trial_post_quota": 0,
        "max_workspaces": 10,
        "counts_docs_against_quota": False,
        "max_campaigns": -1,
        "max_tracked_competitors": -1,
        "max_labels": -1,
        "included_views": -1,
        "features": {
            **_PAID_FEATURES,
            "approval_workflow": True,
            "approval_chain_depth": 10,
            "api_access": True,
            "autopilot": True,
            "analytics_history_days": 730,
            "reply_to_comments": True,
            "ad_templates": True,
            "ad_templates_people": True,
            "ad_templates_talking": True,
            "max_avatars": 10,
            "avatar_drafts_monthly": 20,
            "ad_rerolls": 10,
            "max_references": 6,
            "ad_templates_premium": True,
            "ad_avatars_per_ad": 3,
            "ad_products_per_ad": 5,
            "real_avatars": True,
        },
        # `0` is webhook-driven (L-4a), as on Advanced.
        "comment_capture_interval_minutes": 0,
        "reaction_detail": "REACTORS",
        "support_response": "Same working day",
    },
]


# How each plan presents itself on the pricing and help pages (P4, P8). Kept
# out of `PLANS` on purpose: `billing/0018` imports that list and creates rows
# from it before these columns exist (`0020`), so a key added there would break
# every fresh database at 0018.
PRESENTATION: dict[str, dict[str, Any]] = {
    "trial": {"support_response": "2 working days"},
    "free": {"support_response": "2 working days"},
    "pro": {"support_response": "1 working day"},
    "advanced": {"support_response": "Same working day"},
}


class Command(BaseCommand):
    help = "Seed or update the plans: the legacy rows and the 2026-10-21 packages."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for spec in [*PLANS, *PACKAGES]:
            code = spec["code"]
            plan, created = Plan.objects.update_or_create(
                code=code,
                defaults={k: v for k, v in spec.items() if k != "code"}
                | PRESENTATION.get(code, {}),
            )
            self._seed_default_price(plan)
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {plan.code} ({plan.display_name})")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PLANS) + len(PACKAGES)} plans."))

    @staticmethod
    def _seed_default_price(plan: Plan) -> None:
        """The default price row, mirroring the columns above.

        Only the default. Every other currency is an operator's decision — a
        seed command that invented a euro price would be guessing at a number
        it has no basis for, and a wrong price is worse than an absent one
        because the absent one falls back to the default and the wrong one
        just charges.
        """
        currency, _ = Currency.objects.get_or_create(
            code=plan.currency.upper(), defaults={"name": plan.currency.upper(), "symbol": "$"}
        )
        PlanPrice.objects.update_or_create(
            plan=plan,
            currency=currency,
            defaults={
                "monthly_cents": plan.price_monthly_cents,
                "annual_cents": plan.price_annual_cents,
                "per_workspace_cents": plan.price_per_workspace_cents,
                "stripe_price_id_monthly": plan.stripe_price_id_monthly,
                "stripe_price_id_annual": plan.stripe_price_id_annual,
                "is_default": True,
            },
        )
