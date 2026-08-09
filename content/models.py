"""Content core (design.md §6.3, §6.4 media half).

`Post` is the data spine every later generation and schedule writes into
(implementation.md Phase 4). Three fields design.md §6.3 lists are deliberately
absent until the app they point at exists — `product→Product` (Phase 5),
`generation→Generation` (Phase 7), `recipe→CreativeRecipe` (Phase 14) — each
lands as that phase's own nullable column, the same shape the billing ledgers
used for `generation` before Phase 7 (design.md A32, A48).

`PostTarget.platform` is a plain choice field rather than `social_account→
SocialAccount`: that model does not exist until channels/ lands in Phase 9, and
the Adaptation Engine only ever needed the platform to adapt for, not a
connected account (design.md A47).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import ClassVar

from django.conf import settings
from django.db import models


class Platform(models.TextChoices):
    """The six platforms the onboarding wizard already collects
    (`Workspace.platforms`, wizard-steps.tsx `PLATFORMS`) — kept in lockstep so
    a post's target list is always drawn from values the FE already renders."""

    INSTAGRAM = "instagram", "Instagram"
    LINKEDIN = "linkedin", "LinkedIn"
    TIKTOK = "tiktok", "TikTok"
    YOUTUBE = "youtube", "YouTube"
    THREADS = "threads", "Threads"
    FACEBOOK = "facebook", "Facebook"


class PostStatus(models.TextChoices):
    """Full enum from design.md §6.3. Only `DRAFT` is reachable in Phase 4 —
    the transitions between the rest are Phase 8 (scheduling), Phase 9
    (publishing) and Phase 13 (the approval state machine)."""

    DRAFT = "DRAFT", "Draft"
    PENDING_REVIEW = "PENDING_REVIEW", "Pending review"
    CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
    REJECTED = "REJECTED", "Rejected"
    APPROVED = "APPROVED", "Approved"
    SCHEDULED = "SCHEDULED", "Scheduled"
    REMINDER_ARMED = "REMINDER_ARMED", "Reminder armed"
    PUBLISHING = "PUBLISHING", "Publishing"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed"
    PAUSED = "PAUSED", "Paused"


class DeliveryMode(models.TextChoices):
    REMINDER = "REMINDER", "Reminder"
    AUTO_PUBLISH = "AUTO_PUBLISH", "Auto-publish"


class PostSource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    AI = "AI", "AI"
    AUTOPILOT = "AUTOPILOT", "Autopilot"
    REPURPOSE = "REPURPOSE", "Repurpose"


