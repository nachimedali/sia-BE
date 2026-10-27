"""The Brand Core (steps-plan S1).

Two tables, because there are two different facts:

* `BrandImport` — **one reading of a website**: what was fetched, how far the
  reading got, what it found, and how the user reviewed each section. A draft;
  nothing it holds is used by generation until it is applied.
* `BrandCore` — **the brand kit the product reads**, versioned. Applying an
  import writes a new version; nothing edits a version in place, so every later
  generation can say which kit it ran under (the same reason `TasteProfile` is
  versioned — rule 12).

Every value carries where it came from (`source` URL or `you`) and how sure the
reading was. An unknown is absent, never a guess dressed as a fact.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models


class ImportStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    #: The user chose to fill the brand in by hand. Recorded, so the wizard can
    #: tell "skipped" from "not reached yet".
    SKIPPED = "SKIPPED", "Skipped"
    #: Reviewed and written into a `BrandCore` version.
    APPLIED = "APPLIED", "Applied"


class ImportStage(models.TextChoices):
    """The reading, in the order the progress panel shows it."""

    RESOLVING = "RESOLVING", "Resolving domain"
    READING = "READING", "Reading pages"
    PRODUCTS = "PRODUCTS", "Finding products"
    AUDIENCE = "AUDIENCE", "Reading audience signals"
    COMPETITORS = "COMPETITORS", "Spotting competitors"
    VOICE = "VOICE", "Listening for voice"
    PALETTE = "PALETTE", "Sampling palette and fonts"
    DONE = "DONE", "Done"


#: The sections the user reviews, in order. Fixed: each one has its own shape.
SECTIONS: tuple[str, ...] = ("products", "audience", "competitors", "voice", "palette")


class ReviewState(models.TextChoices):
    ACCEPTED = "accepted", "Accepted"
    EDITED = "edited", "Edited"


class BrandImport(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="brand_imports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    url = models.URLField(max_length=500, blank=True)
    domain = models.CharField(max_length=253, blank=True)
    status = models.CharField(
        max_length=10, choices=ImportStatus.choices, default=ImportStatus.QUEUED
    )
    stage = models.CharField(
        max_length=12, choices=ImportStage.choices, default=ImportStage.RESOLVING
    )
    #: 0-100, for the progress bar. Moves with `stage`; never decreases.
    progress = models.PositiveSmallIntegerField(default=0)
    #: Every URL actually read, in order — the audit trail of the reading.
    pages = models.JSONField(default=list, blank=True)
    #: What was found, section by section (see `brand.services.extract`).
    result = models.JSONField(default=dict, blank=True)
    #: section → `accepted` | `edited`.
    review = models.JSONField(default=dict, blank=True)
    #: section → the user's edited value, replacing the found one on apply.
    edits = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.domain or 'manual'} ({self.status})"


class BrandCore(models.Model):
    """One version of the brand kit. Append-only: a change is a new version."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="brand_cores"
    )
    version = models.PositiveIntegerField()
    source_import = models.ForeignKey(
        BrandImport, null=True, blank=True, on_delete=models.SET_NULL, related_name="cores"
    )
    #: section → {"value": …, "origin": "site" | "you", "source": url | ""}.
    sections = models.JSONField(default=dict)
    #: Identity facts read from the site (name, description, logo, socials,
    #: languages, timezone…), each with its source — what the wizard pre-fills.
    identity = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-version"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "version"], name="unique_brand_core_version"
            ),
        ]

    def __str__(self) -> str:
        return f"Brand core v{self.version} ({self.workspace_id})"


class ProductStage(models.TextChoices):
    """Reading one product page, in the order the import overlay shows it."""

    OPENING = "OPENING", "Opening the page"
    DETAILS = "DETAILS", "Title, price and SKU"
    PHOTOS = "PHOTOS", "Collecting photos"
    DESCRIPTION = "DESCRIPTION", "Description and features"
    CLAIMS = "CLAIMS", "Spotting claims"
    DONE = "DONE", "Done"


