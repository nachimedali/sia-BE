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

from common.records import AppendOnly

# A quota of -1 means "unlimited" and short-circuits the balance check.
UNLIMITED = -1

# design.md §4.1 — the feature flags a plan may carry. Unknown keys are rejected
# so a typo cannot silently disable a gate.
FEATURE_KEYS = frozenset(
    {
        "trend_engine",
        "repurposing",
        "playbook",
        # C-02 made approval itself universal, so this no longer gates it.
        # What it still gates is the workspace-wide **audit log**, which is a
        # governance read rather than part of getting a post out.
        "approval_workflow",
        # P2-13: Advanced is re-pitched on chain *depth* — multi-level,
        # sequential, client-facing — not on approval existing. An integer
        # rather than a boolean so the ceiling is an admin edit (Part 7
        # rule 10), and 1 means "one stage", never "no approval".
        "approval_chain_depth",
        "api_access",
        "video_generation",
        "autopilot",
        "autopilot_auto_approve",
        "auto_publish",
        "analytics_history_days",
        "credits_rollover",
        # L-4a: replying to an audience comment from inside the app. Reading is
        # free from the provider; the gate is commercial, not technical.
        "reply_to_comments",
    }
)


class Currency(models.Model):
    """A currency this business will take money in.

    **A row rather than an enum**, so adding one is an admin edit rather than a
    deploy — the same rule every other commercial value here follows (I8, Part 7
    rule 10). A new market opens on a Tuesday; waiting for a release to price
    for it is the wrong constraint.

    `minor_units` is not decoration. Amounts are stored in the currency's
    smallest unit everywhere, and that unit is not always 1/100: JPY and KRW
    have none at all, so ¥3700 stored as "cents" would render as ¥37.00 and
    undercharge by a hundred. Formatting reads this; arithmetic never does.
    """

    #: ISO 4217, upper-case. The join key to Stripe, which speaks the same
    #: vocabulary — so it is immutable for the same reason `CatalogueItem.code`
    #: is, and for once we get the validation for free from the standard.
    code = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=64)
    symbol = models.CharField(max_length=8, blank=True)
    #: 2 for USD/EUR/MAD, 0 for JPY/KRW, 3 for a handful of dinars.
    minor_units = models.PositiveSmallIntegerField(default=2)
    #: Whether the symbol leads (`$12`) or trails (`12 €`). A display fact that
    #: differs by locale and is cheaper to store than to special-case.
    symbol_first = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["code"]
        verbose_name_plural = "currencies"

    def __str__(self) -> str:
        return f"{self.code} ({self.name})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.code = self.code.upper()
        super().save(*args, **kwargs)

    def format(self, minor: int) -> str:
        """`3700` in USD → `$37.00`; in JPY → `¥3700`.

        Rendering only. Nothing computes with the result, which is why rounding
        here cannot cost anyone money.
        """
        if self.minor_units == 0:
            amount = str(minor)
        else:
            major, remainder = divmod(abs(minor), 10**self.minor_units)
            amount = f"{'-' if minor < 0 else ''}{major}.{remainder:0{self.minor_units}d}"
        symbol = self.symbol or self.code
        return f"{symbol}{amount}" if self.symbol_first else f"{amount} {symbol}"


class CataloguePrice(models.Model):
    """What one catalogue item costs in one currency.

    **A row per currency, not a conversion.** €37 is not $37 through today's FX
    rate — it is a separate commercial decision, usually a rounder number, and
    often a different one relative to local buying power. Storing a base price
    and converting at read time would make every market's price a function of
    the dollar and move it every morning, which is not something anyone would
    choose to sell.

    Abstract for the same reason `LedgerEntry` and `CatalogueItem` are: a plan
    carries three amounts and two Stripe ids, a pack carries one of each, and
    forcing them into one table would mean half the columns are null in half
    the rows.
    """

    currency = models.ForeignKey("billing.Currency", on_delete=models.PROTECT, related_name="+")
    #: The one used when a customer's own currency is not offered for this item.
    #: Exactly one per item, enforced in `validation_errors`.
    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        errors = self.validation_errors()
        if errors:
            raise ValidationError(errors)

    def validation_errors(self) -> dict[str, str]:
        return {}


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


