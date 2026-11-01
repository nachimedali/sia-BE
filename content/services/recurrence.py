"""Recurrence (P1-10).

**A grid, not a queue.** Every scan recomputes the same slot times from the
rule and materialises only what nothing covers yet. That is the shape
`products.services.autopilot` already uses, and it buys three properties a
queue cannot: a worker that was down for a week backfills on its next run, a
rule edited today takes effect today rather than after the queue drains, and
two scans racing each other collide on `unique_recurrence_slot` instead of
double-creating.

It also means recurrence does **not replay**. A scan recomputes the *current*
window; slots that passed while nothing was running are gone. Posting last
Monday's spotlight on Thursday is worse than not posting it.

**Local wall time in, UTC out.** "Every Monday at 09:00" means 09:00 on the
office clock, so the rule is expanded naive in its own zone and each occurrence
is localised afterwards. Expanding in UTC would move the slot an hour twice a
year — precisely what Part 3's "store UTC, convert at edges" is about.

**An RRULE is user input.** `FREQ=SECONDLY` over a 30-day horizon is 2.6
million datetimes and a dead worker, so the frequency floor is validated at the
edge and the expansion is capped underneath it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.services.flags import CONTENT_MODEL_V2, TEMPLATE_RULES_S18, flag_enabled
from common.exceptions import InsufficientCredits, OCCSError
from content.models import (
    DeliveryMode,
    Post,
    PostStatus,
    RecurrenceOccurrence,
    RecurrenceRule,
)
from content.services.templates import apply_template, fill_placeholders, template_products

logger = logging.getLogger(__name__)

#: The finest cadence a rule may ask for. Daily, because this materialises a
#: *post* per slot and nothing below a day is a content calendar — it is a
#: mistake or an attack. The names are dateutil's own vocabulary.
FORBIDDEN_FREQUENCIES = frozenset({"SECONDLY", "MINUTELY", "HOURLY"})

#: A hard ceiling under the validation above, so an admin-edited or migrated
#: row cannot take a worker down. Sized well past any real calendar: 30 days of
#: four-a-day is 120.
MAX_SLOTS_PER_SCAN = 500


class InvalidRecurrenceError(OCCSError):
    default_code = "invalid_recurrence"
    default_detail = "This recurrence rule cannot be used."


def validate(*, rrule: str, timezone_name: str) -> None:
    """Raises `InvalidRecurrenceError` if the rule could not be safely
    expanded. Called from the serializer, so a bad rule is a 400 rather than a
    worker that never finishes."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidRecurrenceError(
            f"Unknown timezone '{timezone_name}'.", detail={"timezone": timezone_name}
        ) from exc

    upper = rrule.upper()
    for frequency in FORBIDDEN_FREQUENCIES:
        if f"FREQ={frequency}" in upper:
            raise InvalidRecurrenceError(
                f"{frequency.title()} recurrence is not supported; the finest cadence is daily.",
                detail={"rrule": rrule},
            )

    try:
        rrulestr(rrule, dtstart=dt.datetime(2026, 1, 1, tzinfo=None))
    except Exception as exc:  # dateutil raises bare ValueError/TypeError variants
        raise InvalidRecurrenceError(
            "This is not a valid RFC 5545 recurrence rule.", detail={"rrule": rrule}
        ) from exc

    # Referenced so a timezone that parses but resolves to nothing still fails
    # here rather than at the first scan.
    assert zone is not None


def planned_slots(rule: RecurrenceRule, *, now: dt.datetime | None = None) -> list[dt.datetime]:
    """Every slot between now and the horizon, in UTC.

    Deterministic: two scans over the same window must agree exactly, or the
    uniqueness constraint stops being able to recognise a duplicate.
    """
    moment = now or timezone.now()
    zone = ZoneInfo(rule.timezone)
    start_local = moment.astimezone(zone).replace(tzinfo=None)
    end_local = (
        (moment + dt.timedelta(days=rule.horizon_days)).astimezone(zone).replace(tzinfo=None)
    )
    # S18: From and Until are local days, both inclusive.
    if rule.starts_on is not None:
        start_local = max(start_local, dt.datetime.combine(rule.starts_on, dt.time.min))
    if rule.ends_on is not None:
        end_local = min(end_local, dt.datetime.combine(rule.ends_on, dt.time.max))
    if start_local > end_local:
        return []

    try:
        occurrences = rrulestr(rule.rrule, dtstart=start_local)
    except Exception:
        logger.exception("unparseable recurrence rule", extra={"rule_id": rule.pk})
        return []

    slots: list[dt.datetime] = []
    for naive in occurrences:
        if naive > end_local:
            break
        if len(slots) >= MAX_SLOTS_PER_SCAN:
            logger.warning(
                "recurrence expansion hit the slot ceiling",
                extra={"rule_id": rule.pk, "ceiling": MAX_SLOTS_PER_SCAN},
            )
            break
        # `fold=0` resolves the ambiguous hour an autumn DST shift creates to
        # the first occurrence. Either answer is defensible; picking one and
        # staying with it is what keeps two scans agreeing.
        slots.append(naive.replace(tzinfo=zone, fold=0).astimezone(dt.UTC))
    return slots


