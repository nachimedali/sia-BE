"""The six quick utilities (design.md §8.10, C-11 / P0-04).

**Built rather than removed.** C-11 says a shipped 404 is worse than an absent
feature, and gave two options for each declared-but-unbuilt surface. Tools is
six small forms over machinery that already exists — the AI pipeline, the trend
corpus, the analytics signals — so building it costs an app and removing it
would cost a route the frontend already renders.

The distinguishing property against `ai.Generation`: **a tool answers in the
request**. Everything else in this system splits DB-work-in-request from
provider-work-on-a-queue (design.md §11), and for good reason. A tool is the
scoped exception: the output is a paragraph, the user is watching, and a job id
they have to poll for a headline is worse product than a two-second wait. The
exception is scoped by cost — one credit — and by the fact that no tool touches
a product, a voice profile or a publishing account.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models


class Tool(models.TextChoices):
    HEADLINE = "headline", "Headline"
    BIO = "bio", "Bio"
    HASHTAG = "hashtag", "Hashtag"
    HOOK = "hook", "Hook"
    THREAD_SPLITTER = "thread-splitter", "Thread splitter"
    BEST_TIME = "best-time", "Best time to post"


class ToolConfig(models.Model):
    """One row per tool, seeded and admin-editable.

    `credits_cost` is a commercial number, so it is a column rather than a
    constant (Part 7 rule 10), and `is_enabled` lets an operator take one tool
    down — a provider outage, a bad prompt — without a deploy and without the
    other five going with it.
    """

    slug = models.CharField(max_length=32, choices=Tool.choices, unique=True)
    display_name = models.CharField(max_length=80)
    is_enabled = models.BooleanField(default=True)
    credits_cost = models.PositiveIntegerField(default=1)
    sort_order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["sort_order", "slug"]

    def __str__(self) -> str:
        return self.display_name


class ToolUsage(models.Model):
    """What one run produced, and what it cost.

    Kept rather than discarded because the credit was spent: a user who is
    charged and then loses the output to a page refresh has been charged for
    nothing, and support has no way to see what happened.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="tool_usages"
    )
    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tool_usages",
    )
    tool = models.CharField(max_length=32, choices=Tool.choices)
    payload = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    credits_charged = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tool} for {self.workspace_id}"
