"""Stripe webhook processing (implementation.md Phase 3.5).

The webhook is the source of truth for subscription state, which makes it the
most security-sensitive unauthenticated endpoint in the service: a forged
delivery would be a free upgrade for anyone who can POST. Two rules follow.

**Verify before parsing.** The signature is checked against the raw body; a
failure never reaches the handlers.

**Record before acting.** Every accepted event is written by `event.id` first.
Stripe retries on any non-2xx and can deliver the same event more than once
regardless, so without this a replayed `invoice.payment_succeeded` would grant a
second month of credits (`test_stripe_webhook_idempotent_on_replay`).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import (
    CreditReason,
    Plan,
    StripeEvent,
    Subscription,
    SubscriptionStatus,
)
from billing.services import ledger, purchases, subscriptions
from billing.services.subscriptions import STRIPE_STATUS
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
    }
)


def process_event(event: dict[str, Any]) -> bool:
    """Handles one verified event. Returns False when it was a replay.

    The event is recorded outside the handler's transaction: if the handler
    fails, the record survives with its error so the failure is visible, and
    Stripe's retry is still refused rather than replaying a partial write.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")

    if not event_id:
        logger.warning("stripe event without an id; ignoring")
        return False

    try:
        with transaction.atomic():
            record = StripeEvent.objects.create(
                event_id=event_id, event_type=event_type, payload=event
            )
    except IntegrityError:
        logger.info("stripe event replayed; ignoring", extra={"event_id": event_id})
        return False

    if event_type not in HANDLED_EVENTS:
        record.processed_at = timezone.now()
        record.error = "unhandled event type"
        record.save(update_fields=["processed_at", "error"])
        return True

    try:
        _dispatch(event_type, event)
    except Exception as exc:
        # Recorded rather than re-raised: the event is stored, so returning 200
        # stops Stripe retrying a delivery that would fail the same way. The log
        # and the row are what an operator replays from.
        logger.exception("stripe webhook handler failed", extra={"event_id": event_id})
        record.error = f"{type(exc).__name__}: {exc}"
        record.processed_at = timezone.now()
        record.save(update_fields=["error", "processed_at"])
        return True

    record.processed_at = timezone.now()
    record.save(update_fields=["processed_at"])
    return True


def _dispatch(event_type: str, event: dict[str, Any]) -> None:
    obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        _on_checkout_completed(obj)
    elif event_type.startswith("customer.subscription."):
        _on_subscription_change(obj, deleted=event_type.endswith("deleted"))
    elif event_type == "invoice.payment_succeeded":
        _on_payment_succeeded(obj)
    elif event_type == "invoice.payment_failed":
        _on_payment_failed(obj)


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------
def _on_checkout_completed(session: dict[str, Any]) -> None:
    """Attaches the Stripe customer, and delivers a pack if this was one.

    For a subscription this handler deliberately does nothing else: the
    subscription arrives on `customer.subscription.created`, which may land
    before or after this — Stripe does not order deliveries.

    A `mode=payment` session is a prepaid pack (§4.3), and this is the only
    place its units are credited. Nothing is granted because a browser reached
    the success URL.
    """
    workspace = _workspace_from(session.get("client_reference_id"))
    if workspace is None:
        return

    customer_id = _id_of(session.get("customer"))
    if customer_id and workspace.stripe_customer_id != customer_id:
        workspace.stripe_customer_id = customer_id
        workspace.save(update_fields=["stripe_customer_id", "updated_at"])

    if session.get("mode") != "payment":
        return

    if session.get("payment_status") != "paid":
        # Delayed payment methods complete the session before the money clears.
        # We only enable cards, so this is a configuration surprise worth seeing.
        logger.warning(
            "checkout completed without payment; nothing credited",
            extra={"session": session.get("id"), "payment_status": session.get("payment_status")},
        )
        return

    pack_code = (session.get("metadata") or {}).get("pack_code")
    if not pack_code:
        logger.error(
            "paid checkout session carries no pack_code", extra={"session": session.get("id")}
        )
        return

    purchases.fulfil_purchase(workspace, pack_code=str(pack_code))


