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
from billing.services import ledger, pricing
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

# The Checkout metadata key that carries the pack from `start_purchase` to
# `fulfil_purchase`. Shared with the webhook rather than spelled twice: the two
# ends of this contract are written three files apart.
PACK_CODE_KEY = "pack_code"


def start_purchase(
    workspace: Workspace, *, pack_code: str, success_url: str, cancel_url: str
) -> CheckoutSession:
    pack = Pack.objects.filter(code=pack_code, is_public=True).first()
    if pack is None:
        raise OCCSError("That pack is not available.", code="invalid_pack")

    if pack.kind == PackKind.VIDEO:
        # Video is the one pack with an entitlement in front of it: Free is 0
        # videos with no overage path (§4.3), so selling it a video pack would
        # sell something it cannot spend.
        entitlements_for(workspace).require_feature("video_generation")

    # Resolved by the organization's billing currency (per-country pricing),
    # falling back to the pack's default row and then to its legacy column.
    resolved = pricing.pack_price(pack, organization=workspace.organization)
    if not resolved.stripe_price_id:
        # A pack with no Stripe price is a configuration error, not a user error.
        logger.error(
            "pack has no Stripe price id",
            extra={"pack": pack.code, "currency": resolved.currency.code},
        )
        raise OCCSError("This pack is not available for purchase yet.", code="pack_not_purchasable")
    if resolved.is_fallback:
        logger.warning(
            "charging a fallback currency",
            extra={"pack": pack.code, "currency": resolved.currency.code},
        )

    session = get_billing_gateway().create_checkout_session(
        mode="payment",
        workspace_id=workspace.pk,
        customer_id=workspace.organization.stripe_customer_id or None,
        customer_email=workspace.organization.owner.email,
        price_id=resolved.stripe_price_id,
        metadata={PACK_CODE_KEY: pack.code},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    logger.info(
        "pack checkout session created",
        extra={
            "workspace_id": workspace.pk,
            "pack": pack.code,
            "currency": resolved.currency.code,
        },
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
        # Raised, not swallowed: `webhooks.process_event` records it on the
        # StripeEvent row, and someone has paid for something we cannot name.
        raise OCCSError(f"Paid pack '{pack_code}' does not exist.", code="unknown_pack")

    note = f"{pack.display_name} pack"
    if pack.kind == PackKind.VIDEO:
        # The unit cost recorded on the ledger is the one actually charged, in
        # the currency actually charged — reconstructing it from `pack` later
        # would quote a dollar figure against a euro payment.
        paid = pricing.pack_price(pack, organization=workspace.organization)
        ledger.purchase_video_units(
            workspace,
            pack.units,
            unit_cost_cents=paid.amount_minor // pack.units,
            note=f"{note} ({paid.currency.code})",
        )
    else:
        ledger.purchase_credits(workspace, pack.units, note=note)

    logger.info(
        "pack fulfilled",
        extra={"workspace_id": workspace.pk, "pack": pack.code, "units": pack.units},
    )
