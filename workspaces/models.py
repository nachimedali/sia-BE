"""Tenancy, roles, approvals and audit (design.md §6.1, §6.9, §8.8).

Every workspace-scoped model filters by workspace through the shared queryset
mixin — tenancy leakage is a security bug, not a defect (design.md §11).
"""

from __future__ import annotations

import secrets
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from common.records import AppendOnly


class BusinessType(models.TextChoices):
    D2C = "D2C", "D2C brand"
    SERVICE = "SERVICE", "Service business"
    CREATOR = "CREATOR", "Creator"


class BrandVoice(models.TextChoices):
    WARM = "WARM", "Warm & plain"
    SHARP = "SHARP", "Sharp & direct"
    PLAYFUL = "PLAYFUL", "Playful"
    EDITORIAL = "EDITORIAL", "Editorial"


class Role(models.TextChoices):
    """design.md §8.8. Order matters: index doubles as seniority."""

    OWNER = "OWNER", "Owner"
    ADMIN = "ADMIN", "Admin"
    EDITOR = "EDITOR", "Editor"
    CONTRIBUTOR = "CONTRIBUTOR", "Contributor"
    VIEWER = "VIEWER", "Viewer"


#: Lower is more senior — `Role`'s own declared order (its docstring: "index
#: doubles as seniority"). The one place that reads the ordering as numbers,
#: so `workspaces.permissions.HasRole` and the Celery-preflight recheck
#: (`workspaces.services.approvals.ensure_approval_still_valid`) compare the
#: same ranks rather than each re-deriving them from `Role.values.index(...)`.
ROLE_RANK: dict[str, int] = {role: index for index, role in enumerate(Role.values)}


class Permission(models.TextChoices):
    """The authority set (BUILD-PLAN Phase 0). `Role` stays as a display label
    and a seeded preset; **`Membership.permissions` is the authority**.

    Ranked roles cannot express "may approve but not publish", which approval
    chains need by Phase 2, and every attempt to bolt that onto a rank ends in
    a second, contradictory ordering.
    """

    VIEW = "view", "View content"
    COMMENT = "comment", "Comment internally"
    EDIT = "edit", "Create and edit content"
    APPROVE = "approve", "Approve content"
    PUBLISH = "publish", "Schedule and publish"
    ANALYZE = "analyze", "Read analytics"
    ADMIN = "admin", "Administer the workspace"


PERMISSIONS: frozenset[str] = frozenset(Permission.values)

#: The five seeded presets, derived from the role gates that existed *before*
#: the permission set did, so the migration cannot widen access:
#:
#:   comment, edit   CONTRIBUTOR+  `content/views.py` gates `submit` and
#:                                 `resolve_comment` on CONTRIBUTOR and raises
#:                                 "VIEWER cannot comment" explicitly.
#:   approve, admin  ADMIN+        `content/views.py` approve/request-changes/
#:                                 reject, `workspaces/views.py` team writes.
#:   view, analyze   every member  Reads and `analytics/views.py` carry no role
#:                                 gate, only `IsAuthenticated`.
#:
#: **`publish` is the one deliberate narrowing.** `POST /posts/{id}/schedule/`
#: carries no role gate today — only the ViewSet's `IsAuthenticated` — so a
#: VIEWER can currently schedule a post for publication. Preserving that
#: faithfully would carry a live authorization hole into the new model, so the
#: preset grants `publish` at EDITOR+ instead. Nothing reads these presets yet
#: (expand step); the narrowing takes effect when the gates are cut over, and
#: it is a behaviour change that belongs in that PR's notes, not this one.
_PRESETS: dict[str, frozenset[str]] = {
    Role.OWNER: PERMISSIONS,
    Role.ADMIN: PERMISSIONS,
    Role.EDITOR: frozenset({"view", "comment", "edit", "publish", "analyze"}),
    Role.CONTRIBUTOR: frozenset({"view", "comment", "edit", "analyze"}),
    Role.VIEWER: frozenset({"view", "analyze"}),
}


def permissions_for(role: str) -> set[str]:
    """The preset for `role`, as a fresh mutable set.

    Raises `KeyError` on an unknown role rather than returning an empty set: a
    typo that resolves to "no permissions" reads as a working deny and hides
    itself until someone is locked out of their own workspace.
    """
    return set(_PRESETS[role])


