"""The taste model, the candidate lifecycle and the decision log (Phase 5).

**This is the moat.** Two assets accumulate here that a competitor cannot ship
as a feature: a per-workspace taste model grown from every accept and reject,
and the structured decision history that grows it. Everything else in the
product is table stakes; this is the part that gets better by being used.

Three ideas, deliberately separate models:

* `TasteProfile` — what this brand sounds like, **versioned**. Without
  versioning, a change in acceptance rate cannot be attributed to a change in
  the profile, which makes the whole loop unfalsifiable (C-08).
* `ContentCandidate` — a proposal, upstream of and distinct from `Post`.
  Nothing reaches the publish pipeline without a human approving one (C-01,
  L-2).
* `Decision` — append-only, one row per verdict, carrying the versions that
  produced the thing being judged. A rejection with no record of *what was
  rejected under which profile* teaches nothing.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from common.records import AppendOnly


class TasteProfile(models.Model):
    """One versioned profile per workspace — **the brand is the workspace**
    (L-1, C-08).

    Voice and restrictions used to be split between `Product` and a workspace
    `VoiceProfile`, and neither was versioned. Products keep narrow overrides
    (`ProductTasteOverride`) — a product-specific restriction or reference set
    — never a separate voice identity, because a brand that sounds different
    per product is not a brand.

    **Versioned, and the version is load-bearing.** Every `ContentCandidate`
    and every `Decision` carries the version it was produced under, so "did
    acceptance improve after we changed the voice" is a question with an
    answer. A profile edited in place would make it unanswerable.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="taste_profiles"
    )
    version = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False)

    #: **Blocking.** Brand policy — never mention competitors, no alcohol
    #: imagery, always include a CTA. Judged by `services.screening`, which is
    #: a *different layer* from the quality gate (C-09): a candidate can be a
    #: technically perfect image that violates policy, and merging the two
    #: checks is the failure to guard against.
    hard_constraints = models.JSONField(default=dict, blank=True)
    #: Tone, reading level, formality, humour, emoji policy, language mix.
    #: Guidance for the generator, not a gate — `voice` shapes the prompt where
    #: `hard_constraints` refuses the output.
    voice = models.JSONField(default=dict, blank=True)
    #: Lengths per platform, hashtag count, hook patterns, CTA styles.
    structural = models.JSONField(default=dict, blank=True)
    #: `{favour: [...], avoid: [...], seasonal: [...]}` — what this brand talks
    #: about. Trend stage 6 filters ranked clusters through it (C-10).
    topic_posture = models.JSONField(default=dict, blank=True)

    #: Exemplary **and counter-example** posts. Both, because "more like this"
    #: and "never like this" are different instructions and a single list
    #: collapses them into one.
    reference_examples = models.ManyToManyField(
        "content.Post", blank=True, related_name="taste_references"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="taste_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-version"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "version"], name="taste_profile_version_is_unique"
            ),
            # One active profile per workspace. A partial unique index rather
            # than a boolean anyone can set twice: "which profile generated
            # this" must have exactly one answer at any moment.
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_active=True),
                name="one_active_taste_profile_per_workspace",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id} taste v{self.version}"


