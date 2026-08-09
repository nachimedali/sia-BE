"""The I7 gate: "Products cannot generate without >=1 reference image."

`POST /ai/generate/` does not exist until Phase 7 — this module is what that
endpoint (and the autopilot engine, Phase 12) will call, built now the same
way Phase 4 built nullable FK columns for models that did not exist yet
(design.md A48). `test_generation_rejected_without_reference_image` (I7)
exercises this function directly rather than the future URL (design.md
§15.6 A54).

A plain 400, not 402: this is not an entitlement a plan upgrade would fix —
the workspace is on any plan already able to afford generation, it just has
not finished setting the product up. A 402 would send the frontend to the
billing upsell instead of back to the product page.
"""

from __future__ import annotations

from common.exceptions import OCCSError
from products.models import Product


class ProductNotGenerationReadyError(OCCSError):
    default_code = "product_not_generation_ready"
    default_detail = "This product needs at least one reference image before it can generate."


def ensure_generation_ready(product: Product) -> None:
    if not product.is_generation_ready:
        raise ProductNotGenerationReadyError(
            detail={"product": product.pk},
        )
