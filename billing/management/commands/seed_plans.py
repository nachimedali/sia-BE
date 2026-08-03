"""Seeds the three plans with the design.md §4.1 matrix.

Idempotent: re-running updates values in place rather than duplicating rows, so
it is safe on every deploy. Operators may edit any of these in admin afterwards
(I8) — this command only establishes the starting point.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import Plan

# Values are the §4.1 matrix verbatim. This module and the migrations are the
# only places these numbers may appear (I8 / implementation.md §4.2).
PLANS: list[dict[str, Any]] = [
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
        },
    },
    {
        "code": "pro",
        "display_name": "Pro",
        "tagline": "Auto-publish, trends and autopilot.",
        "price_monthly_cents": 3700,
        "price_annual_cents": 34800,
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
        },
    },
    {
        "code": "advanced",
        "display_name": "Advanced",
        "tagline": "Approvals, API access and auto-approve autopilot.",
        "price_monthly_cents": 9700,
        "price_annual_cents": 92400,
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
        },
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
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {plan.code} ({plan.display_name})")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PLANS)} plans."))