def materialise_slot(rule: RecurrenceRule, slot: dt.datetime) -> Post | None:
    """Creates the post for one slot, or returns `None` if it already exists.

    The `IntegrityError` catch is the race handler, not a fallback: two scans
    can both see the slot as uncovered and both try to write it. One wins on
    `unique_recurrence_slot` and the other finds out here, which is exactly
    what the grid is for.
    """
    author = rule.source.created_by or rule.source.workspace.organization.owner
    try:
        with transaction.atomic():
            if RecurrenceOccurrence.objects.filter(rule=rule, slot_at=slot).exists():
                return None
            post = apply_template(rule.source, author=author)
            RecurrenceOccurrence.objects.create(rule=rule, slot_at=slot, post=post)
            return post
    except IntegrityError:
        logger.info(
            "recurrence slot was taken by a concurrent scan",
            extra={"rule_id": rule.pk, "slot_at": slot.isoformat()},
        )
        return None


def materialise_due(*, now: dt.datetime | None = None) -> int:
    """Fills every active rule's window. Returns how many posts were created."""
    moment = now or timezone.now()
    created = 0

    rules = RecurrenceRule.objects.filter(active=True).select_related(
        "source", "source__workspace", "source__workspace__organization"
    )
    # One flag lookup per organization, not per rule — an org running several
    # recurring templates would otherwise ask the same question once per rule
    # in every scan.
    flag_by_org: dict[int, bool] = {}
    for rule in rules:
        organization = rule.source.workspace.organization
        enabled = flag_by_org.get(organization.pk)
        if enabled is None:
            enabled = flag_enabled(organization, CONTENT_MODEL_V2)
            flag_by_org[organization.pk] = enabled
        if not enabled:
            # Pre-phase behaviour: recurrence did not exist, so this
            # organization is passed by rather than erroring.
            continue
        if flag_enabled(organization, TEMPLATE_RULES_S18) and (
            products := template_products(rule.source)
        ):
            created += _materialise_products(rule, products, now=moment)
            continue
        covered = set(
            RecurrenceOccurrence.objects.filter(rule=rule).values_list("slot_at", flat=True)
        )
        for slot in planned_slots(rule, now=moment):
            if slot in covered:
                continue
            if materialise_slot(rule, slot) is not None:
                created += 1
    return created


# -----------------------------------------------------------------------------
# S18 — one draft per product per slot
# -----------------------------------------------------------------------------
#: Where a slot's post stands once a person has said yes to it.
_READY = frozenset(
    {
        PostStatus.APPROVED,
        PostStatus.SCHEDULED,
        PostStatus.REMINDER_ARMED,
        PostStatus.PUBLISHING,
        PostStatus.PUBLISHED,
    }
)
#: How far ahead a slot that will not go out is announced.
WARN_AHEAD = dt.timedelta(days=7)


def _materialise_products(rule: RecurrenceRule, products: list[Any], *, now: dt.datetime) -> int:
    """Every slot and product in the window that has no post yet. A skipped slot
    is tried again on every scan, so a top-up makes it."""
    occurrences = {(o.slot_at, o.product_id): o for o in rule.occurrences.all()}
    created = 0
    for slot in planned_slots(rule, now=now):
        for product in products:
            occurrence = occurrences.get((slot, product.pk))
            # shortcut: a scan that dies between claim and post leaves a row with
            # no post and no reason, never retried; sweep stale ones if it is seen.
            if occurrence is not None and (occurrence.post_id or not occurrence.skipped_reason):
                continue
            if make_product_slot(rule, slot, product, occurrence=occurrence) is not None:
                created += 1
    return created


def _claim(
    rule: RecurrenceRule, slot: dt.datetime, product: Any, occurrence: RecurrenceOccurrence | None
) -> tuple[RecurrenceOccurrence | None, bool]:
    """The occurrence this scan may fill, and whether it is new. `None` when
    another scan has it: a new row collides on the unique constraint, and a
    skipped one is taken by clearing its reason, which only one scan can do."""
    if occurrence is None:
        try:
            with transaction.atomic():
                return RecurrenceOccurrence.objects.create(
                    rule=rule, slot_at=slot, product=product
                ), True
        except IntegrityError:
            return None, False
    taken = RecurrenceOccurrence.objects.filter(
        pk=occurrence.pk, post__isnull=True, skipped_reason=occurrence.skipped_reason
    ).update(skipped_reason="")
    return (occurrence, False) if taken else (None, False)


