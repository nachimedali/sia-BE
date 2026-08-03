"""Plans (design.md §6.8, §4.1).

Only `Plan` lands in Phase 2 — registration has to assign the Free plan, so the
table must exist. `Subscription`, the two ledgers, the entitlement resolver and
Stripe are Phase 3.

Invariant I8: every quota is a row here. No quota may be a code constant or a
settings value, so that an operator can retune a plan without a deploy.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.core.exceptions import ValidationError
from django.db import models

# A quota of -1 means "unlimited" and short-circuits the balance check.
UNLIMITED = -1

# design.md §4.1 — the feature flags a plan may carry. Unknown keys are rejected
# so a typo cannot silently disable a gate.
FEATURE_KEYS = frozenset(
    {
        "trend_engine",
        "repurposing",
        "playbook",
        "approval_workflow",
        "api_access",
        "video_generation",
        "autopilot",
        "autopilot_auto_approve",
        "auto_publish",
        "analytics_history_days",
        "credits_rollover",
    }
)


class Plan(models.Model):
    code = models.SlugField(unique=True, help_text="Immutable after creation (D13).")
    display_name = models.CharField(max_length=64)
    tagline = models.CharField(max_length=200, blank=True)

    price_monthly_cents = models.IntegerField(default=0)
    price_annual_cents = models.IntegerField(default=0)
    currency = models.CharField(max_length=3, default="USD")

    stripe_price_id_monthly = models.CharField(max_length=64, blank=True)
    stripe_price_id_annual = models.CharField(max_length=64, blank=True)

    is_public = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    monthly_ai_credits = models.IntegerField(default=0)
    included_videos = models.IntegerField(default=0)
    max_social_accounts = models.IntegerField(default=1)
    max_autopublish_posts = models.IntegerField(default=0)
    max_scheduled_posts = models.IntegerField(default=0)
    scheduling_horizon_days = models.IntegerField(default=7)
    max_workspace_members = models.IntegerField(default=1)
    max_products = models.IntegerField(default=1)
    trial_days = models.IntegerField(default=0)

    features = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.display_name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}

        # I6: "unlimited" is never permitted for social accounts. Every account
        # is per-seat COGS with the publishing provider, so an unbounded cap
        # would make one workspace arbitrarily expensive.
        if self.max_social_accounts == UNLIMITED:
            errors["max_social_accounts"] = (
                "max_social_accounts is a hard cap on every plan and may never be unlimited (I6)."
            )
        if self.max_social_accounts < 1:
            errors["max_social_accounts"] = "max_social_accounts must be at least 1."

        unknown = set(self.features) - FEATURE_KEYS
        if unknown:
            errors["features"] = f"Unknown feature keys: {', '.join(sorted(unknown))}."

        if self.pk:
            previous = Plan.objects.filter(pk=self.pk).values_list("code", flat=True).first()
            # D13: the code is the join key for Stripe mapping and analytics.
            if previous is not None and previous != self.code:
                errors["code"] = "Plan.code is immutable after creation (D13)."

        if errors:
            raise ValidationError(errors)

    def feature(self, key: str) -> Any:
        """Reads a feature flag. The full resolver arrives in Phase 3."""
        return self.features.get(key, False)
