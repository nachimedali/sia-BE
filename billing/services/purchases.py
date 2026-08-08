"""Prepaid packs (design.md §4.3, D16, `POST /billing/purchase/`).

Two halves, deliberately separated by the webhook:

* `start_purchase` only produces a hosted Stripe Checkout URL. It grants
  nothing — a user who reaches the payment page and closes it has bought
  nothing, and neither has one who tampers with the success URL.
* `fulfil_purchase` runs from the webhook, once Stripe says the money moved.

Exactly-once is `StripeEvent` (A37): the event row is unique on `event.id`, so a
retried delivery never credits a second pack. That is the same mechanism the
renewal grant relies on, rather than a second idempotency scheme with its own
failure modes.
"""

from __future__ import annotations

import logging

from billing.gateways.base import CheckoutSession
from billing.gateways.stripe import get_billing_gateway
from billing.models import Pack, PackKind
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

# Video is the one pack with an entitlement in front of it: Free is 0 videos
# with no overage path (§4.3), so selling it a video pack would sell something
# it cannot spend.
VIDEO_FEATURE = "video_generation"


def start_purchase(
    workspace: Workspace, *, pack_code: str, success_url: str, cancel_url: str
) -> CheckoutSession:
    pack = Pack.objects.filter(code=pack_code, is_public=True).first()
    if pack is None:
        raise OCCSError("That pack is not available.", code="invalid_pack")

    if pack.kind == PackKind.VIDEO:
        entitlements_for(workspace).require_feature(VIDEO_FEATURE)

    if not pack.stripe_price_id:
        # A pack with no Stripe price is a configuration error, not a user error.
        logger.error("pack has no Stripe price id", extra={"pack": pack.code})
        raise OCCSError("This pack is not available for purchase yet.", code="pack_not_purchasable")

    session = get_billing_gateway().create_payment_session(
        workspace_id=workspace.pk,
        customer_id=workspace.stripe_customer_id or None,
        customer_email=workspace.owner.email,
        price_id=pack.stripe_price_id,
        metadata={"pack_code": pack.code},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    logger.info(
        "pack checkout session created",
        extra={"workspace_id": workspace.pk, "pack": pack.code},
    )
    return session


def fulfil_purchase(workspace: Workspace, *, pack_code: str) -> None:
    """Credits a paid pack.

    Deliberately does not re-check the entitlement gate. The money has already
    moved; a workspace that downgraded between checkout and delivery has still
    paid, and refusing here would take payment for nothing.
    """
    pack = Pack.objects.filter(code=pack_code).first()
    if pack is None:
        # Recorded rather than raised: the caller records the error on the
        # StripeEvent row, and someone has paid for something we cannot name.
        raise OCCSError(f"Paid pack '{pack_code}' does not exist.", code="unknown_pack")

    note = f"{pack.display_name} pack"
    if pack.kind == PackKind.VIDEO:
        ledger.purchase_video_units(
            workspace, pack.units, unit_cost_cents=pack.unit_price_cents, note=note
        )
    else:
        ledger.purchase_credits(workspace, pack.units, note=note)

    logger.info(
        "pack fulfilled",
        extra={"workspace_id": workspace.pk, "pack": pack.code, "units": pack.units},
    )
