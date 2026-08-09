"""Product authoring (implementation.md §4.1: business logic in services/,
never in serializers or views).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.core.files.uploadedfile import UploadedFile

from billing.services.entitlements import entitlements_for
from categories.models import Category
from content.models import MediaAsset
from content.services.media import ingest_media
from products.models import Product
from products.services.completeness import recompute_completeness
from workspaces.models import Workspace


def create_product(
    *,
    workspace: Workspace,
    name: str,
    description: str = "",
    preferences: dict[str, Any] | None = None,
    restrictions: list[str] | None = None,
    category: Category | None = None,
    formats: list[str] | None = None,
    platforms: list[str] | None = None,
    voice: str = "",
    moods: list[str] | None = None,
    hashtags_style: str = "",
    emoji_style: str = "",
    ctas: list[str] | None = None,
) -> Product:
    # Preflight against the plan cap (I8: the limit itself lives on `Plan`,
    # never a literal here). The authoritative check is this same call — there
    # is no separate task/serializer path to keep in sync because product
    # creation is synchronous end to end.
    entitlements_for(workspace).check_quota(
        "max_products", current=Product.objects.filter(workspace=workspace).count()
    )
    product = Product.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        preferences=preferences or {},
        restrictions=restrictions or [],
        category=category,
        formats=formats or [],
        platforms=platforms or [],
        voice=voice,
        moods=moods or [],
        hashtags_style=hashtags_style,
        emoji_style=emoji_style,
        ctas=ctas or [],
    )
    return recompute_completeness(product)


def update_product(product: Product, **fields: Any) -> Product:
    """`fields` is exactly what the caller wants to change, mirroring
    `content.services.posts.update_post` — a PATCH that omits a key must not
    touch it."""
    for name, value in fields.items():
        setattr(product, name, value)
    if fields:
        product.save(update_fields=[*fields.keys(), "updated_at"])
    return recompute_completeness(product)


def attach_reference_images(
    *, product: Product, uploads: Sequence[UploadedFile[Any]]
) -> list[MediaAsset]:
    """Ingests each upload as a new `MediaAsset` and attaches it in one call —
    the composer's "Add image" is a single action, not upload-then-attach."""
    assets = [ingest_media(workspace=product.workspace, upload=upload) for upload in uploads]
    product.reference_images.add(*assets)
    recompute_completeness(product)
    return assets


def detach_reference_image(*, product: Product, media_asset: MediaAsset) -> None:
    """No public endpoint calls this yet (design.md §7 lists no DELETE for
    `reference-images`) — it exists so `is_generation_ready`'s "flips on
    first *and last*" behaviour is provable without one."""
    product.reference_images.remove(media_asset)
    recompute_completeness(product)
