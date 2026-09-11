"""Content core (design.md §6.3, §6.4 media half).

`Post` is the data spine every later generation and schedule writes into
(implementation.md Phase 4). `Post.product` and `Post.generation` land in this
module now that Phase 7 exists; `Post.recipe→CreativeRecipe` stays absent until
Phase 14 — each lands as that phase's own nullable column, the same shape the
billing ledgers used for `generation` before Phase 7 (design.md A32, A48).

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

from common.records import AppendOnly
from common.visibility import Visibility


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
    #: Whether a guest reviewer may load this post at all (P2-03). Defaulting
    #: to `INTERNAL` is the whole point: a draft becoming client-visible
    #: because a field was forgotten is the one failure here that cannot be
    #: walked back, since by then the client has read it. Enforced by
    #: `common.visibility.VisibilityScopedQuerySetMixin` in the queryset, never
    #: by a serializer omitting a field — a serializer that hides a row still
    #: loaded it, still counted it in a page total, and still answered 200.
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.INTERNAL
    )
    # Server-controlled: `POST /posts/{id}/schedule/` (Phase 8) is the only
    # writer. Exposing these on the generic Post serializer now would let a
    # client set a delivery mode or a schedule the horizon/quota checks that
    # endpoint owns have no chance to run against yet (design.md A49).
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)

    # --- approval (BUILD-PLAN P2-07, P2-10, P2-11) -------------------------
    #: Where the post sits in its workspace's approval chain. Null unless it is
    #: `PENDING_REVIEW` on a blocking chain. **No new statuses**: `PENDING_REVIEW`
    #: means "at stage N" and this column says which — putting the chain's shape
    #: into `PostStatus` would make every new chain shape a migration.
    current_stage = models.ForeignKey(
        "workspaces.ApprovalStage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts_in_review",
    )
    #: The author's *proposal*, captured at submit time and consumed by the
    #: final approval (P2-10). Deliberately not `scheduled_at`: that column has
    #: exactly one writer, and a proposal written straight into it would skip
    #: the horizon, quota and entitlement checks living behind it.
    proposed_delivery_mode = models.CharField(
        max_length=16, choices=DeliveryMode.choices, blank=True
    )
    proposed_scheduled_at = models.DateTimeField(null=True, blank=True)
    #: Set when the last approval stage clears (P2-11). While it is set, every
    #: content mutation is a 409 at the **service** layer — otherwise "approved"
    #: describes content that nobody approved. Revisions stay readable; an
    #: `admin` unlocks, and that writes an audit entry.
    locked_at = models.DateTimeField(null=True, blank=True)

    source = models.CharField(max_length=16, choices=PostSource.choices, default=PostSource.MANUAL)
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts",
    )
    product = models.ForeignKey(
        "products.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts",
    )
    generation = models.ForeignKey(
        "ai.Generation",
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
            # The due-publish scan (`scheduling.publishing.due_post_ids`) runs
            # every minute across every workspace, so no workspace-leading index
            # helps it. Same shape, and the same reason, as
            # `Reminder`'s `(state, send_at)`.
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self) -> str:
        preview = (self.master_body[:40] + "…") if len(self.master_body) > 40 else self.master_body
        return preview or f"Post {self.pk}"

    def ordered_attachments(self) -> list[PostMediaAttachment]:
        """This post's media, in carousel order — **base rows only**.

        Deliberately not `self.media_assets.order_by(...)`: ordering through
        a reverse accessor on MediaAsset would join on *every* post that
        asset is attached to, not just this one, corrupting both the order
        and the row count the moment an asset is reused on a second post.

        A bare `.all()` rather than re-chaining `.select_related()` /
        `.order_by()` here on purpose: PostMediaAttachment.Meta already
        orders by ("order", "id"), and any extra clause on the manager would
        build a new queryset that no longer matches a `Prefetch` set up by a
        caller (content/views.py) — chaining anything defeats the cache and
        re-queries per post.

        Which is exactly why the per-target alt-text rows (P1-06) are filtered
        **in Python** rather than with `.filter()`: a filter here would defeat
        the prefetch and re-query once per post in a list response. The base
        rows are the ones with no `target_override`; an override row describes
        the same asset for one platform and is not a second slide.
        """
        return [a for a in self.media_attachments.all() if a.target_override_id is None]

    def ordered_media(self) -> list[MediaAsset]:
        return [attachment.media_asset for attachment in self.ordered_attachments()]


#: Long enough for a full descriptive sentence on the most generous platform
#: and short enough that a paste of the whole caption is refused. A platform
#: fact, like the char limits in `rules.py`, not a commercial number.
ALT_TEXT_MAX_LENGTH = 1000


class PostMediaAttachment(models.Model):
    """Through table for `Post.media_assets`, ordered — a carousel's slide
    order is content, not an implementation detail, so it needs a place to
    live that a bare `ManyToManyField` does not reliably preserve.

    **Alt text lives here, not on `MediaAsset`** (P1-06). It is a property of
    *this use of the file in this post*: the same photograph is "our founder
    at the 2019 launch" in one post and "the espresso machine we still use" in
    another. Putting it on the asset would force one of those onto the other
    and, worse, would put a mutable text column on a row that is immutable by
    design — `test_media_asset_declares_no_mutable_text_field` is what keeps
    that from being undone by a one-line convenience.

    Two row shapes share this table:

    * **base** — `target_override` null. The post's ordered media, one row per
      asset. This is what a carousel is made of.
    * **override** — `target_override` set. Alt text for one platform only,
      because Instagram and LinkedIn describe the same image to different
      audiences. Not a second slide; `Post.ordered_attachments` filters them
      out, and `test_an_override_row_does_not_duplicate_the_media` is the
      guard on that.
    """

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="media_attachments")
    # PROTECT: an asset backing a post is not safe to delete out from under it;
    # remove the attachment first.
    media_asset = models.ForeignKey(
        "content.MediaAsset", on_delete=models.PROTECT, related_name="post_attachments"
    )
    order = models.PositiveSmallIntegerField(default=0)
    alt_text = models.CharField(max_length=ALT_TEXT_MAX_LENGTH, blank=True)
    #: Null on a base row. Set on a per-platform override, which carries alt
    #: text and nothing else — `order` on an override row is meaningless, since
    #: per-target ordering is `PostTarget.media_override`'s job.
    target_override = models.ForeignKey(
        "content.PostTarget",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="media_alt_overrides",
    )

    class Meta:
        ordering: ClassVar[list[str]] = ["order", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Two partial constraints rather than one over three columns:
            # Postgres treats repeated NULLs as distinct, so a plain
            # `UNIQUE(post, media_asset, target_override)` would let a post
            # carry the same asset twice as a base row and publish a
            # two-slide carousel of one photograph. Same shape, and the same
            # reason, as `PostTarget`'s pair of constraints.
            models.UniqueConstraint(
                fields=["post", "media_asset"],
                condition=models.Q(target_override__isnull=True),
                name="unique_post_media_asset",
            ),
            models.UniqueConstraint(
                fields=["post", "media_asset", "target_override"],
                condition=models.Q(target_override__isnull=False),
                name="unique_post_media_asset_target_override",
            ),
        ]

    def __str__(self) -> str:
        if self.target_override_id is not None:
            return f"{self.post_id}/{self.target_override_id} alt -> {self.media_asset_id}"
        return f"{self.post_id}[{self.order}] -> {self.media_asset_id}"


class MediaKind(models.TextChoices):
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class MediaSource(models.TextChoices):
    UPLOAD = "UPLOAD", "Upload"
    GENERATED = "GENERATED", "Generated"
    #: Produced by cropping or trimming another asset (P1-12). A separate
    #: source rather than a flag on `derived_from`: "where did these bytes come
    #: from" is one question with one answer, and two columns encoding it is
    #: two columns to disagree.
    DERIVED = "DERIVED", "Derived"


def media_asset_upload_to(instance: MediaAsset, filename: str) -> str:
    """Workspace-namespaced, content-addressed-ish path.

    A random name rather than the original filename: the original is
    user-supplied and neither unique nor safe to trust as a path segment.
    """
    ext = Path(filename).suffix.lower()
    return f"workspaces/{instance.workspace_id}/media/{uuid.uuid4().hex}{ext}"


class MediaAsset(models.Model):
    """design.md §6.3/§6.4. `generation→Generation` lands now that Phase 7
    exists (A48)."""

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
    generation = models.ForeignKey(
        "ai.Generation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_assets",
    )
    #: The asset this one was cropped or trimmed from (P1-12). Immutability is
    #: why this exists: an edit cannot rewrite the original, so it makes a new
    #: row and says where it came from. `SET_NULL` rather than `CASCADE` —
    #: deleting an original must not take the crop that is on a published post
    #: with it.
    derived_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derivatives"
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

    Unique on `(post, social_account)` as of Phase 9, which is when one
    workspace could first connect two accounts on the same platform and make
    the old `(post, platform)` constraint wrong (A50). `social_account` stays
    nullable: a reminder-delivered post has a target row describing what was
    rendered without any connected account behind it, and Free-tier workspaces
    never have one at all.
    """

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="targets")
    platform = models.CharField(max_length=16, choices=Platform.choices)
    social_account = models.ForeignKey(
        "channels.SocialAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="post_targets",
    )
    # --- per-platform overrides (BUILD-PLAN P1-03) ------------------------
    #
    # **Null means inherit**, and that is not the same as empty. A blank
    # `body_override` is a deliberate empty caption — legitimate on a
    # video-first platform — while `None` means "whatever the master post
    # says". Collapsing them would make clearing an override impossible.
    #
    # Nothing outside `render_post` reads these. Resolution lives there
    # precisely so preview and publish cannot resolve them differently
    # (P1-04), which is the single greatest threat to preview-equals-publish.
    # DJ001 says avoid `null=True` on a text field, and it is right almost
    # everywhere. Here the tri-state is the feature: null inherits, "" is a
    # deliberately empty caption. Collapsing them would make an empty caption
    # unrepresentable.
    body_override = models.TextField(null=True, blank=True)  # noqa: DJ001
    media_override = models.JSONField(null=True, blank=True)
    #: Per-platform composer fields, validated against the declaration in
    #: `content.services.rules` (P1-05). Non-null `{}` rather than nullable:
    #: unlike a body, "no options" and "default options" are the same thing.
    platform_options = models.JSONField(default=dict, blank=True)
    #: Which shape the options above were written under, so a payload rendered
    #: by an older release is recognisable rather than silently misread.
    options_schema_version = models.PositiveSmallIntegerField(default=0)

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
            models.UniqueConstraint(
                fields=["post", "social_account"], name="unique_post_social_account_target"
            ),
            # Two targets on the same platform are only meaningful when they
            # name different accounts; without one, platform is still the
            # discriminator (a reminder-mode post, or Free tier).
            models.UniqueConstraint(
                fields=["post", "platform"],
                condition=models.Q(social_account__isnull=True),
                name="unique_post_platform_target_unconnected",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["state"])]

    def __str__(self) -> str:
        return f"{self.post_id} -> {self.platform} ({self.state})"


