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


def generate_referral_code() -> str:
    """Affiliates are deferred (§12), but the code ships in v1 so attribution
    can be backfilled without a migration over live rows."""
    return secrets.token_urlsafe(9)


class Workspace(models.Model):
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
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
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