class ProductTasteOverride(models.Model):
    """A product's **narrow** deviations from the brand's taste (C-08).

    Constraints and references only. There is deliberately no `voice` here: a
    product that sounded different from its brand would make the workspace-level
    profile a suggestion rather than the identity, which is the split C-08
    exists to undo.
    """

    product = models.OneToOneField(
        "products.Product", on_delete=models.CASCADE, related_name="taste_override"
    )
    constraints = models.JSONField(default=dict, blank=True)
    references = models.ManyToManyField(
        "content.Post", blank=True, related_name="product_taste_references"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"taste override for product {self.product_id}"


class RuleSet(models.Model):
    """A versioned set of rules the generator follows (P5-05).

    Proposed by Phase 7's Learn and **activated only by a human** (Part 7
    rule 14). The activation is the product: a system that applied its own
    conclusions would be one whose mistakes compound silently.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="rulesets"
    )
    version = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-version"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "version"], name="ruleset_version_is_unique"
            ),
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_active=True),
                name="one_active_ruleset_per_workspace",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id} rules v{self.version}"


class RuleKind(models.TextChoices):
    """What a rule constrains. A closed vocabulary, because a rule kind the
    generator does not understand is a rule that silently does nothing."""

    STRUCTURE = "STRUCTURE", "Structure"
    TIMING = "TIMING", "Timing"
    TOPIC = "TOPIC", "Topic"
    FORMAT = "FORMAT", "Format"
    VOICE = "VOICE", "Voice"


class Rule(models.Model):
    """One accepted conclusion.

    `provenance` — the `Finding` this rule came from — **arrives with Phase 7**,
    which is the phase that builds `Finding`. Adding a FK to a model that does
    not exist is not possible, and a nullable integer pretending to be one is
    worse than the column's absence: it would type-check, resolve to nothing,
    and look like traceability. The same ordering call P2-06 made for
    `Decision`. Acceptance criterion 11 ("a rule proposal traces backward to
    the findings that produced it") is Phase 7's to satisfy.
    """

    ruleset = models.ForeignKey(RuleSet, on_delete=models.CASCADE, related_name="rules")
    kind = models.CharField(max_length=16, choices=RuleKind.choices)
    payload = models.JSONField(default=dict, blank=True)

    #: Who said yes, and when. Null on a *proposed* rule that nobody has
    #: accepted yet — which is how "proposed" and "active" are told apart
    #: without a second status column that could disagree with this one.
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_rules",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]

    def __str__(self) -> str:
        return f"{self.kind} rule in {self.ruleset_id}"


class CandidateState(models.TextChoices):
    """The lifecycle from BUILD-PLAN Phase 5.

    `generated → screened_out | pending_review`, and from `pending_review` to
    `rejected`, `regenerating`, `expired` or `approved`. `approved` is the only
    state that may materialise a `Post`, and `services.candidates` is the only
    place that happens.
    """

    GENERATED = "generated", "Generated"
    SCREENED_OUT = "screened_out", "Screened out"
    PENDING_REVIEW = "pending_review", "Pending review"
    REJECTED = "rejected", "Rejected"
    REGENERATING = "regenerating", "Regenerating"
    EXPIRED = "expired", "Expired"
    APPROVED = "approved", "Approved"


class ContentCandidate(models.Model):
    """A proposal — **upstream of and distinct from `Post`** (C-01, L-2).

    The existing `Generation` stays what it is: the record of a provider call.
    This is the reviewable thing a person says yes or no to, and the two are
    separate because one generation can yield several candidates and a
    candidate can outlive the call that made it.

    **Nothing reaches the publish pipeline without `approved`.** No bypass at
    any tier, enforced in `services.candidates.materialise` and tested as a
    boundary (P5-G3).
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="candidates"
    )
    state = models.CharField(
        max_length=16, choices=CandidateState.choices, default=CandidateState.GENERATED
    )
    #: What would be posted: `{master_body, media_asset_ids, platform_options}`.
    payload = models.JSONField(default=dict, blank=True)

    product = models.ForeignKey(
        "products.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="candidates",
    )
    generation = models.ForeignKey(
        "ai.Generation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="candidates",
    )
    campaign = models.ForeignKey(
        "planning.Campaign",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="candidates",
    )

    #: **Non-null from the first migration** (P5-01's risk row). There is no
    #: unversioned path to skip to under deadline, so a candidate always knows
    #: which profile produced it.
    taste_profile = models.ForeignKey(
        TasteProfile, on_delete=models.PROTECT, related_name="candidates"
    )
    ruleset = models.ForeignKey(
        RuleSet, null=True, blank=True, on_delete=models.PROTECT, related_name="candidates"
    )

    #: The candidate this one replaces, and **why the first was rejected**
    #: (P5-09). The reason travels so the new prompt can carry it: a
    #: regeneration that ignores why the first attempt failed reproduces the
    #: failure, which is a test rather than a code comment.
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="regenerations"
    )
    parent_reason_code = models.CharField(max_length=32, blank=True)

    #: Why screening refused it. Logged and **never surfaced** (P5-08), so the
    #: hard-constraint violation rate is measurable — a rising rate signals
    #: prompt or profile drift and is monitored rather than merely recorded.
    screened_reason = models.TextField(blank=True)

    #: Defaults to the campaign boundary (P5-10). Expiry is a **weak** signal,
    #: weighted below explicit rejection: a candidate nobody looked at says
    #: something about the queue, not about the content.
    expires_at = models.DateTimeField(null=True, blank=True)

    post = models.ForeignKey(
        "content.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="from_candidates",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            # The approval queue's own question, asked on every page load.
            models.Index(fields=["workspace", "state", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"candidate {self.pk} ({self.state})"

    def clean(self) -> None:
        if self.taste_profile_id and self.workspace_id:
            profile_workspace = TasteProfile.objects.filter(pk=self.taste_profile_id).values_list(
                "workspace_id", flat=True
            )
            if profile_workspace and profile_workspace[0] != self.workspace_id:
                raise ValidationError(
                    {"taste_profile": "A candidate cannot be judged against another brand's taste."}
                )


class Verdict(models.TextChoices):
    ACCEPTED = "accepted", "Accepted"
    ACCEPTED_WITH_EDITS = "accepted_with_edits", "Accepted with edits"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"


#: The reason vocabulary (P5-12). **Fixed and admin-extensible**, and fixed is
#: the point: structured codes are what make rejections aggregable — *"63% of
#: your rejections this month were off_brand_voice"* routes to a specific
#: profile revision, and free text cannot do that at all.
REASON_CODES: tuple[str, ...] = (
    "off_brand_voice",
    "factually_wrong",
    "wrong_timing",
    "topic_not_relevant",
    "too_promotional",
    "weak_hook",
    "formatting_issue",
    "duplicate_of_recent",
    "legal_or_policy_risk",
    "good_but_not_now",
    "other",
)


class Decision(AppendOnly):
    """One verdict on one candidate — **the dataset the moat is made of**.

    Append-only because a decision log that can be edited is not evidence. A
    changed mind is a new row on a new candidate, never a rewrite of what
    somebody actually thought on the day.

    **Carries the versions that produced what was judged.** Without
    `taste_profile_version`, `ruleset_version`, `model_identity` and
    `prompt_template_version`, a change in acceptance rate has five possible
    causes and no way to tell them apart — attribution is impossible without
    all of them, which is why they are columns rather than a hopeful JSON blob.
    """

    append_only_hint = "record a new decision instead of editing this one."

    candidate = models.ForeignKey(
        ContentCandidate, on_delete=models.CASCADE, related_name="decisions"
    )
    #: The payload **exactly as the reviewer saw it**. Stored rather than read
    #: back off the candidate: an edit after the fact would otherwise rewrite
    #: history, and "what did they actually say yes to" is the question this
    #: table exists to answer.
    payload_as_shown = models.JSONField(default=dict, blank=True)

    taste_profile_version = models.PositiveIntegerField()
    ruleset_version = models.PositiveIntegerField(null=True, blank=True)
    prompt_context = models.JSONField(default=dict, blank=True)
    model_identity = models.CharField(max_length=120, blank=True)
    prompt_template_version = models.CharField(max_length=40, blank=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decisions",
    )
    verdict = models.CharField(max_length=24, choices=Verdict.choices)
    reason_code = models.CharField(max_length=32, blank=True)
    note = models.TextField(blank=True)

    #: **The highest-value signal in the system** (P5-13). An `accepted_with_
    #: edits` diff shows exactly what the model got wrong *where the user cared
    #: enough to fix rather than discard*. Retained 13 months, `admin` to read,
    #: and it never leaves the tenant boundary or enters a cohort aggregate —
    #: Part 7 rule 16, enforced by the Phase 8 projection it is excluded from.
    edit_diff = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["candidate", "-created_at"]),
            # "Why are we rejecting things this month" — the aggregation the
            # reason vocabulary exists for.
            models.Index(fields=["verdict", "reason_code"]),
        ]

    def __str__(self) -> str:
        return f"{self.verdict} on candidate {self.candidate_id}"
