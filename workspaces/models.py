"""Tenancy, roles, approvals and audit (design.md §6.1, §6.9, §8.8).

Every workspace-scoped model filters by workspace through the shared queryset
mixin — tenancy leakage is a security bug, not a defect (design.md §11).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Any, ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone
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
#:   comment, edit   CONTRIBUTOR+  `content/views.py` gated `submit` and the
#:                                 post-comment endpoints on CONTRIBUTOR and
#:                                 raised "VIEWER cannot comment" explicitly.
#:                                 Those comments are `collaboration.Thread`
#:                                 since P2-01; the gate they implied is now
#:                                 `HasPermission(COMMENT)` on `ThreadViewSet`.
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

    `owner`, `plan`, `trial_ends_at` and `stripe_customer_id` moved up from
    `Workspace` and were dropped there at the contract step (P0-56) — this row
    is the only place they live.

    `provider_profile_id` and `referral_code` exist on both and mean different
    things: the workspace copy is the publishing provider's tenant for that
    brand and that brand's own referral code, neither of which pools.
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

    #: What this company is billed in. **On the organization, not the
    #: workspace**, because billing pools here (L-1): one company pays one
    #: invoice, and two brands under it charged in different currencies would
    #: be two invoices wearing one subscription.
    #:
    #: Null means "whatever the catalogue's default is", which is the right
    #: answer before anyone has said otherwise — and is why this is nullable
    #: rather than defaulted to USD, a choice that would silently make every
    #: new market American.
    billing_currency = models.ForeignKey(
        "billing.Currency",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="organizations",
    )

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


class WorkspaceStatus(models.TextChoices):
    """Whether this brand may be written to (P0-18, P0-23).

    Both non-`ACTIVE` states are **read-only, never invisible**. A workspace
    the customer asked for and cannot see is worse than one they can see and
    cannot yet write to — and in the downgrade case, hiding it would look
    exactly like data loss.
    """

    ACTIVE = "ACTIVE", "Active"
    #: Created, but the subscription quantity update has not landed. The
    #: webhook is the source of truth for what was granted, so the workspace
    #: exists and waits rather than being refused.
    PENDING_BILLING = "PENDING_BILLING", "Awaiting billing confirmation"
    #: Beyond the plan's workspace cap after a downgrade. Oldest survive, same
    #: rule as social accounts.
    OVER_LIMIT = "OVER_LIMIT", "Over the plan limit"


class Workspace(models.Model):
    #: Non-null since the contract step (P0-56). Every workspace sits inside
    #: exactly one company, `provision_workspace` builds both in one
    #: transaction, and the migration gave the pre-migration rows an
    #: organization of their own before adding the constraint.
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    status = models.CharField(
        max_length=16, choices=WorkspaceStatus.choices, default=WorkspaceStatus.ACTIVE
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
    # `plan`, `owner`, `trial_ends_at` and `stripe_customer_id` moved to
    # `Organization` and were dropped here at the contract step (P0-56).
    # Billing pools at the company (L-1), and a shadow copy of the
    # authoritative column is precisely where drift comes from.
    #
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
    # `requires_approval` moved to `ApprovalChain.blocks_publish` (P2-04).
    # A boolean could say *whether* a workspace reviews; C-02 makes approval
    # universal and leaves only *which flow*, which a boolean cannot say —
    # and keeping both would be two sources of truth for one question.

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


class ApiKey(models.Model):
    """A scoped, organization-owned API key (P0-50).

    **Shipped in Phase 0 although no key is issued until Phase 10.** Scopes are
    a versioning decision, not a feature: an API that goes out unscoped can
    only be scoped later by breaking every integration built against it.
    Declaring them now costs days; retrofitting them costs months, and the
    OpenAPI schema is already generated.

    Hash-only, like every other credential here. `prefix` is the first eight
    characters of the raw key, stored in the clear purely so a user can tell
    two keys apart in a list without the system being able to reconstruct
    either.
    """

    #: The vocabulary a key may be granted. Deliberately the same words as the
    #: membership permission set: two parallel authority vocabularies would
    #: drift, and then "what can this key do" would have two answers.
    SCOPES: ClassVar[frozenset[str]] = frozenset(PERMISSIONS)

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=8)
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_api_keys",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(
        cls, *, organization: Organization, name: str, scopes: list[str], created_by: Any = None
    ) -> tuple[ApiKey, str]:
        unknown = set(scopes) - cls.SCOPES
        if unknown:
            raise ValueError(f"Unknown scopes: {', '.join(sorted(unknown))}.")

        raw = f"cv_{secrets.token_urlsafe(32)}"
        key = cls.objects.create(
            organization=organization,
            name=name,
            prefix=raw[:8],
            key_hash=cls.hash_key(raw),
            scopes=sorted(set(scopes)),
            created_by=created_by,
        )
        return key, raw

    def allows(self, scope: str) -> bool:
        """Whether this key carries `scope`.

        No implicit hierarchy: holding `admin` does not imply `publish`. A key
        is a machine credential and the caller who minted it said exactly what
        it may do — inferring more would grant something nobody typed.
        """
        return self.is_active and scope in set(self.scopes)


class Invitation(models.Model):
    """An invitation to join a workspace, addressed to an **email**, not a user
    (P0-16, P0-47).

    **Why not `EmailToken(purpose=INVITE)`**, which was already scaffolded:
    that model is keyed to a `user` FK, and the case Phase 0's ship gate names
    is inviting *"someone with no account"*. Making the FK nullable and hanging
    an email, a workspace and a role off the auth token would turn one clean
    single-purpose table into a grab-bag. So this reuses the *discipline* —
    hash-only storage, single-use, expiring — without reusing the table.
    `EmailTokenPurpose.INVITE` is left in place but unused; deleting an enum
    member is a migration for no gain.

    Only a SHA-256 hash is stored. The raw value exists once, in the email, so
    a database leak cannot be replayed into a workspace someone does not
    belong to.
    """

    TTL = dt.timedelta(days=14)

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    #: Snapshotted at mint time, not derived from `role` at accept time: a
    #: preset an admin edits next month must not silently re-grade an invite
    #: that was already sent and agreed.
    permissions = models.JSONField(default=list, blank=True)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_workspace_invitations",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "accepted_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.email} → {self.workspace}"

    @property
    def is_usable(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(
        cls,
        *,
        workspace: Workspace,
        email: str,
        role: str,
        invited_by: Any = None,
    ) -> tuple[Invitation, str]:
        """Mints an invitation, superseding any outstanding one for the same
        address and workspace.

        Superseding matters for the same reason it does on a verification
        email: without it, re-inviting leaves the earlier link live, which
        widens the window on an invitation that may have gone to a mistyped
        address.
        """
        normalised = email.strip().lower()
        cls.objects.filter(workspace=workspace, email=normalised, accepted_at__isnull=True).update(
            revoked_at=timezone.now()
        )

        raw = secrets.token_urlsafe(32)
        invitation = cls.objects.create(
            workspace=workspace,
            email=normalised,
            role=role,
            permissions=sorted(permissions_for(role)),
            invited_by=invited_by,
            token_hash=cls.hash_token(raw),
            expires_at=timezone.now() + cls.TTL,
        )
        return invitation, raw

    @classmethod
    def resolve(cls, raw: str) -> Invitation | None:
        """The usable invitation behind a raw token, or `None`.

        Read-only — accepting is a separate, locked step, because creating the
        account and the membership has to be one transaction with spending the
        token, and a resolve that consumed would leave the invite spent if that
        transaction rolled back.
        """
        invitation = cls.objects.filter(token_hash=cls.hash_token(raw)).first()
        return invitation if invitation is not None and invitation.is_usable else None


class ApprovalChain(models.Model):
    """*How* this workspace reviews, replacing the old `requires_approval`
    boolean (P2-04, C-02).

    **Approval is now required on every plan** (L-2). What a chain configures is
    not *whether* a human says yes but *which* flow says it:

    | chain                          | what it means                          |
    |--------------------------------|----------------------------------------|
    | `blocks_publish=False`         | whoever schedules the post approves it |
    | `blocks_publish=True`, 1 stage | one named reviewer, before scheduling  |
    | `blocks_publish=True`, N stages| a sequence — client sign-off, legal    |

    A non-blocking chain is **not** "no approval". `scheduling.services
    .schedule_post` records an `APPROVE` action naming whoever scheduled it, so
    the invariant *no `SCHEDULED` post without an `APPROVE` row* holds at every
    tier without asking a solo user to review their own draft. There is no
    "None" mode any more; L-2 removed it.

    **Modes are configurations, not code paths** — one state machine reads this
    table. The moment a mode becomes an `if` in the service, the third one costs
    as much as the first two together.

    Advanced is re-pitched on **chain depth** (P2-13): multi-stage, sequential,
    client-facing. Not on approval existing.
    """

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="approval_chains"
    )
    name = models.CharField(max_length=120)
    #: Exactly one per workspace, enforced below. `provision_workspace` creates
    #: it; a workspace without one has no defined review flow, which every
    #: caller would then have to invent an answer for.
    is_default = models.BooleanField(default=False)
    blocks_publish = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-is_default", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_default=True),
                name="one_default_approval_chain_per_workspace",
            ),
            models.UniqueConstraint(
                fields=["workspace", "name"], name="unique_approval_chain_name_per_workspace"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({'blocking' if self.blocks_publish else 'open'})"


class ApprovalStage(models.Model):
    """One step in a chain.

    `required_approvers` is a list of people, not a permission: "the brand lead
    signs off" is a statement about a person, and expressing it as a permission
    would grant that authority on every post in the workspace rather than on
    this stage. **Empty means anyone holding `approve`** — the common single-
    stage case, which should not require naming names.
    """

    chain = models.ForeignKey(ApprovalChain, on_delete=models.CASCADE, related_name="stages")
    #: 1-based and dense. It is what a reviewer sees ("stage 2 of 3"), so it
    #: cannot be the primary key, which is global and gapped.
    order = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=120)
    required_approvers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="approval_stages"
    )
    min_approvals = models.PositiveSmallIntegerField(default=1)
    #: Off by default. The author approving their own work is the failure a
    #: review stage exists to prevent, so allowing it has to be typed.
    allow_self_approve = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["order"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["chain", "order"], name="unique_approval_stage_order"),
            # A stage needing zero approvals would clear itself the moment a
            # post arrived, which is a stage that is not there.
            models.CheckConstraint(
                condition=models.Q(min_approvals__gte=1), name="approval_stage_needs_an_approval"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chain_id} #{self.order} {self.name}"


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
    #: Null in exactly two cases, and `guest_link` is what tells them apart:
    #:
    #: * **grandfathered** (P2-06) — both null. A post that reached `SCHEDULED`
    #:   before approval became universal; there is no person behind it.
    #: * **a guest reviewer** (P2-09) — `guest_link` set. A client signed off
    #:   from an emailed link and holds no account.
    #:
    #: Nullable rather than pointing at a synthetic "system" user, because a
    #: fake row in the user table is a fake row that can be invited, assigned
    #: and emailed. And two nullable columns rather than one, because "the
    #: migration did this" and "the client did this" are different answers to
    #: *who approved it*, and an audit trail that cannot tell them apart is not
    #: much of one.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approval_actions",
    )
    guest_link = models.ForeignKey(
        "collaboration.ReviewLink",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_actions",
    )
    action = models.CharField(max_length=16, choices=ApprovalActionType.choices)
    #: Which stage this action was taken at; null on a single-stage or
    #: non-blocking chain, and on the grandfathered rows. `SET_NULL` so editing
    #: a chain cannot rewrite the history of decisions taken under the old one.
    stage = models.ForeignKey(
        ApprovalStage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_actions",
    )
    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["post", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action} on post {self.post_id} by {self.actor_id or self.guest_link_id}"

    @property
    def actor_label(self) -> str:
        """Who to show in the trail. Never blank: a decision with no visible
        author is the one a reader has to go and ask about."""
        if self.actor is not None:
            return self.actor.email
        if self.guest_link is not None:
            return str(self.guest_link.reviewer_name)
        return "system"


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
