"""Product authoring (implementation.md §4.1: business logic in services/,
never in serializers or views).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from billing.services.entitlements import entitlements_for
from content.models import MediaAsset
from content.services.media import ingest_media
from products.models import Product
from products.services.completeness import recompute_completeness
from workspaces.models import Workspace


def _brief_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """`rules_reviewed` is a checkbox on the form, a timestamp in the database:
    ticking it records *when* the owner read the rules, which is the part that
    means something later."""
    out = dict(fields)
    if "rules_reviewed" in out:
        out["rules_reviewed_at"] = timezone.now() if out.pop("rules_reviewed") else None
    return out


def create_product(*, workspace: Workspace, name: str, **fields: Any) -> Product:
    """`fields` is any subset of `Product`'s editable columns (the basics and the
    brief). Unset ones take the model's defaults, so a caller that sends only a
    name — autopilot fixtures, the eval harness — gets exactly what it did
    before the brief existed."""
    # Preflight against the plan cap (I8: the limit itself lives on `Plan`,
    # never a literal here). The authoritative check is this same call — there
    # is no separate task/serializer path to keep in sync because product
    # creation is synchronous end to end.
    entitlements_for(workspace).check_quota(
        "max_products", current=Product.objects.filter(workspace=workspace).count()
    )
    product = Product.objects.create(workspace=workspace, name=name, **_brief_fields(fields))
    return recompute_completeness(product)


def update_product(product: Product, **fields: Any) -> Product:
    """`fields` is exactly what the caller wants to change, mirroring
    `content.services.posts.update_post` — a PATCH that omits a key must not
    touch it."""
    fields = _brief_fields(fields)
    for name, value in fields.items():
        setattr(product, name, value)
    if fields:
        product.save(update_fields=[*fields.keys(), "updated_at"])
    return recompute_completeness(product)


def attach_reference_images(
    *,
    product: Product,
    uploads: Sequence[UploadedFile[Any]],
    shot_tags: Sequence[str] = (),
) -> list[MediaAsset]:
    """Ingests each upload as a new `MediaAsset` and attaches it in one call —
    the composer's "Add image" is a single action, not upload-then-attach.

    `shot_tags[i]` names what upload `i` shows (front, detail, packaging…). Tags
    live on the product rather than the asset because `MediaAsset` is immutable
    and a shot type is a property of *this use* of the file."""
    assets = [ingest_media(workspace=product.workspace, upload=upload) for upload in uploads]
    product.reference_images.add(*assets)
    if shot_tags:
        tags = dict(product.reference_tags)
        tags.update(
            {str(asset.pk): tag for asset, tag in zip(assets, shot_tags, strict=False) if tag}
        )
        product.reference_tags = tags
        product.save(update_fields=["reference_tags", "updated_at"])
    recompute_completeness(product)
    return assets


def detach_reference_image(*, product: Product, media_asset: MediaAsset) -> None:
    """No public endpoint calls this yet (design.md §7 lists no DELETE for
    `reference-images`) — it exists so `is_generation_ready`'s "flips on
    first *and last*" behaviour is provable without one."""
    product.reference_images.remove(media_asset)
    if str(media_asset.pk) in product.reference_tags:
        product.reference_tags = {
            k: v for k, v in product.reference_tags.items() if k != str(media_asset.pk)
        }
        product.save(update_fields=["reference_tags", "updated_at"])
    recompute_completeness(product)
