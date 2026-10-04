"""Generation (design.md §6.5, §8.3, I1, I2, I7).

`Generation.trend_cluster`/`recipe` — design.md §6.5 lists `trend_cluster→
TrendCluster (null)` and `recipe→CreativeRecipe (null)`; both models arrive in
Phase 10/14. Deferred the same way `Post.product`/`generation` were before
this phase (A32/A47/A48/A60).

`GenerationMode` declares the full enum design.md names, but only a subset is
actually invocable in Phase 7 — `ai.services.pipeline.ALLOWED_MODES` is what
enforces that (design.md §15.8 A69), the same "declare the full enum, gate
what's reachable" shape `PostStatus` used in Phase 4 (A47).
"""

from __future__ import annotations

import re
from typing import ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class GenerationKind(models.TextChoices):
    TEXT = "TEXT", "Text"
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class GenerationMode(models.TextChoices):
    IDEA = "IDEA", "Idea"
    TREND = "TREND", "Trend"
    REPURPOSE = "REPURPOSE", "Repurpose"
    REWRITE = "REWRITE", "Rewrite"
    PRODUCT = "PRODUCT", "Product"
    AUTOPILOT = "AUTOPILOT", "Autopilot"
    RECIPE = "RECIPE", "Recipe"
    REVISION = "REVISION", "Revision"
    #: Caption written from an image the workspace already owns (P1-13). A
    #: vision call, so it needs `Generation.source_media` — a caption mode with
    #: no image is a text generation wearing the wrong name.
    CAPTION = "CAPTION", "Caption"
    #: Post ideas grounded in the workspace's own top-percentile posts (P1-13).
    SUGGEST = "SUGGEST", "Suggest"


class GenerationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class VoiceProfile(models.Model):
    """design.md §6.5. `exemplar_post_ids` is a plain id list rather than an
    m2m — exemplars are a handful of the workspace's own posts picked for
    prompt grounding, not a relationship anything queries from the other
    side."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="voice_profiles"
    )
    name = models.CharField(max_length=120)
    tone_descriptors = models.JSONField(default=list, blank=True)
    banned_phrases = models.JSONField(default=list, blank=True)
    exemplar_post_ids = models.JSONField(default=list, blank=True)
    system_prompt = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        return self.name


class Generation(models.Model):
    """One request to a provider, grounded and quality-gated (design.md §8.3).

    `credits_charged`/`video_units_charged` record what was actually debited
    — the ledger row is the source of truth (I4), these are a denormalised
    read for "what did this generation cost" without joining the ledger.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="generations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="generations"
    )
    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    mode = models.CharField(max_length=16, choices=GenerationMode.choices)
    prompt = models.TextField(blank=True)

    product = models.ForeignKey(
        "products.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generations",
    )
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generations",
    )
    voice_profile = models.ForeignKey(
        VoiceProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="generations"
    )
    parent_generation = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions"
    )
    #: The image a `CAPTION` generation read (P1-13). Nullable because every
    #: other mode has no source image, and `SET_NULL` because deleting the
    #: picture must not delete the record of what was generated from it.
    source_media = models.ForeignKey(
        "content.MediaAsset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="captions",
    )

    output_type = models.CharField(max_length=32, blank=True)
    aspect = models.CharField(max_length=16, blank=True, default="1:1")
    render_style = models.CharField(max_length=64, blank=True)
    scene = models.CharField(max_length=200, blank=True)
    motion = models.CharField(max_length=64, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)
    #: **Every Studio control, as the user set it** — scene, light, camera,
    #: who is in the frame, moods, palette, language, call to action, format,
    #: the three sliders, the toggles, what to keep out, which platforms.
    #: Option *keys* (validated against `CreativeOption` before they get here),
    #: never labels, so renaming "Golden hour" in admin does not rewrite what an
    #: old generation was asked for. `{}` for every caller that is not the
    #: Studio — autopilot, revisions, captions — which is what keeps them
    #: priced and rendered exactly as before. `scene`/`render_style`/`aspect`
    #: above stay populated for the same compatibility reason.
    creative = models.JSONField(default=dict, blank=True)

    is_batch = models.BooleanField(default=False)
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)

    # --- provenance (P5-15) ------------------------------------------------
    # **Attribution is impossible without all of these.** A change in
    # acceptance rate otherwise has five candidate causes — the model, the
    # prompt, the taste profile, the rule set, or the content itself — and no
    # way to tell them apart. `provider`/`model` and the token counts above
    # cover the first and the cost; these three cover the rest.
    #
    # Null on a generation that predates Phase 5, and on any run with no taste
    # profile behind it. Null rather than zero: "we did not record it" and
    # "version 0" are different claims, and only the first is true.
    taste_profile_version = models.PositiveIntegerField(null=True, blank=True)
    ruleset_version = models.PositiveIntegerField(null=True, blank=True)
    prompt_template_version = models.CharField(max_length=40, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    credits_charged = models.PositiveIntegerField(default=0)
    #: How many variants this generation's buyer paid to keep (X-09). The
    #: engine renders `GenerationCost.variant_pool`, which is larger; the
    #: surplus is locked until bought. Stored on the row rather than derived
    #: from the ledger because it is what the *selection* rule reads on every
    #: click, and a rule that re-derives its own limit from money already
    #: spent gets the answer wrong the moment a refund exists.
    paid_slots = models.PositiveSmallIntegerField(default=1)
    #: How many variants the engine was told to render (X-09). **Zero means
    #: "no surplus"** — render exactly what the caller asked for, which is
    #: what autopilot, revisions, captions and suggestions all want: none of
    #: them has a human looking at a dock, so a pool would be provider spend
    #: with nobody to sell it to. Only Studio sets it, and it is stored rather
    #: than re-resolved so a retuned pool column never changes what an old
    #: generation is understood to have offered.
    variant_pool = models.PositiveSmallIntegerField(default=0)
    video_units_charged = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=GenerationStatus.choices, default=GenerationStatus.PENDING
    )
    # Populated on the terminal FAILED state — the quality gate's rejection
    # reasons, or a provider error, surfaced to the caller without a join.
    error_detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.mode} {self.pk} ({self.status})"

    @property
    def model_identity(self) -> str:
        """`provider/model`, or whichever half is known.

        One string because that is what a `Decision` records and what a digest
        groups by — a reader comparing the two halves separately would have to
        reimplement this join, and would eventually do it differently.
        """
        return "/".join(part for part in (self.provider, self.model) if part)


class GenerationVariant(models.Model):
    """One candidate output. `was_selected` marks the one the user actually
    used (composed into a post, kept as the revision's basis) — a generation
    can produce several variants and none, one, or several may end up used."""

    generation = models.ForeignKey(Generation, on_delete=models.CASCADE, related_name="variants")
    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    body = models.TextField(blank=True)
    media_asset = models.ForeignKey(
        "content.MediaAsset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generation_variants",
    )
    #: The line set over the picture. Studio image variants only; `body` is
    #: their caption. Both are editable before the variant is sent, which is
    #: why they are columns and not read back out of the provider's reply.
    headline = models.CharField(max_length=200, blank=True)
    #: Observed tags from the category corpus (counted, never invented — see
    #: `ai.services.hashtags`). Empty when the toggle was off or no corpus exists.
    hashtags = models.JSONField(default=list, blank=True)
    #: How closely the render matches the product's reference photo, 0-1, as
    #: the quality gate measured it. **Null** for text variants and for rows
    #: from before this was kept — unknown is not a low score.
    identity_score = models.FloatField(null=True, blank=True)
    platform = models.CharField(max_length=16, blank=True)
    rank = models.PositiveSmallIntegerField(default=0)
    rationale = models.CharField(max_length=300, blank=True)
    was_selected = models.BooleanField(default=False)

    #: **Bought beyond the paid slots** (X-09). A generation renders a pool
    #: larger than the slots the user paid for; selecting one of the extras
    #: costs `GenerationCost.unlock_percent` of the per-variant price. An
    #: unlocked variant stops counting against the free allowance, which is
    #: what lets the two rules — "only N free" and "the rest are buyable" —
    #: coexist without either needing to know about the other.
    is_unlocked = models.BooleanField(default=False)
    #: What was actually charged to unlock it, not what the table says today.
    #: A price retuned in admin must not rewrite what a customer already paid,
    #: and an audit that recomputes the figure would do exactly that.
    unlock_charged = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["rank", "id"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["generation", "rank"])]

    def __str__(self) -> str:
        return f"variant {self.pk} of generation {self.generation_id} (rank {self.rank})"