def make_product_slot(
    rule: RecurrenceRule,
    slot: dt.datetime,
    product: Any,
    *,
    occurrence: RecurrenceOccurrence | None = None,
) -> Post | None:
    """One product's draft for one slot, submitted for review with the slot as
    its proposed time — approving it schedules it then (L-2: nobody's approval,
    nothing scheduled). Generating spends credits and never writes the product;
    a slot the balance cannot pay for makes no draft and tells the workspace."""
    from ai.models import GenerationKind, GenerationMode, GenerationStatus
    from billing.services.entitlements import entitlements_for
    from channels.services import active_accounts
    from integrations.generation import get_generation
    from notifications.models import EventKey
    from notifications.services import template_slot_event
    from scheduling.services import default_delivery_mode
    from workspaces.services.approvals import submit_for_review

    occurrence, fresh = _claim(rule, slot, product, occurrence)
    if occurrence is None:
        return None

    template = rule.source
    workspace = template.workspace
    payload = template.payload or {}
    author = template.created_by or workspace.organization.owner
    port = get_generation(workspace.organization)
    generate = [
        kind
        for kind, key, mode in (
            (GenerationKind.TEXT, "text_mode", "generate"),
            (GenerationKind.IMAGE, "image_mode", "generate"),
        )
        if payload.get(key) == mode
    ]
    cost = sum(port.resolve_cost(kind=kind, mode=GenerationMode.PRODUCT) for kind in generate)
    if cost:
        try:
            entitlements_for(workspace).require_credits(cost)
        except InsufficientCredits:
            occurrence.skipped_reason = "credits"
            occurrence.save(update_fields=["skipped_reason"])
            if fresh:
                template_slot_event(occurrence, event_key=EventKey.TEMPLATE_SLOT_SKIPPED)
            return None

    text = fill_placeholders(str(payload.get("master_body") or ""), product)
    photo = product.reference_images.first()
    media = [photo] if photo is not None else []
    for kind in generate:
        brief = (
            f"Write one social post for {product.name}, following this template: {text}"
            if kind == GenerationKind.TEXT
            else f"An image of {product.name} for this post: {text}"
        )
        try:
            generation = port.generate(
                workspace=workspace,
                user=author,
                kind=kind,
                mode=GenerationMode.PRODUCT,
                prompt=brief,
                product=product,
                category=workspace.category,
            )
        except OCCSError:
            # The debit is on pass only, so a failure cost nothing: the slot
            # keeps the placeholder text or the product photo instead.
            logger.warning("template slot generation failed", extra={"rule_id": rule.pk})
            continue
        variant = generation.variants.first()
        if generation.status != GenerationStatus.SUCCEEDED or variant is None:
            continue
        if kind == GenerationKind.TEXT and variant.body:
            text = variant.body
        elif kind == GenerationKind.IMAGE and variant.media_asset is not None:
            media = [variant.media_asset]

    accounts = list(
        active_accounts(workspace).filter(pk__in=payload.get("social_account_ids") or [])
    )
    with transaction.atomic():
        post = apply_template(template, author=author, product=product, body=text, media=media)
        post.planned_accounts = [account.pk for account in accounts]
        post.planned_platforms = sorted({account.platform for account in accounts})
        post.save(update_fields=["planned_accounts", "planned_platforms"])
        mode = default_delivery_mode(workspace) if accounts else DeliveryMode.REMINDER
        submit_for_review(post, actor=author, delivery_mode=mode, scheduled_at=slot)
        occurrence.post = post
        occurrence.skipped_reason = ""
        occurrence.save(update_fields=["post", "skipped_reason"])
    return post


def warn_due(*, now: dt.datetime | None = None) -> int:
    """Tells the workspace, once, about each product slot inside the coming
    week that will not go out as it stands: skipped, or still unapproved."""
    from notifications.models import EventKey
    from notifications.services import template_slot_event

    moment = now or timezone.now()
    due = RecurrenceOccurrence.objects.filter(
        product__isnull=False,
        warned_at__isnull=True,
        rule__active=True,
        slot_at__gt=moment,
        slot_at__lte=moment + WARN_AHEAD,
    ).select_related("post", "product", "rule__source__workspace__organization")
    warned = 0
    for occurrence in due:
        organization = occurrence.rule.source.workspace.organization
        if not flag_enabled(organization, TEMPLATE_RULES_S18):
            continue
        if occurrence.post is not None and occurrence.post.status in _READY:
            continue
        template_slot_event(occurrence, event_key=EventKey.TEMPLATE_SLOT_AT_RISK)
        occurrence.warned_at = moment
        occurrence.save(update_fields=["warned_at"])
        warned += 1
    return warned