class PostRevision(AppendOnly):
    """One version of a post's content (P1-08).

    **Append-only, and a restore is a new row.** History that a restore can
    rewrite is not history — "what did this look like on Tuesday" has to
    survive somebody putting Tuesday's version back on Friday.

    **Diff plus periodic checkpoint, not a snapshot every time.** A full
    snapshot per revision stores the whole post again for a one-word edit; a
    pure diff chain makes reconstruction walk the entire history and, worse,
    makes retention unsafe — deleting an old row silently invalidates every
    diff after it. Every `CHECKPOINT_EVERY`-th revision carries a full
    `snapshot` and the rest carry only `diff`, so reconstruction is bounded and
    the prune has an anchor it can stop at.

    `author` is nullable because not every revision has a person behind it:
    a recurrence materialisation or an autopilot draft has no author, and
    `PROTECT` on a real one would block deleting a user who ever typed.
    """

    append_only_hint = "restore the revision instead of editing it."

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="revisions")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="post_revisions",
    )
    #: 1-based and dense per post — it is what a user cites ("restore version
    #: 4"), so it cannot be the primary key, which is global and gapped.
    sequence = models.PositiveIntegerField()
    #: Populated only on a checkpoint; `{}` otherwise. Not nullable — an empty
    #: mapping and "no snapshot" are the same statement, and two ways to say it
    #: is one too many.
    snapshot = models.JSONField(default=dict, blank=True)
    #: `{field: [before, after]}` against the previous revision. Empty on the
    #: first, which has nothing to differ from.
    diff = models.JSONField(default=dict, blank=True)
    is_checkpoint = models.BooleanField(default=False)
    reason = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-sequence"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["post", "sequence"], name="unique_post_revision_seq")
        ]
        indexes: ClassVar[list[models.Index]] = [
            # The retention sweep asks "which revisions are older than this
            # date", across every post at once — no post-leading index helps it.
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.post_id} v{self.sequence}"


