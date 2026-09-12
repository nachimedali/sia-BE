"""Campaigns and the planning surfaces around them (Phase 3).

**`Campaign` is one entity serving two roles** — an always-on theme and a
time-boxed sprint — separated by `kind` and by which nullable fields are
filled. Two models would fragment the calendar, the digest and the rule
lineage: a post would belong to a theme *and* a sprint, every planning view
would union two relations, and Phase 7's digest would have to pick one to hang
off. The cost is a handful of columns a `THEME` leaves null, which is the
cheaper of the two mistakes.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


class CampaignKind(models.TextChoices):
    THEME = "THEME", "Ongoing theme"
    SPRINT = "SPRINT", "Planning sprint"


class CampaignStatus(models.TextChoices):
    """`PLANNING → ACTIVE → CLOSED`, one direction only.

    `CLOSED` is terminal because Phase 7's Learn runs on it and produces a
    `Digest` bound to that window. Reopening would leave a digest describing a
    campaign that has since changed, and the digest is the input to rule
    proposals — so the stale conclusion would outlive the evidence.
    """

    PLANNING = "PLANNING", "Planning"
    ACTIVE = "ACTIVE", "Active"
    CLOSED = "CLOSED", "Closed"


class Campaign(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="campaigns"
    )
    name = models.CharField(max_length=140)
    #: A `DOC` post (P3-01). A foreign key rather than a text column so the
    #: brief gets threads, revisions and approval like anything else written
    #: here — which is the entire argument for `DOC` being a subtype.
    brief = models.ForeignKey(
        "content.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="briefed_campaigns",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    kind = models.CharField(max_length=8, choices=CampaignKind.choices, default=CampaignKind.THEME)
    status = models.CharField(
        max_length=10, choices=CampaignStatus.choices, default=CampaignStatus.PLANNING
    )
    #: `[{metric, target}]`, validated against `campaigns.GOAL_METRICS`.
    goals = models.JSONField(default=list, blank=True)
    #: `{platform: count}` — what the plan intends, against which the calendar
    #: shows what actually exists.
    planned_volume = models.JSONField(default=dict, blank=True)

    #: When it was actually closed, which is **not** `ends_at`: that is what was
    #: planned. Phase 7 bounds its analysis window on the fact, not the
    #: intention, because a sprint closed two weeks late analysed to its
    #: planned end would silently drop its last fortnight of posts.
    closed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_campaigns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-starts_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "status"]),
            # The calendar's question is "what is running over this window",
            # asked on every planning view.
            models.Index(fields=["workspace", "starts_at", "ends_at"]),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="campaign_ends_after_it_starts",
            )
        ]
        # **No uniqueness on the window, deliberately** (P3-05). Campaigns may
        # overlap: an agency runs brands on different cadences, and one brand
        # runs an always-on theme straight through a launch sprint. A
        # constraint here would encode a global clock the product does not have.

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "A campaign ends after it starts."})


class CampaignItem(models.Model):
    """A post's membership of a campaign.

    A join model rather than a plain `ManyToManyField` because *who added this
    and when* is the beginning of the provenance Phase 7 reads when it explains
    why a finding covers the posts it does.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="items")
    post = models.ForeignKey(
        "content.Post", on_delete=models.CASCADE, related_name="campaign_items"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="campaign_additions",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-added_at", "-id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # A post belongs to a campaign once. Racing adds collide here
            # rather than double-counting the campaign's volume.
            models.UniqueConstraint(fields=["campaign", "post"], name="campaign_item_is_unique")
        ]

    def __str__(self) -> str:
        return f"post {self.post_id} in {self.campaign_id}"


class Label(models.Model):
    """A colour a post can be marked with (P3-10).

    Deleting one **cascades off its posts** rather than being refused. A label
    is an organising device, not a record: refusing to delete one because it is
    in use would make tidying up impossible, and the post is unharmed by losing
    a colour.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="labels"
    )
    name = models.CharField(max_length=60)
    #: `#RRGGBB`. Validated rather than free text: the value goes straight into
    #: a style attribute, and "red" renders differently in every browser while
    #: an unvalidated string is somewhere for markup to hide.
    colour = models.CharField(
        max_length=7,
        validators=[
            RegexValidator(r"^#[0-9A-Fa-f]{6}$", "A colour is a hex triple such as #FF5722.")
        ],
    )
    posts = models.ManyToManyField("content.Post", related_name="labels", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="label_name_is_unique_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return self.name


class SavedView(models.Model):
    """A filter someone wants to come back to (P3-10).

    The filter is validated on the way **in**, against the same declaration the
    list endpoint applies. A saved view carrying a key nothing filters on would
    silently show everything — the most dangerous possible failure for a view
    its owner named "Awaiting me".
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="saved_views"
    )
    name = models.CharField(max_length=80)
    filters = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="saved_views",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="saved_view_name_is_unique_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return self.name


