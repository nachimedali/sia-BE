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