class ReactionDetail(models.TextChoices):
    """How much of a reaction breakdown a plan may see (L-4a).

    Reading costs nothing from the provider, so this is a product ladder, not
    a cost pass-through — and it degrades, it does not fail: a plan capped at
    `TOTAL` sees a real total, never a fabricated breakdown.
    """

    TOTAL = "TOTAL", "Total count only"
    PER_TYPE = "PER_TYPE", "Per-reaction-type breakdown"
    REACTORS = "REACTORS", "Per-type plus reactor identities"


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

    # --- organization tier (BUILD-PLAN L-1/L-4) ---
    #: The quota trial that replaced Free-forever: N posts, no expiry, no card.
    trial_post_quota = models.IntegerField(default=0)
    max_workspaces = models.IntegerField(default=1)
    price_per_workspace_cents = models.IntegerField(default=0)
    counts_docs_against_quota = models.BooleanField(default=True)
    included_views = models.IntegerField(default=0)
    max_labels = models.IntegerField(default=0)
    max_campaigns = models.IntegerField(default=0)
    #: How many competitor accounts a workspace may track (P6-08). A row like
    #: every other commercial number (Part 7 rule 10) — the ingest cost is one
    #: vendor call per tracked account per refresh, so this is genuinely what
    #: the plan is paying for.
    max_tracked_competitors = models.IntegerField(default=0)
    storage_bytes = models.BigIntegerField(default=0)
    version_history_days = models.IntegerField(default=0)

    # --- audience engagement (BUILD-PLAN L-4a) ---
    #: Minutes between audience-comment captures. `0` means this plan is
    #: driven by the `comment.received` webhook instead of a poll — the
    #: provider caches reads for ten minutes and says plainly not to poll them,
    #: so the top tier subscribes rather than tightening the interval.
    comment_capture_interval_minutes = models.IntegerField(default=1440)
    reaction_detail = models.CharField(
        max_length=16, choices=ReactionDetail.choices, default=ReactionDetail.TOTAL
    )

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
        "trial_post_quota",
        "max_workspaces",
        "included_views",
        "max_labels",
        "max_campaigns",
        "max_tracked_competitors",
        "version_history_days",
        "comment_capture_interval_minutes",
    }
)


class PlanPrice(CataloguePrice):
    """One plan's price in one currency (per-country pricing).

    The three amounts move together because they are one commercial decision:
    a market where the monthly price is lower is a market where the annual and
    the per-workspace price are too, and splitting them across rows would let
    them drift apart with nothing to notice.

    Stripe ids live here rather than on `Plan` because Stripe models this the
    same way — one product, one price object per currency — so a row here maps
    to a row there, and a mismatch is visible instead of inferred.
    """

    plan = models.ForeignKey("billing.Plan", on_delete=models.CASCADE, related_name="prices")

    monthly_cents = models.IntegerField(default=0)
    annual_cents = models.IntegerField(default=0)
    #: What the subscription quantity multiplies (P0-17): one subscription per
    #: organization, quantity = workspace count.
    per_workspace_cents = models.IntegerField(default=0)

    stripe_price_id_monthly = models.CharField(max_length=64, blank=True)
    stripe_price_id_annual = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["plan", "currency__code"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["plan", "currency"], name="unique_plan_price_currency"),
            # One default per plan, enforced by the database rather than by
            # whoever is editing: two defaults means the resolver picks by row
            # order, which is a bug that only shows up in one market.
            models.UniqueConstraint(
                fields=["plan"],
                condition=models.Q(is_default=True),
                name="one_default_price_per_plan",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plan_id} @ {self.currency_id}"

    def validation_errors(self) -> dict[str, str]:
        errors = super().validation_errors()
        for field in ("monthly_cents", "annual_cents", "per_workspace_cents"):
            if getattr(self, field) < 0:
                errors[field] = "A price cannot be negative."
        return errors


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


