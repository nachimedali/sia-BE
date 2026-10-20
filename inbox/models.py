"""The audience inbox (steps-plan S2).

An `AudienceComment` is immutable: it is what the audience wrote, as the
platform holds it (C-03). Everything a teammate adds — an intent, an assignee, a
status, a note — therefore lives in its own row *pointing at* the comment, and
none of it is ever sent to a platform. Absence of a row is meaningful: no coding
row means "never looked at", a coding row with no intent means "looked, could
not tell", and neither is dressed up as a guess.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models


class Intent(models.TextChoices):
    QUESTION = "question", "Question"
    OBJECTION = "objection", "Objection"
    COMPLAINT = "complaint", "Complaint"
    PRAISE = "praise", "Praise"
    PURCHASE_INTENT = "purchase_intent", "Purchase intent"
    PRODUCT_REQUEST = "product_request", "Product request"


class CodingSource(models.TextChoices):
    RULES = "rules", "Rules"
    MODEL = "model", "Text model"
    MANUAL = "manual", "A teammate"


class CommentCoding(models.Model):
    """What a comment is *about*. `intent` is null when the coder looked and
    could not tell — "uncoded", which is not "neutral" and not "praise". The
    row still exists so the automatic pass does not retry it forever, and so a
    teammate's own call (`MANUAL`) is never overwritten by it."""

    comment = models.OneToOneField(
        "analytics.AudienceComment", on_delete=models.CASCADE, related_name="coding"
    )
    # Null, not "": the spec's "uncoded stays null", and a filter on `intent__isnull`
    # then cannot mistake an empty string for a label.
    intent = models.CharField(  # noqa: DJ001
        max_length=16, choices=Intent.choices, null=True, blank=True
    )
    source = models.CharField(max_length=8, choices=CodingSource.choices)
    coded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.comment_id}: {self.intent or 'uncoded'}"


class TriageStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"


class CommentTriage(models.Model):
    """Who is handling a comment and whether it is finished. No row means open
    and unassigned."""

    comment = models.OneToOneField(
        "analytics.AudienceComment", on_delete=models.CASCADE, related_name="triage"
    )
    status = models.CharField(max_length=8, choices=TriageStatus.choices, default=TriageStatus.OPEN)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.comment_id}: {self.status}"


class CommentNote(models.Model):
    """A teammate talking to the team about a comment. Workspace-internal, and
    deliberately not the audience's thread nor the pre-publish `Thread`."""

    comment = models.ForeignKey(
        "analytics.AudienceComment", on_delete=models.CASCADE, related_name="notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["created_at", "id"]

    def __str__(self) -> str:
        return f"note on {self.comment_id}"


class SavedReply(models.Model):
    """A team snippet to start a reply from (S2-05). Workspace-scoped; using one
    only fills the reply box — nothing is sent until a person sends it."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="saved_replies"
    )
    title = models.CharField(max_length=80)
    body = models.CharField(max_length=1000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["title", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["workspace", "title"], name="unique_saved_reply_title")
        ]

    def __str__(self) -> str:
        return self.title