class Timetable(models.Model):
    """When this brand prefers to post (P3-11).

    **Local wall time plus an IANA zone, never UTC.** "We post at 9am" is a
    statement about the office clock, so the slot has to move with that clock
    across a daylight-saving shift. Stored as UTC it would silently become 8am
    — or 10am — on one Sunday a year, with nobody having edited anything, and
    the bug would read as the scheduler being unreliable.

    Resolution to UTC happens at the moment a slot is used
    (`services.timetables.resolve_slot`), which is the one place that
    conversion may live.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="timetables"
    )
    name = models.CharField(max_length=80)
    #: An IANA name (`Europe/Paris`), not a fixed offset: an offset cannot
    #: express "this moves in summer", which is the entire requirement.
    timezone = models.CharField(max_length=64, default="UTC")
    #: `[{weekday: 0-6, time: "HH:MM"}]` — Monday is 0, matching
    #: `datetime.weekday()` so no call site has to remember a second convention.
    slots = models.JSONField(default=list, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="timetable_name_is_unique_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.timezone})"


class BulkStatus(models.TextChoices):
    """Shared by the operation and its items.

    **`PARTIAL` and `FAILED` are separate, deliberately.** "Some of it worked"
    and "none of it worked" have different causes and different fixes: the
    second is usually one systemic problem, and reporting it as partial success
    sends the reader hunting through two hundred identical errors looking for
    the interesting one.
    """

    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    DONE = "DONE", "Done"
    PARTIAL = "PARTIAL", "Partly done"
    FAILED = "FAILED", "Failed"


class BulkOperation(models.Model):
    """One user action over many posts (P3-12).

    The counts are **stored, not derived**. A list view showing fifty
    operations would otherwise aggregate over every item row to draw fifty
    progress bars, and the numbers are written by the same transaction that
    settles each item — so they cannot disagree with it.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="bulk_operations"
    )
    action = models.CharField(max_length=32)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=BulkStatus.choices, default=BulkStatus.PENDING)
    total_count = models.IntegerField(default=0)
    succeeded_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bulk_operations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} x {self.total_count} ({self.status})"


class BulkOperationItem(models.Model):
    """One post inside one operation — and its own outcome.

    The per-item row **is** the honest report the P3-G1 gate asks for. Without
    it the only available answers are "it worked" and "it did not", and a batch
    of two hundred where sixteen failed is neither.
    """

    operation = models.ForeignKey(BulkOperation, on_delete=models.CASCADE, related_name="items")
    #: **Nullable, and `SET_NULL` rather than `CASCADE`.** `delete` is one of
    #: the bulk actions, so a cascading FK would have the operation erase its
    #: own audit trail: the two hundred items that succeeded would vanish along
    #: with their posts, and the report would show only the failures — a batch
    #: that deleted everything correctly would read as one that did nothing but
    #: fail. Caught by `test_bulk_deleting_drafts`, which could not even save
    #: the item row afterwards.
    post = models.ForeignKey(
        "content.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bulk_operation_items",
    )
    #: The id as it was when the batch was planned, kept because `post` goes
    #: null the moment a `delete` item succeeds. The report has to be able to
    #: say *which* post, and "null" is not an answer to that.
    post_ref = models.IntegerField()
    status = models.CharField(max_length=10, choices=BulkStatus.choices, default=BulkStatus.PENDING)
    #: The reason this item did not work, in the words the service used. Blank
    #: on success — never a placeholder, which would read as an error to
    #: anything scanning for one.
    error = models.TextField(blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # On `post_ref`, never on `post`: Postgres treats repeated NULLs
            # as distinct, so a constraint over the nullable FK would stop
            # constraining anything the moment the first delete succeeded —
            # the same trap P1-06 found in the post↔media join.
            models.UniqueConstraint(
                fields=["operation", "post_ref"], name="bulk_item_is_unique_per_operation"
            )
        ]

    def __str__(self) -> str:
        return f"post {self.post_ref} in operation {self.operation_id} ({self.status})"