class PackPrice(CataloguePrice):
    """One pack's price in one currency.

    Note what is *not* here: `units`. A pack grants the same number of credits
    everywhere and only the price changes — varying both would make two markets
    incomparable in every revenue question anyone later asks.
    """

    pack = models.ForeignKey("billing.Pack", on_delete=models.CASCADE, related_name="prices")
    amount_cents = models.IntegerField(default=0)
    stripe_price_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["pack", "currency__code"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["pack", "currency"], name="unique_pack_price_currency"),
            models.UniqueConstraint(
                fields=["pack"],
                condition=models.Q(is_default=True),
                name="one_default_price_per_pack",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.pack_id} @ {self.currency_id}"

    def validation_errors(self) -> dict[str, str]:
        errors = super().validation_errors()
        if self.amount_cents < 0:
            errors["amount_cents"] = "A pack cannot have a negative price."
        return errors

    @property
    def unit_price_cents(self) -> int:
        """What one unit cost in this currency. Recorded on the ledger row so a
        revenue question does not have to reconstruct it from the pack."""
        return self.amount_cents // self.pack.units


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
    #: The base plan line. Quantity is always 1: this is the subscription
    #: itself, and it already covers `Plan.max_workspaces` workspaces.
    stripe_subscription_item_id = models.CharField(max_length=64, blank=True)
    #: The **overage** line, priced at `price_per_workspace_cents` (P0-17:
    #: "tiered above `Plan.max_workspaces`"). Its quantity is the number of
    #: workspaces *beyond* the included allowance, which is why it is a second
    #: item rather than a quantity on the line above: charging the base line
    #: per workspace would bill for the ones the plan already includes.
    stripe_overage_item_id = models.CharField(max_length=64, blank=True)
    #: Written alongside `workspace` during the org migration. Nullable until
    #: the backfill has run everywhere; nothing reads it during expand.
    organization = models.ForeignKey(
        "workspaces.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )

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


class LedgerEntry(AppendOnly):
    """Append-only base for both ledgers (design.md §8.2, I4).

    The guard itself lives in `common.records.AppendOnly`, shared with the
    analytics captures — same rule, same exception, different columns.

    `balance_after` is written at insert and never corrected. If it stops
    matching the running `SUM(delta)`, the nightly reconciliation task is
    supposed to be loud about it — that divergence means a bug in the debit
    path, and silently repairing it would hide the bug.
    """

    append_only_hint = "write a compensating row instead of updating this one (I4)."

    #: **Re-scoped to the organization** (L-1, P0-53): entitlement accounting
    #: pools at the company, so the balance is the org's. `workspace` is
    #: retained for *attribution* — which brand spent it — and is not the
    #: aggregation key after the cut-over.
    #:
    #: Nullable through the backfill. Append-only tables are re-scoped by
    #: adding a nullable FK and filling it with raw SQL in a data migration —
    #: the one sanctioned exception to "never rewrite", recorded in a
    #: `MigrationNote` row.
    organization = models.ForeignKey(
        "workspaces.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(class)s_entries",
    )
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
    # The row this one compensates — answers "which row does this reverse",
    # distinct from `generation` below (A32).
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )
    # design.md §6.8, lands now that Phase 7's model exists (A32). Answers
    # "which generations did this cost", which `reverses` cannot: one
    # generation can produce several ledger rows (a debit, then a refund).
    #
    # RESTRICT, not SET_NULL (design.md §15.8 A75): the append-only trigger
    # (A34) forbids UPDATE on this table unconditionally, so SET_NULL's
    # attempt to null this column out on a Generation delete would not
    # degrade gracefully — it would crash with a raw Postgres exception
    # instead of Django's own `RestrictedError`. RESTRICT still permits a
    # Generation to be deleted together with the ledger rows that reference
    # it, in the same collected delete, unlike PROTECT.
    generation = models.ForeignKey(
        "ai.Generation",
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="credit_ledger_entries",
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
    # RESTRICT — see the identical note on `CreditLedger.generation` (A75).
    generation = models.ForeignKey(
        "ai.Generation",
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="video_ledger_entries",
    )

    class Meta(LedgerEntry.Meta):
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}: {self.delta:+d} video units ({self.reason})"


