"""The product identity card and clean photos (S17-29, `studio_content_audit.md` §4.3).

```
draft         the front photo → a card read by vision, paid on pass, unconfirmed
save          the person's card, confirmed or not (only confirmed cards are sent)
lines         a confirmed card as the words the image model reads
clean         one photo → a new picture of the product alone, from that photo only
clean_result  a finished clean is added once as a `front` photo; the original stays
```

The model drifts on labels; the photos and these words together hold them
better than either alone (goose-skills `make-static-ads/prompts.md` §1).
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from ai.models import Generation, GenerationKind, GenerationMode, GenerationStatus
from common.exceptions import OCCSError, ProviderError
from products.models import Product
from products.services.completeness import recompute_completeness

TEXT_MAX = {"shape": 160, "colours": 160, "keep": 200}
LABEL_LINES = 6
LABEL_MAX = 60
#: The mode a clean is made in: an image of this product, held to its photos.
CLEAN_MODE = GenerationMode.PRODUCT
CLEAN_TAG = "front"

IDENTITY_SYSTEM = (
    "You describe a product's identity from its photo, for an image model that must "
    "reproduce it exactly. Say only what is visible; never guess a word you cannot read.\n"
    "Reply with exactly four lines and nothing else:\n"
    "SHAPE: the form, proportions, material and closure, in one phrase\n"
    "COLOURS: the product's and label's colours, in one phrase\n"
    "LABEL: every word printed on the product that you can read, each in quotes, "
    'separated by commas ("NAME", "500 ml"), or NONE\n'
    "KEEP: the one or two details that make it recognisable and must never change"
)
_LINE = re.compile(r"^\s*(SHAPE|COLOURS|LABEL|KEEP)\s*:\s*(.+?)\s*$", re.I | re.M)
_QUOTED = re.compile(r'"([^"]+)"')


class IdentityUnreadableError(OCCSError):
    status_code = 422
    default_code = "identity_unreadable"
    default_detail = "The photo could not be read into a card. No credits were taken."


def draft_credits(product: Product) -> int:
    from integrations.generation import get_generation

    return get_generation(product.workspace.organization).resolve_cost(
        kind=GenerationKind.TEXT, mode=GenerationMode.CAPTION
    )


def _parse(body: str) -> dict[str, Any] | None:
    found = {key.lower(): text for key, text in _LINE.findall(body)}
    if not all(found.get(key) for key in ("shape", "colours", "label", "keep")):
        return None
    label = [] if found["label"].strip().upper() == "NONE" else _QUOTED.findall(found["label"])
    return clean_card(
        {
            "shape": found["shape"][: TEXT_MAX["shape"]],
            "colours": found["colours"][: TEXT_MAX["colours"]],
            "label": [word[:LABEL_MAX] for word in label[:LABEL_LINES]],
            "keep": found["keep"][: TEXT_MAX["keep"]],
        }
    )


def clean_card(raw: Any) -> dict[str, Any]:
    """The card's four fields, bounded; anything else is a 400."""
    if not isinstance(raw, dict):
        raise ValidationError({"identity": "Expected shape, colours, label and keep."})
    out: dict[str, Any] = {}
    for field, limit in TEXT_MAX.items():
        value = raw.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValidationError({field: f"Text of at most {limit} characters."})
        out[field] = value.strip()
    label = raw.get("label", [])
    if (
        not isinstance(label, list)
        or len(label) > LABEL_LINES
        or any(not isinstance(word, str) or len(word) > LABEL_MAX for word in label)
    ):
        raise ValidationError(
            {"label": f"At most {LABEL_LINES} lines of at most {LABEL_MAX} characters."}
        )
    out["label"] = [word.strip() for word in label if word.strip()]
    return out


