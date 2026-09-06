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
        "price_per_workspace_cents": 0,
        "features": {
            "trend_engine": False,
            "repurposing": False,
            "playbook": False,
            "approval_workflow": False,
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
        "features": {
            "trend_engine": False,
            "repurposing": False,
            "playbook": False,
            "approval_workflow": False,
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
        "tagline": "Auto-publish, trends and autopilot.",
        "price_monthly_cents": 3700,
        "price_annual_cents": 34800,
        # Priced per workspace, tiered above `max_workspaces` (L-4, P0-17).
        # The subscription quantity is the workspace count.
        "price_per_workspace_cents": 3700,
        "max_workspaces": 3,
        "sort_order": 1,
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
            "trend_engine": True,
            "repurposing": True,
            "playbook": True,
            "approval_workflow": False,
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
        "tagline": "Approvals, API access and auto-approve autopilot.",
        "price_monthly_cents": 9700,
        "price_annual_cents": 92400,
        "price_per_workspace_cents": 9700,
        "max_workspaces": 10,
        "sort_order": 2,
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
            "trend_engine": True,
            "repurposing": True,
            "playbook": True,
            "approval_workflow": True,
            "api_access": True,
            "video_generation": True,
            "autopilot": True,
            "autopilot_auto_approve": True,
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


class Command(BaseCommand):
    help = "Seed or update the three plans with the design.md §4.1 matrix."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for spec in PLANS:
            code = spec["code"]
            plan, created = Plan.objects.update_or_create(
                code=code, defaults={k: v for k, v in spec.items() if k != "code"}
            )
            self._seed_default_price(plan)
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {plan.code} ({plan.display_name})")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PLANS)} plans."))

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