def generate_referral_code() -> str:
    """Affiliates are deferred (§12), but the code ships in v1 so attribution
    can be backfilled without a migration over live rows."""
    return secrets.token_urlsafe(9)


class Organization(models.Model):
    """The company that pays (BUILD-PLAN L-1).

        Organization      Volkswagen Group      pays, owns entitlements
          └── Workspace   Audi / SEAT / Skoda   the BRAND, owns taste
                └── Product   A4 / Q5 / e-tron  what the brand sells

    A small customer is one organization with one workspace; a group is one
    organization with many. **Entitlement accounting pools here**, not on the
    workspace: a 6-post package is 6 posts across the whole organization.

    `plan`, `trial_ends_at`, `stripe_customer_id`, `provider_profile_id` and
    `referral_code` are the columns moving up from `Workspace`. Both carry them
    until the contract step drops the workspace copies — that is the expand/
    dual-write discipline, not duplication anyone should read from twice.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_organizations"
    )

    plan = models.ForeignKey(
        "billing.Plan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="organizations",
    )
    #: Add-on trials only. The *plan* trial is a post quota, not a clock (L-4),
    #: which is what `trial_posts_used` counts.
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    trial_posts_used = models.IntegerField(default=0)

    stripe_customer_id = models.CharField(max_length=64, blank=True)
    provider_profile_id = models.CharField(max_length=64, blank=True)
    referral_code = models.CharField(max_length=32, unique=True, default=generate_referral_code)

    #: Bumped whenever this organization's add-on set changes (P0-24). It is
    #: half of the entitlement cache key, so an add-on being enabled, disabled
    #: or expired mints a new key rather than requiring anything to hunt down
    #: and evict the old one.
    #:
    #: A counter on this row rather than an aggregate over `OrganizationAddon`
    #: because the key is computed on **every** entitlement resolve: an
    #: aggregate would put a Postgres round-trip in front of a cache whose
    #: entire purpose is to avoid one.
    addon_version = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def unique_slug(name: str) -> str:
        base = slugify(name) or "organization"
        slug = base
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug


class OrganizationMembership(models.Model):
    """Who belongs to the paying company, as distinct from who works in one of
    its brands. An agency lead sits here once rather than in every workspace.

    `OWNER` is **not** assignable here — ownership is `Organization.owner`, a
    single row, so it cannot drift out of sync with a membership table.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="org_memberships"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_org_invitations",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="unique_user_organization"
            ),
            # P0-12. `OWNER` is not a membership row — it is
            # `Organization.owner`, a single FK, so it cannot drift out of sync
            # with a table. Enforced in the database rather than in a
            # serializer because a second write path would otherwise reopen it.
            models.CheckConstraint(
                condition=~models.Q(role=Role.OWNER),
                name="org_membership_owner_is_not_assignable",
            ),
        ]
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization} ({self.role})"


class Workspace(models.Model):
    #: Nullable until `backfill_organizations` has run everywhere and the
    #: contract step makes it required. Nothing reads it during expand.
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_workspaces"
    )

    # --- brand (wizard step 2) ---
    website = models.URLField(blank=True)
    logo = models.ImageField(upload_to="workspace-logos/", blank=True, null=True)
    description = models.CharField(max_length=280, blank=True)
    brand_voice_default = models.CharField(
        max_length=16, choices=BrandVoice.choices, default=BrandVoice.WARM
    )

    # --- market (wizard step 3) ---
    category = models.ForeignKey(
        "categories.Category", null=True, blank=True, on_delete=models.SET_NULL
    )
    business_type = models.CharField(max_length=16, choices=BusinessType.choices, blank=True)
    industry = models.CharField(max_length=120, blank=True)
    target_audience = models.CharField(max_length=280, blank=True)

    # --- operate (wizard step 4) ---
    timezone = models.CharField(max_length=64, default="UTC")
    regions = models.JSONField(default=list, blank=True)
    platforms = models.JSONField(default=list, blank=True)

    # --- commercial ---
    plan = models.ForeignKey(
        "billing.Plan", null=True, blank=True, on_delete=models.PROTECT, related_name="workspaces"
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=64, blank=True)
    # The publishing provider's tenant for this workspace (design.md §6.2,
    # Phase 9). Lives here rather than on `channels.SocialAccount` because it
    # is created before the first account exists and shared by all of them —
    # exactly the shape `stripe_customer_id` above already has: a third
    # party's identifier for this workspace, minted on first use.
    provider_profile_id = models.CharField(max_length=64, blank=True)
    referral_code = models.CharField(max_length=32, unique=True, default=generate_referral_code)

    #: An optional ceiling one workspace may not exceed *within* the org's
    #: pooled entitlement. `null` means off, which is the default — a budget
    #: nobody set must never be mistaken for a budget of zero.
    soft_budget_posts = models.IntegerField(null=True, blank=True)
    soft_budget_credits = models.IntegerField(null=True, blank=True)
    #: Attribution only. Storage is charged at org level; this says which brand
    #: spent it.
    storage_bytes_used = models.BigIntegerField(default=0)

    onboarding_complete = models.BooleanField(default=False)
    requires_approval = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def unique_slug(name: str) -> str:
        base = slugify(name) or "workspace"
        slug = base
        while Workspace.objects.filter(slug=slug).exists():
            # Random rather than incrementing: a counter leaks how many
            # workspaces share a name.
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug


class Membership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    #: BUILD-PLAN calls this column `role_preset`. It is not renamed: this
    #: column already *is* the preset, and renaming a live column to match a
    #: document costs a migration and buys nothing. `role` is display,
    #: `permissions` is authority.
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    #: Empty during expand. `backfill_memberships` fills it from
    #: `permissions_for(role)`; the gates keep reading `role` until cut-over.
    permissions = models.JSONField(default=list, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_invitations",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["user", "workspace"], name="unique_user_workspace")
        ]
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} @ {self.workspace} ({self.role})"


class PostComment(models.Model):
    """design.md §6.9. Internal team discussion on a draft — not the platform
    comments `analytics.Comment` captures once a post is live; different
    domain, different app, same English word.

    Mutable, unlike `ApprovalAction`/`AuditLog` below: `resolved_at` is set by
    a later action on the same row, which append-only would forbid.
    """

    post = models.ForeignKey("content.Post", on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="post_comments"
    )
    body = models.TextField()
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["post", "created_at"])]

    def __str__(self) -> str:
        return f"comment {self.pk} on post {self.post_id}"


class ApprovalActionType(models.TextChoices):
    """design.md §8.8's state machine, named by the transition rather than the
    state it lands on — `SUBMIT` reaches `PENDING_REVIEW` from either `DRAFT`
    or `CHANGES_REQUESTED`, so the row records what the actor *did*, not a
    status this table would otherwise duplicate from `Post.status`."""

    SUBMIT = "SUBMIT", "Submitted for review"
    APPROVE = "APPROVE", "Approved"
    REQUEST_CHANGES = "REQUEST_CHANGES", "Changes requested"
    REJECT = "REJECT", "Rejected"


class ApprovalAction(AppendOnly):
    """design.md §6.9, §8.8. One row per state-machine transition, append-only
    — "an audit trail that survives inconvenience" (Phase 13's own tagline)
    means the record of who decided what cannot be edited after the fact, not
    even by the person who made the call.

    `workspaces.services.approvals.ensure_approval_still_valid` reads the
    latest `APPROVE` row's `actor` to re-verify, at publish time, that whoever
    approved this still holds the role that let them (I5's Celery-preflight
    recheck).
    """

    append_only_hint = "record a new action instead of editing this one."

    post = models.ForeignKey(
        "content.Post", on_delete=models.CASCADE, related_name="approval_actions"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approval_actions"
    )
    action = models.CharField(max_length=16, choices=ApprovalActionType.choices)
    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["post", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action} on post {self.post_id} by {self.actor_id}"


class AuditLog(AppendOnly):
    """design.md §6.9, §8.8. Append-only, workspace-wide record of who did
    what — broader than `ApprovalAction`, which is specifically the four
    approval-state transitions. `target_repr` and `meta` are a free-text
    description and a small JSON payload rather than a generic FK, because a
    single audit trail spanning posts, memberships and workspace settings has
    no one model to point at (the same reasoning `CreditLedger.note` uses for
    a free-text field over a second FK for every possible source).
    """

    append_only_hint = "record a new entry instead of editing this one."

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="audit_log"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    verb = models.CharField(max_length=64)
    target_repr = models.CharField(max_length=200, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.verb} in workspace {self.workspace_id}"
