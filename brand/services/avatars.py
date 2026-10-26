"""Brand avatars (S17): the people a brand puts in its images and videos.

```
draft          traits → 4 portraits (task on ai_q), each look-alike checked,
               paid in credits on success, counted per organization per month
create         a portrait from a ready draft → an invented avatar (cap checked)
               or 3 to 5 photos + recorded consent → a real one (admin, Scale)
use            `resolve_for_render`: 404 elsewhere · 409 consent · 402 locked
house · revoke · archive   the only changes an avatar ever takes
```

**Limits are counted, never cached** (Part 7 rule 5). The cap is per brand
(`max_avatars`); a downgrade locks the newest above it and deletes nothing —
the oldest stay usable, the same rule social accounts follow. Creation takes
the workspace row lock so twenty racing requests against a cap of three make
three; a portrait batch takes the organization row lock, like the trial quota.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from ai.models import AITask, CreativeKind, CreativeOption, GenerationKind, GenerationMode
from billing.services import ledger
from billing.services.entitlements import Entitlements, entitlements_for
from billing.services.plans import suggest_upgrade
from brand.models import Avatar, AvatarDraft, AvatarKind, DraftStatus, Likeness
from common.exceptions import (
    FeatureNotAvailable,
    InsufficientCredits,
    OCCSError,
    ProviderError,
    StateConflict,
)
from content.models import MediaAsset, MediaKind, MediaSource
from workspaces.models import Organization, Workspace
from workspaces.services.approvals import log

logger = logging.getLogger(__name__)

PORTRAITS_PER_DRAFT = 4
ANGLES = ("three-quarter left", "three-quarter right", "profile")
REAL_PHOTOS = (3, 5)
LANGUAGES = ("en", "fr", "it")
NAME_MAX = 80
NOTE_MAX = 140
GROUPS = ("archetype", "age", "look", "style", "energy")


class AvatarLimitError(FeatureNotAvailable):
    default_code = "avatar_limit_reached"
    default_detail = "This brand has all the avatars its plan allows."


class AvatarOverLimitError(FeatureNotAvailable):
    default_code = "avatar_over_limit"
    default_detail = "This avatar is locked: the plan allows fewer avatars than the brand has."


class AvatarDraftsExhaustedError(FeatureNotAvailable):
    default_code = "avatar_drafts_exhausted"
    default_detail = "No portrait batches left this month."


class ConsentRequiredError(StateConflict):
    default_code = "avatar_consent_required"
    default_detail = "A real person needs recorded consent, and it must still stand."


class LikenessMatchError(OCCSError):
    status_code = 422
    default_code = "likeness_match"
    default_detail = "This face is too close to a well-known person. Pick another portrait."


class AvatarRefusedError(OCCSError):
    """400 with its own code: `consent_signer`, `language`."""

    default_code = "avatar_refused"
    default_detail = "This avatar cannot be used this way."


class DraftNotReadyError(StateConflict):
    default_code = "avatar_draft_not_ready"
    default_detail = "These portraits are not ready yet."


# -----------------------------------------------------------------------------
# Counting
# -----------------------------------------------------------------------------
def cap(workspace: Workspace, entitlements: Entitlements | None = None) -> int:
    return (entitlements or entitlements_for(workspace)).int_feature("max_avatars")


def active(workspace: Workspace) -> QuerySet[Avatar]:
    """Avatars that still occupy a place: not archived, consent not withdrawn."""
    return Avatar.objects.filter(
        workspace=workspace, archived_at__isnull=True, consent_revoked_at__isnull=True
    ).order_by("created_at", "id")


def usable_ids(workspace: Workspace, limit: int | None = None) -> set[int]:
    """The oldest `cap` active avatars. A query, every time."""
    limit = cap(workspace) if limit is None else limit
    return set(active(workspace).values_list("pk", flat=True)[: max(limit, 0)])


def state_of(avatar: Avatar, usable: set[int], *, real_allowed: bool) -> str:
    """`usable`, or the reason it is not — what the library and the drawer show."""
    if avatar.archived_at is not None:
        return "archived"
    if avatar.kind == AvatarKind.REAL:
        if avatar.consent_revoked_at is not None:
            return "consent_revoked"
        if not (avatar.consent_signed_by and avatar.consent_signed_on):
            return "consent_required"
        if not real_allowed:
            return "plan"
    if avatar.likeness == Likeness.MATCH:
        return "likeness"
    if avatar.pk not in usable:
        return "over_limit"
    return "usable"


def drafts_used(organization: Organization, now: dt.datetime | None = None) -> int:
    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        AvatarDraft.objects.filter(workspace__organization=organization, created_at__gte=start)
        .exclude(status=DraftStatus.FAILED)
        .count()
    )


def limits(workspace: Workspace, entitlements: Entitlements | None = None) -> dict[str, Any]:
    entitlements = entitlements or entitlements_for(workspace)
    plan = entitlements.plan
    monthly = entitlements.int_feature("avatar_drafts_monthly")
    return {
        "max_avatars": entitlements.int_feature("max_avatars"),
        "used": active(workspace).count(),
        "drafts_monthly": monthly,
        "drafts_left": max(0, monthly - drafts_used(workspace.organization)),
        "real_avatars": bool(entitlements.feature("real_avatars")),
        "suggested_plan": suggest_upgrade(plan, "max_avatars"),
        "real_suggested_plan": suggest_upgrade(plan, "real_avatars"),
    }


# -----------------------------------------------------------------------------
# What the model is told
# -----------------------------------------------------------------------------
def _traits() -> dict[str, CreativeOption]:
    return {
        row.key: row
        for row in CreativeOption.objects.filter(kind=CreativeKind.AVATAR_TRAIT, is_active=True)
    }


def clean_traits(raw: Any) -> dict[str, str]:
    """Keys by group, each an active row of that group; an archetype fills the
    groups it sets that the person left blank. Unknown is a 400, never ignored."""
    if not isinstance(raw, dict):
        raise ValidationError({"traits": "Expected an object."})
    rows = _traits()
    out: dict[str, str] = {}
    errors: dict[str, str] = {}
    for group in GROUPS:
        key = raw.get(group)
        if key in (None, ""):
            continue
        row = rows.get(str(key))
        if row is None or (row.metadata or {}).get("group") != group:
            errors[group] = f"'{key}' is not an available {group}."
            continue
        out[group] = row.key
    if set(raw) - set(GROUPS) - {"note"}:
        errors["traits"] = "Unknown trait group."
    note = raw.get("note", "")
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        errors["note"] = f"Text of at most {NOTE_MAX} characters."
    if errors:
        raise ValidationError(errors)
    archetype = rows.get(out.get("archetype", ""))
    for group, key in ((archetype.metadata or {}).get("values") or {}).items() if archetype else ():
        out.setdefault(group, key)
    if note.strip():
        out["note"] = note.strip()
    if not any(group in out for group in GROUPS):
        raise ValidationError({"traits": "Describe the person: pick at least one trait."})
    return out


def describe(avatar: Avatar | None = None, *, traits: dict[str, str] | None = None) -> str:
    """One sentence for the image model. A real person is described only by
    their photos — words would invent features they do not have."""
    if avatar is not None and avatar.kind == AvatarKind.REAL:
        return "The person shown in the reference photo"
    traits = traits if traits is not None else (avatar.traits if avatar else {})
    rows = _traits()
    parts = [rows[traits[group]].prompt_fragment for group in GROUPS if traits.get(group) in rows]
    sentence = "An invented person: " + ", ".join(part for part in parts if part)
    if traits.get("note"):
        sentence += f"; {traits['note']}"
    return sentence


def _portrait_prompt(traits: dict[str, str]) -> str:
    return (
        f"Head-and-shoulders portrait photo, square, plain light background, soft daylight. "
        f"{describe(traits=traits)}. A believable everyday person who does not resemble any "
        "real or famous person. Natural skin, no retouching, no text, no logo, no watermark."
    )


# -----------------------------------------------------------------------------
# Drafts
# -----------------------------------------------------------------------------
def draft_price(workspace: Workspace) -> int:
    """The `(IMAGE, AVATAR)` cost row, through the generation seam (M0)."""
    from integrations.generation import get_generation

    return get_generation(workspace.organization).resolve_cost(
        kind=GenerationKind.IMAGE, mode=GenerationMode.AVATAR
    )


def start_draft(workspace: Workspace, *, user: Any, raw_traits: Any) -> AvatarDraft:
    traits = clean_traits(raw_traits)
    entitlements = entitlements_for(workspace)
    monthly = entitlements.int_feature("avatar_drafts_monthly")
    if monthly <= 0 or cap(workspace, entitlements) <= 0:
        raise FeatureNotAvailable(
            "Avatars are not in your plan.",
            detail={"feature": "max_avatars", "plan": entitlements.plan.code},
            suggested_plan=suggest_upgrade(entitlements.plan, "max_avatars"),
        )
    price = draft_price(workspace)
    with transaction.atomic():
        Organization.objects.select_for_update().filter(pk=workspace.organization_id).exists()
        used = drafts_used(workspace.organization)
        if used >= monthly:
            raise AvatarDraftsExhaustedError(
                f"All {monthly} portrait batches are used this month.",
                detail={"feature": "avatar_drafts_monthly", "limit": monthly, "used": used},
                suggested_plan=suggest_upgrade(entitlements.plan, "avatar_drafts_monthly"),
            )
        entitlements.require_credits(price)
        draft = AvatarDraft.objects.create(
            workspace=workspace,
            created_by=user,
            traits=traits,
            prompt=_portrait_prompt(traits),
            credits=price,
        )
    from brand.tasks import run_avatar_draft

    run_avatar_draft.delay(draft.pk)
    draft.refresh_from_db()
    return draft


def _ingest(workspace: Workspace, content: bytes, mime: str, name: str) -> MediaAsset:
    from content.services.media import ingest_media

    upload = SimpleUploadedFile(name, content, content_type=mime)
    return ingest_media(workspace=workspace, upload=upload, source=MediaSource.GENERATED)


def _likeness(content: bytes) -> tuple[str, str]:
    from ai.providers.likeness import get_likeness_checker

    checker = get_likeness_checker()
    if checker is None:
        return Likeness.UNCHECKED, ""
    result = checker.check(content)
    return (Likeness.MATCH if result.match else Likeness.CLEAR), result.provider


def run_draft(draft_id: int) -> AvatarDraft:
    """Idempotent: a draft that is not QUEUED is returned as it is."""
    from ai.providers.nanobanana_image import get_image_provider

    with transaction.atomic():
        draft = AvatarDraft.objects.select_for_update().get(pk=draft_id)
        if draft.status != DraftStatus.QUEUED:
            return draft
        draft.status = DraftStatus.RUNNING
        draft.save(update_fields=["status"])

    def fail(message: str) -> AvatarDraft:
        draft.status = DraftStatus.FAILED
        draft.error = message
        draft.finished_at = timezone.now()
        draft.save(update_fields=["status", "error", "finished_at"])
        return draft

    try:
        result = get_image_provider(AITask.AVATAR_PORTRAIT, workspace=draft.workspace).generate(
            prompt=draft.prompt,
            reference_images=[],
            aspect="1:1",
            n=PORTRAITS_PER_DRAFT,
            batch=False,
        )
        portraits = []
        for index, variant in enumerate(result.variants[:PORTRAITS_PER_DRAFT]):
            likeness, provider = _likeness(variant.content)
            asset = _ingest(
                draft.workspace, variant.content, variant.mime, f"avatar-{draft.pk}-{index}.png"
            )
            portraits.append({"media": asset.pk, "likeness": likeness, "provider": provider})
    except ProviderError:
        logger.warning("avatar draft %s: provider error", draft.pk, exc_info=True)
        return fail("The portraits could not be drawn. No credits were taken.")
    if not portraits:
        return fail("No portrait came back. No credits were taken.")

    entitlements = entitlements_for(draft.workspace)
    try:
        with transaction.atomic():
            entry = ledger.debit_credits(
                draft.workspace,
                draft.credits,
                quota=entitlements.quota("monthly_ai_credits"),
                note=f"avatar portraits {draft.pk}",
            )
            draft.portraits = portraits
            draft.charge = entry
            draft.status = DraftStatus.READY
            draft.finished_at = timezone.now()
            draft.save(update_fields=["portraits", "charge", "status", "finished_at"])
    except InsufficientCredits:
        return fail("There were not enough credits left. Nothing was taken.")
    return draft


# -----------------------------------------------------------------------------
# Creating
# -----------------------------------------------------------------------------
def _clean_languages(raw: Any) -> list[str]:
    if raw in (None, []):
        return ["en"]
    if not isinstance(raw, list) or not raw or set(raw) - set(LANGUAGES):
        raise ValidationError({"languages": f"A list of {', '.join(LANGUAGES)} (L-6)."})
    return list(dict.fromkeys(raw))


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ValidationError({"name": f"A name of 1 to {NAME_MAX} characters."})
    return name


def _take_place(workspace: Workspace) -> None:
    """Under the workspace row lock: refuse a new avatar past the cap."""
    Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
    limit = cap(workspace)
    used = active(workspace).count()
    if used >= limit:
        entitlements = entitlements_for(workspace)
        raise AvatarLimitError(
            f"This brand has {used} of {limit} avatars."
            if limit
            else "Avatars are not in your plan.",
            detail={"feature": "max_avatars", "limit": limit, "used": used},
            suggested_plan=suggest_upgrade(entitlements.plan, "max_avatars"),
        )


def create_synthetic(
    workspace: Workspace,
    *,
    user: Any,
    draft_id: int,
    index: int,
    name: Any,
    languages: Any = None,
    is_house: bool = False,
) -> Avatar:
    draft = AvatarDraft.objects.filter(workspace=workspace, pk=draft_id).first()
    if draft is None:
        raise NotFound
    if draft.status != DraftStatus.READY:
        raise DraftNotReadyError(detail={"status": draft.status})
    if not 0 <= index < len(draft.portraits):
        raise ValidationError({"index": "Pick one of the portraits."})
    picked = draft.portraits[index]
    if picked["likeness"] == Likeness.MATCH:
        raise LikenessMatchError
    portrait = MediaAsset.objects.filter(workspace=workspace, pk=picked["media"]).first()
    if portrait is None:
        raise NotFound
    clean_name, langs = _clean_name(name), _clean_languages(languages)
    with transaction.atomic():
        _take_place(workspace)
        avatar = Avatar.objects.create(
            workspace=workspace,
            name=clean_name,
            kind=AvatarKind.SYNTHETIC,
            portrait=portrait,
            traits=draft.traits,
            languages=langs,
            likeness=picked["likeness"],
            likeness_provider=picked.get("provider", ""),
            created_by=user,
        )
        if is_house or not Avatar.objects.filter(workspace=workspace, is_house=True).exists():
            _make_house(avatar)
    from brand.tasks import run_avatar_angles

    run_avatar_angles.delay(avatar.pk)
    avatar.refresh_from_db()
    return avatar


def run_angles(avatar_id: int) -> Avatar:
    """Reference angles for an invented avatar, drawn from its portrait. Written
    once; a failure leaves the portrait alone, which is enough to render."""
    from ai.providers.nanobanana_image import get_image_provider

    avatar = Avatar.objects.select_related("portrait").get(pk=avatar_id)
    if avatar.angles or avatar.kind != AvatarKind.SYNTHETIC:
        return avatar
    with avatar.portrait.file.open("rb") as handle:
        portrait = handle.read()
    provider = get_image_provider(AITask.AVATAR_PORTRAIT, workspace=avatar.workspace)
    angles: list[int] = []
    try:
        for index, angle in enumerate(ANGLES):
            result = provider.generate(
                prompt=(
                    f"The same person as image 1, photographed from a {angle} angle. Same face, "
                    "hair, skin tone, clothes and light. Plain light background. No text."
                ),
                reference_images=[portrait],
                aspect="1:1",
                n=1,
                batch=False,
            )
            if result.variants:
                variant = result.variants[0]
                angles.append(
                    _ingest(
                        avatar.workspace,
                        variant.content,
                        variant.mime,
                        f"avatar-{avatar.pk}-angle-{index}.png",
                    ).pk
                )
    except ProviderError:
        logger.warning("avatar %s: angles failed", avatar.pk, exc_info=True)
    if angles:
        Avatar.objects.filter(pk=avatar.pk, angles=[]).update(angles=angles)
        avatar.refresh_from_db()
    return avatar


def create_real(
    workspace: Workspace,
    *,
    user: Any,
    name: Any,
    photos: Any,
    signed_by: Any,
    signed_on: dt.date | None,
    languages: Any = None,
) -> Avatar:
    """A real person. Admin only (the view), on a plan with `real_avatars`, with
    consent signed by the person in the photos — recorded in `AuditLog`."""
    entitlements = entitlements_for(workspace)
    if not entitlements.feature("real_avatars"):
        raise FeatureNotAvailable(
            "Avatars of real people are not in your plan.",
            detail={"feature": "real_avatars", "plan": entitlements.plan.code},
            suggested_plan=suggest_upgrade(entitlements.plan, "real_avatars"),
        )
    clean_name, langs = _clean_name(name), _clean_languages(languages)
    low, high = REAL_PHOTOS
    if not isinstance(photos, list) or not low <= len(photos) <= high:
        raise ValidationError({"photos": f"Add {low} to {high} photos of the person."})
    assets = {
        asset.pk: asset
        for asset in MediaAsset.objects.filter(
            workspace=workspace, pk__in=photos, kind=MediaKind.IMAGE
        )
    }
    if len(assets) != len(set(photos)) or len(set(photos)) != len(photos):
        raise NotFound("A photo is not one of this brand's pictures.")
    signer = str(signed_by or "").strip()
    if not signer or signed_on is None:
        raise ConsentRequiredError(
            "A real face needs recorded consent, signed with a name and a date."
        )
    if signer.casefold() != clean_name.casefold():
        raise AvatarRefusedError(
            "The consent must be signed by the person in the photos.", code="consent_signer"
        )
    if signed_on > timezone.localdate():
        raise ValidationError({"consent": "The consent cannot be signed in the future."})
    with transaction.atomic():
        _take_place(workspace)
        avatar = Avatar.objects.create(
            workspace=workspace,
            name=clean_name,
            kind=AvatarKind.REAL,
            portrait=assets[photos[0]],
            angles=list(photos[1:]),
            languages=langs,
            likeness=Likeness.UNCHECKED,
            consent_signed_by=signer,
            consent_signed_on=signed_on,
            consent_recorded_by=user,
            consent_recorded_at=timezone.now(),
            created_by=user,
        )
        log(
            workspace=workspace,
            actor=user,
            verb="avatar.consent_recorded",
            target_repr=clean_name,
            meta={"avatar": avatar.pk, "signed_by": signer, "signed_on": signed_on.isoformat()},
        )
    return avatar


# -----------------------------------------------------------------------------
# Using and changing
# -----------------------------------------------------------------------------
def get(workspace: Workspace, avatar_id: Any) -> Avatar:
    avatar = Avatar.objects.filter(workspace=workspace, pk=avatar_id).first()
    if avatar is None:
        raise NotFound
    return avatar


def resolve_for_render(workspace: Workspace, avatar_id: Any) -> Avatar:
    """The avatar, if it may appear in a new render now. Every reason it may
    not is its own answer: 404 not here or archived, 409 consent, 402 plan."""
    avatar = get(workspace, avatar_id)
    entitlements = entitlements_for(workspace)
    limit = cap(workspace, entitlements)
    state = state_of(
        avatar,
        usable_ids(workspace, limit),
        real_allowed=bool(entitlements.feature("real_avatars")),
    )
    if state == "archived":
        raise NotFound
    if state in ("consent_revoked", "consent_required"):
        raise ConsentRequiredError(detail={"avatar": avatar.pk, "state": state})
    if state == "likeness":
        raise LikenessMatchError(detail={"avatar": avatar.pk})
    if state == "plan":
        raise FeatureNotAvailable(
            "Avatars of real people are not in your plan.",
            detail={"feature": "real_avatars", "avatar": avatar.pk},
            suggested_plan=suggest_upgrade(entitlements.plan, "real_avatars"),
        )
    if state == "over_limit":
        raise AvatarOverLimitError(
            detail={"feature": "max_avatars", "avatar": avatar.pk, "limit": limit},
            suggested_plan=suggest_upgrade(entitlements.plan, "max_avatars"),
        )
    return avatar


def require_language(avatar: Avatar, language: str) -> None:
    """A person who speaks on screen must speak the content language."""
    if language and language not in (avatar.languages or []):
        raise AvatarRefusedError(
            f"{avatar.name} doesn't speak {language.upper()}.",
            code="language",
            detail={"languages": avatar.languages},
        )


def _make_house(avatar: Avatar) -> None:
    Avatar.objects.filter(workspace=avatar.workspace, is_house=True).exclude(pk=avatar.pk).update(
        is_house=False
    )
    Avatar.objects.filter(pk=avatar.pk).update(is_house=True)


def make_house(avatar: Avatar) -> Avatar:
    if avatar.archived_at is not None or avatar.consent_revoked_at is not None:
        raise StateConflict("Only an avatar in use can be the house face.")
    with transaction.atomic():
        _make_house(avatar)
    avatar.refresh_from_db()
    return avatar


def revoke(avatar: Avatar, *, actor: Any) -> Avatar:
    """Withdrawn consent: the avatar leaves every future render. Stamped once;
    renders already made stay where they are."""
    if avatar.kind != AvatarKind.REAL:
        raise StateConflict("Only a real person's consent can be withdrawn.")
    if avatar.consent_revoked_at is not None:
        return avatar
    with transaction.atomic():
        Avatar.objects.filter(pk=avatar.pk).update(
            consent_revoked_at=timezone.now(), is_house=False
        )
        log(
            workspace=avatar.workspace,
            actor=actor,
            verb="avatar.consent_revoked",
            target_repr=avatar.name,
            meta={"avatar": avatar.pk},
        )
    avatar.refresh_from_db()
    return avatar


def archive(avatar: Avatar, *, actor: Any) -> Avatar:
    """Removed from the library and from future renders; its media and every
    render made with it are kept."""
    if avatar.archived_at is not None:
        return avatar
    with transaction.atomic():
        Avatar.objects.filter(pk=avatar.pk).update(archived_at=timezone.now(), is_house=False)
        log(
            workspace=avatar.workspace,
            actor=actor,
            verb="avatar.archived",
            target_repr=avatar.name,
            meta={"avatar": avatar.pk},
        )
    avatar.refresh_from_db()
    return avatar