class GenerationCost(models.Model):
    """The §4.2 credit table, as rows (I8) — never a code constant.

    Resolution is exact `(kind, mode, provider, model)` → `(kind, mode)` →
    `(kind)`; no match is a hard error, never a silent zero (design.md A10).
    Blank `mode`/`provider`/`model` is the wildcard for a fallback row —
    `ai.services.costing.resolve_cost` is the only reader of this table.
    """

    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    mode = models.CharField(max_length=16, choices=GenerationMode.choices, blank=True)
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)
    credits = models.PositiveIntegerField()
    #: How many variants the engine renders for this pair, regardless of how
    #: many the user paid for (X-09). Larger than the usual purchase on
    #: purpose: the surplus is what there is to upsell. A commercial number,
    #: so it is a column an operator retunes and never a constant (rule 10).
    variant_pool = models.PositiveSmallIntegerField(default=4)
    #: What one surplus variant costs, as a percentage of `credits`, rounded
    #: **up** — half of a 3-credit image is 2, never 1. Also a commercial
    #: number, also admin-editable, and deliberately a percentage rather than
    #: a second price so retuning `credits` carries the upsell with it.
    unlock_percent = models.PositiveSmallIntegerField(default=50)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["kind", "mode", "provider", "model"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "mode", "provider", "model"], name="unique_generation_cost_row"
            )
        ]

    def __str__(self) -> str:
        specificity = "/".join(filter(None, [self.mode, self.provider, self.model])) or "any"
        return f"{self.kind} ({specificity}): {self.credits} credits"


class QualityCheck(models.Model):
    """One quality-gate attempt (design.md §8.3). Every attempt is persisted,
    passed or not — the evaluation harness and I2's audit trail both read this
    table, and a rejected attempt is never shown to the user but is never
    silently discarded either."""

    generation = models.ForeignKey(
        Generation, on_delete=models.CASCADE, related_name="quality_checks"
    )
    variant = models.ForeignKey(
        GenerationVariant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quality_checks",
    )
    checks = models.JSONField(default=dict, blank=True)
    identity_score = models.FloatField(null=True, blank=True)
    passed = models.BooleanField()
    attempt = models.PositiveSmallIntegerField()
    rejected_reason = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["generation", "attempt"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["generation", "attempt"])]

    def __str__(self) -> str:
        outcome = "passed" if self.passed else "rejected"
        return f"generation {self.generation_id} attempt {self.attempt}: {outcome}"


class QualityGateConfig(models.Model):
    """Singleton (design.md §8.3: "admin-tunable threshold"). Not a `Plan`/
    `GenerationCost`-style per-transaction row (I8 governs commercial numbers;
    this is an engineering quality bar) — one row, edited in admin, read
    through `get_solo()` the same shape Phase 6's `PUBLISH_RESERVE_RATIO`
    reasoned about for its own constant (design.md A64), except this one *is*
    product-facing enough to warrant admin editing without a deploy."""

    identity_similarity_threshold = models.FloatField(default=0.6)
    max_regeneration_attempts = models.PositiveSmallIntegerField(default=3)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "Quality gate configuration"

    @classmethod
    def get_solo(cls) -> QualityGateConfig:
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance


# -----------------------------------------------------------------------------
# The Studio's controls, as rows
# -----------------------------------------------------------------------------
class CreativeKind(models.TextChoices):
    """Which control a `CreativeOption` row belongs to.

    A new *kind* is a new control and costs code; a new *row* in an existing
    kind is a new choice and costs nothing — an operator adds a scene in admin
    and it is on every workspace's Studio the next time it loads.
    """

    SCENE = "scene", "Scene"
    LIGHT = "light", "Light"
    CAMERA = "camera", "Camera angle"
    CAST = "cast", "Who is in the frame"
    VIBE = "vibe", "Cast vibe"
    MOOD = "mood", "Mood"
    PALETTE = "palette", "Palette"
    LANGUAGE = "language", "Content language"
    CTA = "cta", "Call to action"
    FORMAT = "format", "Format"
    TEMPO = "tempo", "Tempo (creativity)"
    DYNAMICS = "dynamics", "Dynamics (colour intensity)"
    TONE = "tone", "Voice"
    TOGGLE = "toggle", "On/off option"
    PRESET = "preset", "Preset"
    QUICK_TAG = "quick_tag", "Brief quick tag"
    # The product form's choice lists (`/app/products/new`): rows for the same
    # reason the Studio's are — a new audience or claim is an admin edit.
    AUDIENCE = "audience", "Product audience"
    TONE_PRESET = "tone_preset", "Voice preset"
    CLAIM = "claim", "Claim that needs proof"
    SHOT_TAG = "shot_tag", "Photo shot type"
    ASPECT = "aspect", "Image aspect"
    SUGGESTION = "suggestion", "Suggested entry"
    # The post editor's "What should change?" chips: why a post is being
    # regenerated, each carrying the instruction the model reads.
    REVISE_REASON = "revise_reason", "Why regenerate"


#: `d` attribute of an SVG `<path>`: commands, numbers, separators. Nothing
#: else — these strings are rendered from the database into the page, so the
#: only safe icon is one that cannot be anything but a path.
_PATH_DATA = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]+$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate_icon_paths(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ValidationError("icon_paths must be a list of SVG path strings.")
    bad = [p for p in value if len(p) > 600 or not _PATH_DATA.match(p)]
    if bad:
        raise ValidationError("icon_paths may only contain SVG path data.")


def _validate_colors(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(c, str) and _HEX.match(c) for c in value):
        raise ValidationError("colors must be a list of #RRGGBB values.")


class CreativeOption(models.Model):
    """One choice on one Studio control (scene, light, camera angle, …).

    Global, not per-workspace: the catalog is the product's own, edited by an
    operator in admin, and every workspace reads the same one. Per-brand
    taste lives in `TasteProfile`; this is the vocabulary a brief is written
    in.

    **A row carries everything a control needs to draw itself and to be
    understood by a model:**

    * `label` / `description` — the text on the tile and its sub-line
    * `icon_paths` — SVG path data, drawn client-side; validated to be nothing
      but path data
    * `colors` — the swatch or backdrop, as hex
    * `prompt_fragment` — what the generation prompt says when this is chosen
    * `metadata` — per-kind extras, documented where each is read: a light's
      colour `grade`, a format's `aspect`, a preset's `values`, a toggle's
      default

    Selections are stored on `Generation.creative` by **key**, so editing a
    label never rewrites history.
    """

    kind = models.CharField(max_length=16, choices=CreativeKind.choices)
    key = models.SlugField(max_length=40)
    label = models.CharField(max_length=80)
    description = models.CharField(max_length=160, blank=True)
    icon_paths = models.JSONField(default=list, blank=True, validators=[_validate_icon_paths])
    colors = models.JSONField(default=list, blank=True, validators=[_validate_colors])
    prompt_fragment = models.CharField(max_length=300, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["kind", "sort_order", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["kind", "key"], name="unique_creative_option_key")
        ]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["kind", "is_active"])]

    def __str__(self) -> str:
        return f"{self.kind}/{self.key}"