def _on_subscription_change(subscription: dict[str, Any], *, deleted: bool) -> None:
    workspace = _workspace_for_subscription(subscription)
    if workspace is None:
        return

    plan = _plan_from(subscription)
    if plan is None:
        logger.error(
            "no plan matches the Stripe price on this subscription",
            extra={"subscription": subscription.get("id")},
        )
        return

    status = (
        SubscriptionStatus.CANCELED
        if deleted
        else STRIPE_STATUS.get(str(subscription.get("status")), SubscriptionStatus.INCOMPLETE)
    )

    subscriptions.apply_subscription_state(
        workspace,
        plan=plan,
        stripe_subscription_id=str(subscription["id"]),
        status=status,
        period_start=_timestamp(subscription.get("current_period_start")),
        period_end=_timestamp(subscription.get("current_period_end")),
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
    )


def _on_payment_succeeded(invoice: dict[str, Any]) -> None:
    """A renewal: grant the new period's allowances.

    Skips the first invoice of a subscription — `customer.subscription.created`
    has already granted it, and granting again would double the first month.
    """
    subscription_id = _id_of(invoice.get("subscription"))
    if not subscription_id:
        return

    record = (
        Subscription.objects.filter(stripe_subscription_id=subscription_id)
        .select_related("plan", "workspace")
        .first()
    )
    if record is None:
        return

    if str(invoice.get("billing_reason")) == "subscription_create":
        return

    plan = record.plan
    ledger.grant_credits(
        record.workspace,
        plan.monthly_ai_credits,
        reason=CreditReason.MONTHLY_GRANT,
        rollover=bool(plan.feature("credits_rollover")),
        note=f"{plan.code} renewal",
    )
    ledger.grant_video_units(record.workspace, plan.included_videos, note=f"{plan.code} renewal")


def _on_payment_failed(invoice: dict[str, Any]) -> None:
    """Marks the subscription past due. Deliberately does not downgrade —
    Stripe's dunning gets several days to retry the card first, and cutting
    access on the first failed attempt is how you lose a paying customer to a
    reissued card."""
    subscription_id = _id_of(invoice.get("subscription"))
    if not subscription_id:
        return

    Subscription.objects.filter(stripe_subscription_id=subscription_id).update(
        status=SubscriptionStatus.PAST_DUE, updated_at=timezone.now()
    )


# -----------------------------------------------------------------------------
# Payload helpers — Stripe expands objects inconsistently, so every id read
# has to tolerate both a string and a nested object.
# -----------------------------------------------------------------------------
def _id_of(value: Any) -> str | None:
    if isinstance(value, dict):
        return str(value.get("id")) if value.get("id") else None
    return str(value) if value else None


def _timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    return dt.datetime.fromtimestamp(int(value), tz=dt.UTC)


def _workspace_from(reference: Any) -> Workspace | None:
    if not reference:
        return None
    workspace = Workspace.objects.filter(pk=int(reference)).first()
    if workspace is None:
        logger.error("stripe event references an unknown workspace", extra={"ref": reference})
    return workspace


def _workspace_for_subscription(subscription: dict[str, Any]) -> Workspace | None:
    metadata = subscription.get("metadata") or {}
    workspace = _workspace_from(metadata.get("workspace_id"))
    if workspace is not None:
        return workspace

    customer_id = _id_of(subscription.get("customer"))
    if customer_id:
        return Workspace.objects.filter(stripe_customer_id=customer_id).first()
    return None


def _plan_from(subscription: dict[str, Any]) -> Plan | None:
    items = (subscription.get("items") or {}).get("data") or []
    price_ids = {_id_of(item.get("price") or {}) for item in items}
    price_ids.discard(None)
    if not price_ids:
        return None

    from django.db.models import Q

    query = Q()
    for price_id in price_ids:
        query |= Q(stripe_price_id_monthly=price_id) | Q(stripe_price_id_annual=price_id)
    return Plan.objects.filter(query).first()
