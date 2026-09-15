"""Learn — digests, findings and the confidence they are graded with.

BUILD-PLAN Phase 7. Measurement becomes readable conclusions, with uncertainty
stated rather than implied, and rule proposals that trace back to the evidence
that produced them.

**Two invariants live in this module rather than in a caller.**

*Every finding carries its sample size and its grade* (P7-06, P7-07). Both are
non-null columns, so a finding that cannot say how much data it rests on is not
representable — the alternative, a nullable column that surfaces default to
hiding, is how an `n=3` conclusion ends up being read as a fact.

*Nothing here is edited.* `Digest` and `Finding` are append-only. A digest is a
document somebody reads, quotes and acts on; a re-run that rewrote last month's
findings in place would change what a customer was told after they were told it,
and nothing in the row would say so. A second run writes a second digest.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models

from common.records import AppendOnly


class Confidence(models.TextChoices):
    """How much weight a finding can carry (BUILD-PLAN Phase 7).

    The thresholds are deliberately blunt and deliberately *not* configurable
    per workspace: a grade that means something different for two customers is
    not a grade. They are admin-editable as seeded thresholds if they ever need
    to move, but they move for everybody at once.
    """

    STRONG = "STRONG", "Strong"
    EMERGING = "EMERGING", "Emerging"
    INSUFFICIENT = "INSUFFICIENT", "Insufficient"


class NarrationSource(models.TextChoices):
    """Which half of P7-08/P7-09 produced the prose on a digest.

    Recorded rather than inferred because the fallback is not an error and must
    not look like one: a digest narrated from templates is a *correct* digest
    whose provider either declined or tried to introduce a number that was not
    in the payload. Knowing which happened is how a rising fallback rate gets
    noticed at all.
    """

    MODEL = "MODEL", "Model narration"
    TEMPLATE = "TEMPLATE", "Template fallback"


class Digest(AppendOnly):
    """One reading of one window, scoped to a campaign.

    **Scoped to the campaign; computed over a trailing window** (P7-05). The
    distinction is the whole reason early digests are useful: a two-week
    campaign at three posts a week yields n=6, which is below even the Emerging
    floor, so a digest whose statistics stopped at the campaign boundary would
    read "Insufficient" on every line and the feature would look broken on the
    day it shipped. `window_start`/`window_end` are therefore the *statistical*
    window and are routinely wider than the campaign they belong to.
    """

    append_only_hint = "run Learn again to produce a new digest."

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="digests"
    )
    #: Null for an on-demand run over a workspace rather than a campaign. The
    #: campaign side of the pair is `Campaign.digest` (1:1, nullable).
    campaign = models.ForeignKey(
        "planning.Campaign",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="digests",
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()

    #: The prose, already validated against the statistics payload (P7-09).
    narration = models.TextField(blank=True)
    narration_source = models.CharField(
        max_length=10, choices=NarrationSource.choices, default=NarrationSource.TEMPLATE
    )
    #: The exact structured payload the narrator was given. Kept so a disputed
    #: sentence can be checked against the numbers it was supposed to render,
    #: which is the only way P7-08's split is auditable after the fact.
    statistics = models.JSONField(default=dict, blank=True)

    #: The versions in force when this ran. Same reasoning as `Decision`: a
    #: change in what the digests say has several possible causes and no way to
    #: tell them apart without these.
    taste_profile_version = models.PositiveIntegerField(null=True, blank=True)
    ruleset_version = models.PositiveIntegerField(null=True, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_digests",
    )

    class Meta:
        ordering: ClassVar[list[str]] = ["-generated_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-generated_at"]),
            models.Index(fields=["campaign", "-generated_at"]),
        ]

    def __str__(self) -> str:
        return f"Digest {self.pk} for workspace {self.workspace_id}"


class Finding(AppendOnly):
    """One segment, compared against its baseline, graded.

    `segment` says *which slice* — `{"dimension": "format", "value": "CAROUSEL"}`.
    `comparison` says *what was measured*, in code, as numbers: the segment's
    mean, the baseline's, the count of posts on each side and how often the
    segment led. **No prose and no predictions live here** — the narrator reads
    this and may not add to it (P7-08).

    `excluded_reason` is set when a segment was measurable but deliberately
    dropped — a metric that went unavailable mid-window is C-07's rule applied
    here, and the digest has to say that it was excluded and why rather than
    quietly shrinking the denominator.
    """

    append_only_hint = "run Learn again to produce new findings."

    digest = models.ForeignKey(Digest, on_delete=models.CASCADE, related_name="findings")
    segment = models.JSONField(default=dict, blank=True)
    comparison = models.JSONField(default=dict, blank=True)

    confidence = models.CharField(max_length=12, choices=Confidence.choices)
    #: Non-null on purpose (P7-07). Every finding displays this; a finding that
    #: cannot say how many posts it rests on has nothing to display and should
    #: never have been written.
    sample_size = models.PositiveIntegerField()
    #: Posts in the trailing baseline this segment was compared against — the
    #: other half of "how much data is this", and the half a reader forgets to
    #: ask for.
    baseline_size = models.PositiveIntegerField(default=0)
    #: How many distinct campaigns the segment's effect held across. Part of
    #: the Strong bar: an effect seen once is not yet a pattern.
    campaigns_observed = models.PositiveIntegerField(default=1)

    excluded_reason = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["digest", "confidence"]),
        ]

    def __str__(self) -> str:
        return f"{self.segment} ({self.confidence}, n={self.sample_size})"
