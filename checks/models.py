"""Pre-publish check results (steps-plan S4).

One `CheckRun` per run, holding every result it produced. **Per content, not
per post**: `fingerprint` is a hash of exactly what the checks read — the
caption, the media in order, the platforms and their formats — so a result is
known to describe the post as it is now, or known to be out of date. `revision`
is the post's revision number at the time, for people to read.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models


class CheckVerdict(models.TextChoices):
    PASS = "PASS", "Pass"
    FIX = "FIX", "Fix required"
    BLOCK = "BLOCK", "Block"


class CheckStatus(models.TextChoices):
    """One result's outcome. `UNAVAILABLE` is a check that did not run — no
    vendor configured, or nothing it can read — and is never counted as a pass."""

    PASS = "PASS", "Pass"
    FIX = "FIX", "Fix required"
    BLOCK = "BLOCK", "Block"
    UNAVAILABLE = "UNAVAILABLE", "Not checked"


class CheckRunState(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    DONE = "DONE", "Done"


class CheckRun(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="check_runs"
    )
    post = models.ForeignKey("content.Post", on_delete=models.CASCADE, related_name="check_runs")
    revision = models.PositiveIntegerField(null=True, blank=True)
    fingerprint = models.CharField(max_length=64)
    state = models.CharField(
        max_length=8, choices=CheckRunState.choices, default=CheckRunState.RUNNING
    )
    verdict = models.CharField(max_length=8, choices=CheckVerdict.choices, blank=True)
    #: `{"PASS": n, "FIX": n, "BLOCK": n, "UNAVAILABLE": n}` — what a badge shows.
    counts = models.JSONField(default=dict, blank=True)
    results = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["post", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"checks {self.pk} on post {self.post_id}: {self.verdict or self.state}"