class Post(models.Model):
    """One master post. Per-platform copies are never stored here — the
    Adaptation Engine (`content/services/adaptation.py`) derives them from
    `master_body` and `media_assets` on demand, so there is exactly one place
    that can drift from what actually gets published (design.md §8.6).
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="posts"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authored_posts"
    )
    master_body = models.TextField(blank=True)
    media_assets = models.ManyToManyField(
        "content.MediaAsset", through="PostMediaAttachment", related_name="posts", blank=True
    )

    status = models.CharField(max_length=20, choices=PostStatus.choices, default=PostStatus.DRAFT)
    # Server-controlled: `POST /posts/{id}/schedule/` (Phase 8) is the only
    # writer. Exposing these on the generic Post serializer now would let a
    # client set a delivery mode or a schedule the horizon/quota checks that
    # endpoint owns have no chance to run against yet (design.md A49).
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)

    source = models.CharField(max_length=16, choices=PostSource.choices, default=PostSource.MANUAL)
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts",
    )
    origin_post = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="repurposed_posts"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        preview = (self.master_body[:40] + "…") if len(self.master_body) > 40 else self.master_body
        return preview or f"Post {self.pk}"

    def ordered_media(self) -> list[MediaAsset]:
        # Deliberately not `self.media_assets.order_by(...)`: ordering through
        # a reverse accessor on MediaAsset would join on *every* post that
        # asset is attached to, not just this one, corrupting both the order
        # and the row count the moment an asset is reused on a second post.
        #
        # A bare `.all()` rather than re-chaining `.select_related()` /
        # `.order_by()` here on purpose: PostMediaAttachment.Meta already
        # orders by ("order", "id"), and any extra clause on the manager would
        # build a new queryset that no longer matches a `Prefetch` set up by a
        # caller (content/views.py) — chaining anything defeats the cache and
        # re-queries per post.
        return [attachment.media_asset for attachment in self.media_attachments.all()]


class PostMediaAttachment(models.Model):
    """Through table for `Post.media_assets`, ordered — a carousel's slide
    order is content, not an implementation detail, so it needs a place to
    live that a bare `ManyToManyField` does not reliably preserve.
    """

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="media_attachments")
    # PROTECT: an asset backing a post is not safe to delete out from under it;
    # remove the attachment first.
    media_asset = models.ForeignKey(
        "content.MediaAsset", on_delete=models.PROTECT, related_name="post_attachments"
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering: ClassVar[list[str]] = ["order", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["post", "media_asset"], name="unique_post_media_asset")
        ]

    def __str__(self) -> str:
        return f"{self.post_id}[{self.order}] -> {self.media_asset_id}"


class MediaKind(models.TextChoices):
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class MediaSource(models.TextChoices):
    UPLOAD = "UPLOAD", "Upload"
    GENERATED = "GENERATED", "Generated"


def media_asset_upload_to(instance: MediaAsset, filename: str) -> str:
    """Workspace-namespaced, content-addressed-ish path.

    A random name rather than the original filename: the original is
    user-supplied and neither unique nor safe to trust as a path segment.
    """
    ext = Path(filename).suffix.lower()
    return f"workspaces/{instance.workspace_id}/media/{uuid.uuid4().hex}{ext}"


class MediaAsset(models.Model):
    """design.md §6.3/§6.4. `generation→Generation` is absent for the same
    reason as on `Post` — that model does not exist until Phase 7 (A48).
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="media_assets"
    )
    kind = models.CharField(max_length=8, choices=MediaKind.choices)
    file = models.FileField(upload_to=media_asset_upload_to, max_length=255)
    mime = models.CharField(max_length=100, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, blank=True, help_text="sha256 hex digest.")
    source = models.CharField(
        max_length=16, choices=MediaSource.choices, default=MediaSource.UPLOAD
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.pk} ({self.workspace_id})"

    @property
    def aspect_ratio(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width / self.height


class PostTargetState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PUBLISHING = "PUBLISHING", "Publishing"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed"


class PostTarget(models.Model):
    """design.md §6.3. Inert until Phase 8/9 wire up scheduling and
    publishing — the model exists now so `rendered_payload` has somewhere to
    be written that is provably the same shape `/posts/preview/` returns
    (design.md §8.6, A47).

    Unique on `(post, platform)` rather than `(post, social_account)`: without
    `social_account` yet, platform is the only thing distinguishing two targets
    on the same post. This stops being correct the moment Phase 9 lets one
    workspace connect two accounts on the same platform, and moves to
    `(post, social_account)` in that phase's migration (A50).
    """

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="targets")
    platform = models.CharField(max_length=16, choices=Platform.choices)
    rendered_payload = models.JSONField(default=dict, blank=True)
    provider_post_id = models.CharField(max_length=128, blank=True)
    platform_post_id = models.CharField(max_length=128, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=16, choices=PostTargetState.choices, default=PostTargetState.PENDING
    )
    error_detail = models.JSONField(default=dict, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    # Populated by Phase 6 (idempotency groundwork); null rather than blank
    # so many un-populated rows can coexist under the unique constraint —
    # Postgres does not treat repeated NULLs as duplicates.
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["platform"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["post", "platform"], name="unique_post_platform_target")
        ]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["state"])]

    def __str__(self) -> str:
        return f"{self.post_id} -> {self.platform} ({self.state})"
