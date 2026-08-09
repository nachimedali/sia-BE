"""Billing (design.md §6.8, §4.1, §8.2).

`Plan` shipped in Phase 2 because registration has to assign the Free plan
(A24). `Subscription` and the two ledgers land here in Phase 3.

Two invariants live in this module:

* **I8** — every quota is a row on `Plan`. No quota may be a code constant or a
  settings value, so an operator can retune a plan without a deploy.
* **I4** — the ledgers are append-only. Balance is `SUM(delta)`; `balance_after`
  is a per-row cache for display and reconciliation, never the source of truth.
  Enforced twice: no update or delete path exists in Python, and a Postgres
  trigger rejects both at the table. The model guard gives a readable error; the
  trigger is what holds when someone reaches for `.update()` or raw SQL.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

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


class CatalogueItem(models.Model):
    """What `Plan` and `Pack` have in common: a commercial row an operator
    retunes in admin (I8), joined to Stripe and to analytics by its `code`.

    The D13 immutability guard lives here rather than once per model for the
    same reason `LedgerEntry` holds the I4 append-only guard for both ledgers:
    a copy that gets missed fails silently — the code changes, and fulfilment
    stops finding the row without anything being raised.
    """

    code = models.SlugField(unique=True, help_text="Immutable after creation (D13).")
    display_name = models.CharField(max_length=64)
    tagline = models.CharField(max_length=200, blank=True)
    currency = models.CharField(max_length=3, default="USD")

    is_public = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering: ClassVar[list[str]] = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.display_name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        errors = self.validation_errors()
        if errors:
            raise ValidationError(errors)

    def validation_errors(self) -> dict[str, str]:
        """Collected rather than raised, so a subclass's rules and the shared
        ones are reported in one pass instead of one screen at a time."""
        errors: dict[str, str] = {}
        if self.pk:
            manager = type(self)._default_manager
            previous = manager.filter(pk=self.pk).values_list("code", flat=True).first()
            # D13: the code is the join key for Stripe mapping and analytics.
            if previous is not None and previous != self.code:
                errors["code"] = f"{type(self).__name__}.code is immutable after creation (D13)."
        return errors


class Plan(CatalogueItem):
    price_monthly_cents = models.IntegerField(default=0)
    price_annual_cents = models.IntegerField(default=0)

    stripe_price_id_monthly = models.CharField(max_length=64, blank=True)
    stripe_price_id_annual = models.CharField(max_length=64, blank=True)

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

    def validation_errors(self) -> dict[str, str]:
        errors = super().validation_errors()

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

        return errors

    def feature(self, key: str) -> Any:
        """Raw flag read. Callers should go through `Entitlements` instead —
        it applies the trial override and is the one place I5 gates from."""
        return self.features.get(key, False)


# The integer columns `Entitlements.quota()` may be asked for. Naming them makes
# a typo'd quota key a KeyError rather than a silent zero.
QUOTA_FIELDS = frozenset(
    {
        "monthly_ai_credits",
        "included_videos",
        "max_social_accounts",
        "max_autopublish_posts",
        "max_scheduled_posts",
        "scheduling_horizon_days",
        "max_workspace_members",
        "max_products",
    }
)


class PackKind(models.TextChoices):
    CREDITS = "CREDITS", "Credits"
    VIDEO = "VIDEO", "Video units"


class Pack(CatalogueItem):
    """A one-off top-up: credits, or prepaid video units (§4.3, D16).

    A row for the same reason a `Plan` is one (I8): the size of a pack and what
    it costs are commercial numbers an operator retunes, and a literal in code
    would keep working while quietly ignoring the edit.

    Packs are bought outright rather than billed in arrears — there is no path
    to a surprise invoice, and the balance simply stops.
    """

    kind = models.CharField(max_length=8, choices=PackKind.choices)
    units = models.IntegerField(help_text="Credits, or video units, granted on payment.")
    price_cents = models.IntegerField()

    stripe_price_id = models.CharField(max_length=64, blank=True)

    def validation_errors(self) -> dict[str, str]:
        errors = super().validation_errors()

        # UNLIMITED has no meaning for something you buy a fixed amount of, and
        # a pack of -1 units would credit a negative balance.
        if self.units < 1:
            errors["units"] = "A pack must grant at least one unit."
        if self.price_cents < 0:
            errors["price_cents"] = "A pack cannot have a negative price."

        return errors

    @property
    def unit_price_cents(self) -> int:
        """What one unit in this pack cost. Recorded on the ledger row so a
        revenue question does not have to reconstruct it from the pack."""
        return self.price_cents // self.units


class SubscriptionStatus(models.TextChoices):
    """Mirrors Stripe's subscription statuses — the webhook is the source of
    truth (implementation.md Phase 3.5), so inventing our own vocabulary would
    only add a mapping that can drift."""

    TRIALING = "TRIALING", "Trialing"
    ACTIVE = "ACTIVE", "Active"
    PAST_DUE = "PAST_DUE", "Past due"
    CANCELED = "CANCELED", "Canceled"
    INCOMPLETE = "INCOMPLETE", "Incomplete"
    UNPAID = "UNPAID", "Unpaid"

    @classmethod
    def live(cls) -> tuple[str, ...]:
        """Statuses that entitle the workspace to its paid plan."""
        return (cls.TRIALING, cls.ACTIVE, cls.PAST_DUE)


class Subscription(models.Model):
    """The billing record. `Workspace.plan` is what entitlements resolve from —
    this is what Stripe reconciles against, and the webhook keeps the two in
    step (design.md §8.1)."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=16, choices=SubscriptionStatus.choices, default=SubscriptionStatus.INCOMPLETE
    )

    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)

    stripe_subscription_id = models.CharField(max_length=64, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}: {self.plan_id} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in SubscriptionStatus.live()

    @classmethod
    def current_for(cls, workspace: Any) -> Subscription | None:
        """The subscription entitling this workspace, if any.

        Newest live row wins: a workspace that upgrades mid-cycle briefly has
        the old canceled row alongside the new one.
        """
        return (
            cls.objects.filter(workspace=workspace, status__in=SubscriptionStatus.live())
            .select_related("plan")
            .order_by("-created_at")
            .first()
        )