def draft(product: Product) -> Product:
    """Read the front photo into an unconfirmed card. Paid in credits when the
    reply is a card; a reply that is not, or a model that is down, costs
    nothing and says so (422)."""
    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider
    from billing.services import ledger
    from billing.services.entitlements import entitlements_for
    from products.brief import identity_photos

    photos = identity_photos(product)
    if not photos:
        raise ValidationError({"identity": "Add a photo of the product first."})
    price = draft_credits(product)
    entitlements = entitlements_for(product.workspace)
    entitlements.require_credits(price)
    with photos[0].file.open("rb") as handle:
        image = handle.read()
    try:
        result = get_text_provider(AITask.CAPTION_FROM_IMAGE, workspace=product.workspace).caption(
            system=IDENTITY_SYSTEM,
            prompt=f"Product: {product.name}",
            image_bytes=image,
            n=1,
        )
    except ProviderError as error:
        raise IdentityUnreadableError from error
    card = _parse(result.variants[0].body if result.variants else "")
    if card is None:
        raise IdentityUnreadableError
    with transaction.atomic():
        ledger.debit_credits(
            product.workspace,
            price,
            quota=entitlements.quota("monthly_ai_credits"),
            note=f"identity card for product {product.pk}",
        )
        product.identity = card
        product.identity_confirmed_at = None
        product.save(update_fields=["identity", "identity_confirmed_at", "updated_at"])
    return product


def save(product: Product, raw: Any, *, confirmed: bool) -> Product:
    product.identity = clean_card(raw)
    product.identity_confirmed_at = timezone.now() if confirmed else None
    product.save(update_fields=["identity", "identity_confirmed_at", "updated_at"])
    return product


def lines(product: Product | None) -> list[str]:
    """A confirmed card as the image model reads it; nothing for a draft."""
    if product is None or product.identity_confirmed_at is None or not product.identity:
        return []
    card = product.identity
    words = ", ".join(f'"{word}"' for word in card.get("label") or [])
    parts = [
        f"shape: {card['shape']}" if card.get("shape") else "",
        f"colours: {card['colours']}" if card.get("colours") else "",
        f"the label reads exactly {words}" if words else "",
        f"never change: {card['keep']}" if card.get("keep") else "",
    ]
    described = "; ".join(part for part in parts if part)
    return [f"The product, as it must appear — {described}."] if described else []


# -----------------------------------------------------------------------------
# Clean photos
# -----------------------------------------------------------------------------
CLEAN_PROMPT = (
    "The product in the attached photo, alone: front view, centred, label facing the camera, "
    "on a plain light seamless background with a soft contact shadow. Remove every other "
    "object, hand, prop, product and text that is not printed on the product itself. Keep the "
    "product's shape, label, colours and proportions exactly as photographed."
)


def clean(product: Product, asset_id: int, *, user: Any) -> Generation:
    """A new picture of the product alone, made from this one photo. A photo
    that is not this product's is a 404."""
    from ai.services.images import queue_image

    asset = product.reference_images.filter(pk=asset_id).first()
    if asset is None:
        raise NotFound("That photo is not one of this product's.")
    return queue_image(
        workspace=product.workspace,
        user=user,
        mode=CLEAN_MODE,
        prompt=CLEAN_PROMPT,
        aspect="1:1",
        product=product,
        source_media=asset,
    )


def clean_result(product: Product, generation_id: int) -> str:
    """`pending`, `failed`, or `ready` — and on the first `ready`, the picture is
    added to the product as a `front` photo. Idempotent: read twice, added once."""
    generation = Generation.objects.filter(
        workspace=product.workspace,
        product=product,
        pk=generation_id,
        mode=CLEAN_MODE,
        source_media__isnull=False,
    ).first()
    if generation is None:
        raise NotFound
    if generation.status == GenerationStatus.PENDING:
        return "pending"
    variant = (
        generation.variants.filter(media_asset__isnull=False).order_by("id").first()
        if generation.status == GenerationStatus.SUCCEEDED
        else None
    )
    if variant is None:
        return "failed"
    media = variant.media_asset
    assert media is not None  # filtered on media_asset__isnull=False
    with transaction.atomic():
        locked = Product.objects.select_for_update().get(pk=product.pk)
        if not locked.reference_images.filter(pk=media.pk).exists():
            locked.reference_images.add(media)
            locked.reference_tags = {**(locked.reference_tags or {}), str(media.pk): CLEAN_TAG}
            locked.save(update_fields=["reference_tags", "updated_at"])
            recompute_completeness(locked)
    return "ready"
