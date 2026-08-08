"""Tenancy (design.md §6.1).

Every workspace-scoped model filters by workspace through the shared queryset
mixin — tenancy leakage is a security bug, not a defect (design.md §11).
"""

from __future__ import annotations

import secrets
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.text import slugify


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