class AppendOnlyError(RuntimeError):
    """Raised when something tries to mutate a ledger row (I4)."""


class LedgerEntry(models.Model):
    """Append-only base for both ledgers (design.md §8.2, I4).

    `balance_after` is written at insert and never corrected. If it stops
    matching the running `SUM(delta)`, the nightly reconciliation task is
    supposed to be loud about it — that divergence means a bug in the debit
    path, and silently repairing it would hide the bug.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="%(class)s_entries"
    )
    delta = models.IntegerField(
        help_text="Signed. Zero is legitimate: unlimited plans still record usage."
    )
    balance_after = models.IntegerField()
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyError(
                f"{type(self).__name__} is append-only (I4); "
                "write a compensating row instead of updating this one."
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise AppendOnlyError(f"{type(self).__name__} is append-only (I4) and cannot be deleted.")


class CreditReason(models.TextChoices):
    MONTHLY_GRANT = "MONTHLY_GRANT", "Monthly grant"
    GENERATION = "GENERATION", "Generation"
    REFUND = "REFUND", "Refund"
    MANUAL_ADJUST = "MANUAL_ADJUST", "Manual adjustment"
    PURCHASE = "PURCHASE", "Purchase"
    TRIAL_GRANT = "TRIAL_GRANT", "Trial grant"


class CreditLedger(LedgerEntry):
    """Text and image spend. Video is counted separately (§4.3)."""

    reason = models.CharField(max_length=16, choices=CreditReason.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="credit_adjustments",
        help_text="Set for MANUAL_ADJUST so an operator action is attributable.",
    )
    # The row this one compensates. `generation` (design.md §6.8) arrives with
    # the model in Phase 7; until then this is what ties a refund to its debit,
    # which is what `test_refund_nets_to_zero` asserts on.
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )

    class Meta(LedgerEntry.Meta):
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["workspace", "reason"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}: {self.delta:+d} credits ({self.reason})"


class VideoReason(models.TextChoices):
    MONTHLY_GRANT = "MONTHLY_GRANT", "Monthly grant"
    GENERATION = "GENERATION", "Generation"
    REFUND = "REFUND", "Refund"
    PURCHASE = "PURCHASE", "Purchase"
    MANUAL_ADJUST = "MANUAL_ADJUST", "Manual adjustment"


class VideoLedger(LedgerEntry):
    """Video units, outside the credit pool (§4.3).

    Separate because the economics are different by an order of magnitude: one
    8s video is $1.20 of COGS against $0.0155 for a credit, so it gets its own
    allowance, its own overage price, and its own hard ceiling.
    """

    reason = models.CharField(max_length=16, choices=VideoReason.choices)
    unit_cost_cents = models.IntegerField(
        default=0,
        help_text=(
            "Per-unit money for this entry: COGS on a generation, price paid on a "
            "purchase. Overage is sold at cost + $2 (D16)."
        ),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="video_adjustments",
    )
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )

    class Meta(LedgerEntry.Meta):
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}: {self.delta:+d} video units ({self.reason})"


class StripeEvent(models.Model):
    """Every webhook Stripe has delivered, by its own id.

    Stripe retries on any non-2xx and can deliver the same event more than once
    regardless; without this, a replayed `invoice.payment_succeeded` would grant
    a second month of credits. `test_stripe_webhook_idempotent_on_replay`.
    """

    event_id = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.event_id})"