class ReplyReason(models.TextChoices):
    OUTBOUND = "OUTBOUND", "Reply sent to an audience comment"
    MONTHLY_RESET = "MONTHLY_RESET", "Monthly allowance reset"
    MANUAL_ADJUST = "MANUAL_ADJUST", "Manual adjustment"


class ReplyLedger(LedgerEntry):
    """Outbound audience replies, counted against a **pooled org allowance**
    (L-4a, P0-36).

    Org-scoped from birth rather than workspace-scoped-then-migrated: it is a
    new table, so it can start where P0-53 is taking the other two rather than
    needing the same expand/backfill/contract dance later. `workspace` is
    retained for attribution — who spent it — but the *balance* is the org's,
    which is what stops one workspace spending another's headroom.

    Append-only like every other ledger. The free tranche is 10,000 outbound
    messages a month with metered overage beyond it; both numbers live on the
    plan row, never here (Part 7 rule 10).
    """

    reason = models.CharField(max_length=16, choices=ReplyReason.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reply_ledger_entries",
    )


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


class AddonStatus(models.TextChoices):
    TRIALING = "TRIALING", "Trialing"
    ACTIVE = "ACTIVE", "Active"
    CANCELLED = "CANCELLED", "Cancelled"
    #: Distinct from `CANCELLED`: a trial that ran out was never cancelled by
    #: anyone, and the two lead to different re-offers.
    EXPIRED = "EXPIRED", "Trial expired"


class OrganizationAddon(models.Model):
    """An org-level add-on with its own 30-day trial (BUILD-PLAN Phase 0).

    Separate from `Subscription` because the org carries **one** subscription
    whose quantity is its workspace count; add-ons are line items on it, each
    with an independent trial clock that `expire_trials` sweeps. Modelling them
    as extra subscriptions would make the quantity arithmetic ambiguous.
    """

    organization = models.ForeignKey(
        "workspaces.Organization", on_delete=models.CASCADE, related_name="addons"
    )
    addon_key = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=AddonStatus.choices, default=AddonStatus.TRIALING
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    stripe_subscription_item_id = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["organization", "addon_key"], name="unique_org_addon")
        ]
        ordering: ClassVar[list[str]] = ["addon_key"]

    def __str__(self) -> str:
        return f"{self.addon_key} @ {self.organization_id} ({self.status})"


class FeatureFlag(models.Model):
    """Rollout, not entitlement (BUILD-PLAN Part 3).

    Plan features answer "did you pay for this"; a flag answers "has this
    shipped to you yet". Both resolve through the one entitlement resolver, so
    a phase can be reverted by flipping a row rather than deploying — and
    **flag off means pre-phase behaviour, never an error**.

    A null `organization` is the global default, which is what makes a flag
    switchable for everyone without writing one row per customer.
    """

    organization = models.ForeignKey(
        "workspaces.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="feature_flags",
    )
    key = models.CharField(max_length=64)
    enabled = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(organization__isnull=True),
                name="unique_global_feature_flag",
            ),
            models.UniqueConstraint(
                fields=["organization", "key"],
                condition=models.Q(organization__isnull=False),
                name="unique_org_feature_flag",
            ),
        ]
        ordering: ClassVar[list[str]] = ["key"]

    def __str__(self) -> str:
        scope = self.organization_id or "global"
        return f"{self.key}={self.enabled} ({scope})"