class ProductImport(models.Model):
    """**One reading of one product page** (S1, the product half).

    A proposal for the new-product form, never a product: the user reviews
    every field before anything is created, and the product is created by the
    ordinary create endpoint with the ordinary validation. Photos are not
    stored here — `images` lists the URLs found, and the browser pulls each one
    through `GET …/images/{n}/` into the form's normal upload, so an abandoned
    import leaves no orphaned media behind.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="product_imports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    url = models.URLField(max_length=500)
    domain = models.CharField(max_length=253, blank=True)
    status = models.CharField(
        max_length=10, choices=ImportStatus.choices, default=ImportStatus.QUEUED
    )
    stage = models.CharField(
        max_length=12, choices=ProductStage.choices, default=ProductStage.OPENING
    )
    progress = models.PositiveSmallIntegerField(default=0)
    pages = models.JSONField(default=list, blank=True)
    #: field → {"value": …, "source": url}; unknowns absent. See
    #: `brand.services.product_page`.
    result = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.url} ({self.status})"


# -----------------------------------------------------------------------------
# Avatars (S17, `ads_templates.md`, `avatar_upgrade/analysis.md`)
# -----------------------------------------------------------------------------
class AvatarKind(models.TextChoices):
    #: Invented from text traits; passes the look-alike check before it is saved.
    SYNTHETIC = "SYNTHETIC", "Invented"
    #: A real person, usable only while their recorded consent stands.
    REAL = "REAL", "Real person"


class Likeness(models.TextChoices):
    CLEAR = "CLEAR", "Clear"
    MATCH = "MATCH", "Looks like a known person"
    #: No look-alike vendor is configured. Said plainly, never shown as clear.
    UNCHECKED = "UNCHECKED", "Not checked"


class Avatar(models.Model):
    """A person the brand puts in its images and videos.

    **Never edited.** A new face is a new avatar, so every render keeps the
    media it was made with and history stays true. Only the house-face pin, the
    archive stamp and the consent stamps change, each written by
    `brand.services.avatars` with an `AuditLog` row where it matters.

    Limits (`max_avatars`) are counted, never cached: the oldest active
    avatars up to the plan's number are usable, newer ones are locked after a
    downgrade (402 `avatar_over_limit`), and nothing is deleted.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="avatars"
    )
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=AvatarKind.choices)
    #: The face every render is held to. Its file is never changed.
    portrait = models.ForeignKey(
        "content.MediaAsset", on_delete=models.PROTECT, related_name="avatar_portraits"
    )
    #: Reference angles (media ids): made after an invented avatar is saved, or
    #: the further photos of a real person.
    angles = models.JSONField(default=list, blank=True)
    #: `avatar_trait` keys by group (`archetype`, `age`, `look`, `style`,
    #: `energy`) and a free note — what the portraits were drawn from.
    traits = models.JSONField(default=dict, blank=True)
    #: Content languages this person speaks (L-6: en, fr, it).
    languages = models.JSONField(default=list, blank=True)
    is_house = models.BooleanField(default=False)
    #: Unused since the look-alike check was removed (2026-10-27); dropped in a
    #: later deploy (expand → contract).
    likeness = models.CharField(max_length=10, choices=Likeness.choices, default=Likeness.UNCHECKED)
    likeness_provider = models.CharField(max_length=40, blank=True)

    # --- consent, REAL only -----------------------------------------------
    #: The person's full name as it appears on the consent, and the date they
    #: signed. Recorded by an admin of the workspace.
    consent_signed_by = models.CharField(max_length=120, blank=True)
    consent_signed_on = models.DateField(null=True, blank=True)
    consent_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    consent_recorded_at = models.DateTimeField(null=True, blank=True)
    #: Withdrawal: stamped once, never cleared. The avatar leaves every future
    #: render; renders already made stay where they are.
    consent_revoked_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["created_at", "id"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["workspace", "archived_at"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


class DraftStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


class AvatarDraft(models.Model):
    """One batch of portraits drawn from traits, to pick a new avatar from.

    Counted per **organization** per calendar month against
    `avatar_drafts_monthly` (a failed batch does not count), and paid in
    credits on success only — the `GenerationCost` row `(IMAGE, AVATAR)`.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="avatar_drafts"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    traits = models.JSONField(default=dict, blank=True)
    prompt = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=DraftStatus.choices, default=DraftStatus.QUEUED)
    #: `[{"media": id}]` (older rows also carry an unused `likeness`)
    portraits = models.JSONField(default=list, blank=True)
    credits = models.PositiveIntegerField(default=0)
    charge = models.OneToOneField(
        "billing.CreditLedger",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="avatar_draft",
    )
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"avatar draft {self.pk} ({self.status})"
