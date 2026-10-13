"""Connected publishing accounts (design.md §6.2, D2/D3).

**No token storage.** The publishing provider holds the platform credentials —
that is the whole point of D2, and it is why this model carries a
`provider_account_id` and nothing secret. `common/encryption.py` exists for the
day direct OAuth arrives; nothing here needs it.

`status` is the one field the rest of the system reads before acting:
`ACTIVE` publishes, anything else does not. `OVER_LIMIT` is what a downgrade
leaves behind (`billing.services.subscriptions.downgrade_to_free`, I10 — mark,
never delete), `REAUTH_REQUIRED` is what the provider reports when the user's
platform grant lapsed, and `REVOKED` is a disconnect that the provider has
already acted on.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from content.models import Platform


class SocialAccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REAUTH_REQUIRED = "REAUTH_REQUIRED", "Reauth required"
    OVER_LIMIT = "OVER_LIMIT", "Over plan limit"
    #: A Facebook Page (or similar) the login can no longer reach — deleted, or
    #: the user stopped being its admin. Kept with its history; reconnecting
    #: means choosing another Page (`page.removed`, SA-42).
    PAGE_REMOVED = "PAGE_REMOVED", "Page removed"
    REVOKED = "REVOKED", "Revoked"


class SocialAccount(models.Model):
    """design.md §6.2. `UNIQUE(workspace, platform, provider_account_id)`
    rather than `UNIQUE(workspace, platform)`: one workspace may connect two
    Instagram accounts, and the provider's id is what distinguishes them.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="social_accounts"
    )
    platform = models.CharField(max_length=16, choices=Platform.choices)
    handle = models.CharField(max_length=120, blank=True)
    display_name = models.CharField(max_length=160, blank=True)
    #: The publishing provider's identifier for this account — the only handle
    #: OCCS has on it, since the credentials live with the provider (D2).
    provider_account_id = models.CharField(max_length=128)
    followers_cached = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=SocialAccountStatus.choices, default=SocialAccountStatus.ACTIVE
    )
    #: Which vendor this account was connected through (`socialapi`, `zernio`).
    #: An id from one vendor is never sent to the other; blank on rows from
    #: before providers could change, which were all Zernio's.
    provider_key = models.CharField(max_length=24, blank=True)
    #: What this connection can do, as the provider showed it — currently
    #: `{"insights": false}` when Instagram was connected without insights
    #: access, so reach and views read *unavailable* until a reconnect (G13).
    capabilities = models.JSONField(default=dict, blank=True)
    #: The platform's own words when it asks for a reconnect.
    reconnect_reason = models.CharField(max_length=280, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["platform", "handle"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "platform", "provider_account_id"],
                name="unique_workspace_platform_provider_account",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.handle or self.provider_account_id} ({self.platform})"

    @property
    def is_publishable(self) -> bool:
        return self.status == SocialAccountStatus.ACTIVE