class TemplateKind(models.TextChoices):
    """Declared in full, gated to what is reachable — the same shape
    `PostStatus` and `GenerationMode` already use.

    `DOC` is Phase 3's (`Post.content_kind`). The enum lands now so a template
    saved in Phase 1 does not need migrating when Phase 3 arrives;
    `content.services.templates.CREATABLE_KINDS` is what refuses it in the
    meantime.
    """

    SOCIAL = "SOCIAL", "Social"
    DOC = "DOC", "Document"


class PostTemplate(models.Model):
    """A saved starting point (P1-09).

    **Applying copies; it never links.** A post that kept pointing at its
    template would be rewritten every time the template was edited — including
    posts already scheduled, and in the worst case already approved. The cost
    of copying is that a template edit does not propagate, which is the correct
    behaviour rather than a limitation.

    `payload` is validated against `rules.py` on the way in, not at apply time:
    a template holding an option Instagram does not have fails at the worst
    possible moment otherwise.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="post_templates"
    )
    name = models.CharField(max_length=120)
    content_kind = models.CharField(
        max_length=8, choices=TemplateKind.choices, default=TemplateKind.SOCIAL
    )
    #: `{master_body, media_asset_ids: [...], platform_options: {platform: {...}}}`.
    payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="post_templates",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="unique_post_template_name_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return self.name


class RecurrenceRule(models.Model):
    """ "Every Monday at 09:00" (P1-10).

    `timezone` is an IANA name and the rule is expanded in **local wall time**,
    then converted to UTC. Expanding in UTC would move the slot an hour twice a
    year — the exact bug Part 3's "store UTC, convert at edges" rule exists to
    prevent. What is stored is UTC; what is meant is the office clock.

    `horizon_days` bounds how far ahead the scan materialises, which is what
    keeps a rule from filling a calendar to the end of time on its first run.
    """

    #: BUILD-PLAN calls this `source`. It is the template a slot is drawn from,
    #: and a template rather than a post because a recurrence that copied one
    #: particular post would republish that post's edits along with it.
    source = models.ForeignKey(
        PostTemplate, on_delete=models.CASCADE, related_name="recurrence_rules"
    )
    rrule = models.TextField(help_text="RFC 5545 RRULE, without the DTSTART line.")
    timezone = models.CharField(max_length=64, default="UTC")
    horizon_days = models.PositiveSmallIntegerField(default=30)
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["active"])]

    def __str__(self) -> str:
        return f"{self.rrule} ({self.timezone})"


class RecurrenceOccurrence(models.Model):
    """One materialised slot.

    The row exists so a re-scan can tell what it already covered. Its unique
    constraint is what makes two scans racing each other collide instead of
    double-creating — the same guarantee, for the same reason, as
    `unique_autopilot_slot_per_product`.
    """

    rule = models.ForeignKey(RecurrenceRule, on_delete=models.CASCADE, related_name="occurrences")
    #: UTC, as every stored instant is. The local time it means is
    #: reconstructed from `rule.timezone` at display.
    slot_at = models.DateTimeField()
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="recurrence_occurrences")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["slot_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["rule", "slot_at"], name="unique_recurrence_slot")
        ]

    def __str__(self) -> str:
        return f"{self.rule_id} @ {self.slot_at:%Y-%m-%d %H:%M}"
