"""Creating and activating taste profiles (C-08, P5-01).

**Versioned, never edited in place.** A profile changed under a running
workspace would make every earlier decision unattributable — "did acceptance
improve after we changed the voice" is the question the whole loop exists to
answer, and an edited row destroys it.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Max

from accounts.models import User
from taste.models import ProductTasteOverride, TasteProfile
from taste.services.screening import validate_constraints
from workspaces.models import Workspace

#: The eight things a complete profile says. Scored rather than required: a
#: blank profile at first run is the single largest predictor of early churn
#: (the same reasoning behind `Product.is_generation_ready`), so the product
#: needs to *show* how far along a workspace is rather than refuse to work.
COMPLETENESS_FIELDS: tuple[tuple[str, str], ...] = (
    ("voice", "tone"),
    ("voice", "formality"),
    ("voice", "emoji_policy"),
    ("structural", "hashtag_count"),
    ("structural", "cta_style"),
    ("topic_posture", "favour"),
    ("topic_posture", "avoid"),
    ("hard_constraints", ""),
)


@transaction.atomic
def create_profile(
    *, workspace: Workspace, created_by: User | None = None, **fields: Any
) -> TasteProfile:
    """The next version for this workspace. Never activated by creation —
    `activate` is a separate, deliberate act."""
    if "hard_constraints" in fields:
        validate_constraints(fields["hard_constraints"])

    # Locked so two concurrent edits cannot mint the same version, the same
    # way `revisions.record` serialises a post's sequence number.
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    highest = TasteProfile.objects.filter(workspace=workspace).aggregate(Max("version"))
    return TasteProfile.objects.create(
        workspace=workspace,
        version=(highest["version__max"] or 0) + 1,
        created_by=created_by,
        **fields,
    )


@transaction.atomic
def activate(profile: TasteProfile) -> TasteProfile:
    """Makes this the profile new candidates are produced under.

    Deactivates the previous one in the same transaction: the partial unique
    index means a moment with two active profiles is not merely wrong but
    impossible, and doing it in two statements would hit it.
    """
    TasteProfile.objects.filter(workspace_id=profile.workspace_id, is_active=True).exclude(
        pk=profile.pk
    ).update(is_active=False)
    profile.is_active = True
    profile.save(update_fields=["is_active"])
    return profile


def active_profile(workspace: Workspace) -> TasteProfile | None:
    """The profile candidates are judged against, or `None`.

    `None` rather than creating one: a workspace with no taste has not
    described itself yet, and inventing a blank profile would let generation
    proceed against nothing while looking configured.
    """
    return TasteProfile.objects.filter(workspace=workspace, is_active=True).first()


def completeness(profile: TasteProfile) -> float:
    """How much of the profile the workspace has actually filled in (P5-04).

    A score rather than a gate, alongside the product-completeness one that
    already exists. `hard_constraints` counts as filled when it holds anything
    at all — a brand with one rule has stated a policy; the number of rules is
    not the measure.
    """
    filled = 0
    for group, key in COMPLETENESS_FIELDS:
        value = getattr(profile, group, None) or {}
        filled += bool(value.get(key)) if key else bool(value)
    return filled / len(COMPLETENESS_FIELDS)


def constraints_for(workspace: Workspace, product: Any = None) -> dict[str, Any]:
    """The policy a candidate is screened against.

    The workspace's constraints, **narrowed** by the product's own — narrowed
    and never widened: a product override that could relax a brand rule would
    make the brand's policy advisory, which is exactly the split C-08 undoes.
    List constraints union; numeric ones take the stricter; booleans OR.
    """
    profile = active_profile(workspace)
    merged: dict[str, Any] = dict(profile.hard_constraints) if profile else {}
    if product is None:
        return merged

    override = ProductTasteOverride.objects.filter(product=product).first()
    for kind, value in (override.constraints if override else {}).items():
        if kind not in merged:
            merged[kind] = value
        elif isinstance(value, list):
            merged[kind] = sorted({*merged[kind], *value})
        elif isinstance(value, bool):
            merged[kind] = merged[kind] or value
        else:
            merged[kind] = min(merged[kind], value)
    return merged
