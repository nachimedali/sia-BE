"""Bulk operations (P3-12), and the honesty the P3-G1 gate asks for.

Three properties, each guarding a different plausible shortcut:

1. **One task per item, on the existing queues.** Never one task per batch: a
   slow provider on item 3 would stall items 4 to 200, and a batch that dies
   halfway would be indistinguishable from one that never started.
2. **Every item records its own outcome.** Without that the only available
   answers are "it worked" and "it did not", and a batch of two hundred where
   sixteen failed is neither.
3. **A batch is validated whole, before anything is written.** Discovering a
   foreign id at item 150 means 149 rows have already changed that nobody
   authorised — and a bulk operation has no undo.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from content.models import Post, PostStatus
from planning.models import (
    BulkOperation,
    BulkOperationItem,
    BulkStatus,
    Campaign,
    Label,
)
from workspaces.models import Workspace

#: Unbounded, one request becomes a hundred thousand queued tasks and a worker
#: fleet that does nothing else for an hour. The list view's own page cap is
#: 100, so a batch is at most a few screens of "select all matching filter".
MAX_BATCH = 500


def _apply_label(post: Post, payload: dict[str, Any], actor: User) -> None:
    from planning.services.labels import apply_labels

    # `payload["labels"]` is guaranteed non-empty here — `_validate_label_
    # payload` refuses the whole batch before any item exists — because
    # `apply_labels` **replaces** rather than adds, so a missing or empty list
    # reaching this point would silently strip every label from every selected
    # post, up to 500 at once, with the operation settling as `DONE`.
    label_ids = payload["labels"]
    labels = list(Label.objects.filter(pk__in=label_ids, workspace=post.workspace))
    missing = sorted(set(label_ids) - {label.pk for label in labels})
    if missing:
        raise ValidationError({"labels": f"No such label in this workspace: {missing}."})
    apply_labels(post, labels)


def _validate_label_payload(payload: dict[str, Any]) -> None:
    """Found in review: refused **up front**, not per item — a batch whose
    entire effect would be wiping every label from every selected post is
    refused whole, the same way a batch naming a foreign post is, rather than
    run to produce 500 individually-successful, silently destructive items.
    """
    labels = payload.get("labels")
    if not labels:
        raise ValidationError({"labels": "The label action needs at least one label id."})


def _add_to_campaign(post: Post, payload: dict[str, Any], actor: User) -> None:
    from planning.services.campaigns import add_post

    # Typed loose on purpose: the payload is whatever the caller sent, and the
    # lookup either matches a campaign in this workspace or does not.
    campaign_id: Any = payload.get("campaign")
    campaign = Campaign.objects.filter(pk=campaign_id, workspace=post.workspace).first()
    if campaign is None:
        raise ValidationError({"campaign": "No such campaign in this workspace."})
    add_post(campaign, post, actor=actor)


def _delete(post: Post, payload: dict[str, Any], actor: User) -> None:
    if post.status == PostStatus.PUBLISHED:
        # Per item rather than up front: the caller selected a *filter*, not a
        # list they read, so one ineligible post must not cancel the other 199.
        raise ValidationError({"post": "A published post cannot be deleted in bulk."})
    post.delete()


#: Declared as data, like every other table in this codebase: adding an action
#: is an entry plus a test, not a branch in a dispatcher.
ACTIONS: dict[str, Any] = {
    "label": _apply_label,
    "add_to_campaign": _add_to_campaign,
    "delete": _delete,
}

#: Per-action payload shape checks, run **once, before any row is written**
#: (`plan_operation`) — not per item. `add_to_campaign` and `delete` need no
#: entry: a missing campaign id fails safely per item with no default applied,
#: and `delete` reads no payload at all. Only `label`'s underlying replace
#: semantics make an empty payload destructive rather than merely wrong.
PAYLOAD_VALIDATORS: dict[str, Any] = {"label": _validate_label_payload}


def resolve_filters(workspace: Workspace, filters: dict[str, Any]) -> list[int]:
    """Turn a filter into the ids it matches, **on the server** (P3-09).

    The list page caps at 100, so a client that gathers ids from what it has
    rendered silently operates on the first page and then reports success over
    the other nine hundred. The fix is not a larger page: it is never asking
    the client for the list.

    Applied to a workspace-scoped queryset, so there is no value a caller can
    send that widens the selection past their own tenant.
    """
    from planning.services.views import validate_filters

    validate_filters(filters)
    queryset = Post.objects.filter(workspace=workspace)

    if statuses := filters.get("status"):
        queryset = queryset.filter(status__in=statuses)
    if kinds := filters.get("content_kind"):
        queryset = queryset.filter(content_kind__in=kinds)
    if labels := filters.get("label"):
        queryset = queryset.filter(labels__id__in=labels)
    if campaigns := filters.get("campaign"):
        queryset = queryset.filter(campaign_items__campaign_id__in=campaigns)
    if authors := filters.get("author"):
        queryset = queryset.filter(author_id__in=authors)
    if platforms := filters.get("platform"):
        # Found in review: declared as a valid key in `FILTER_KEYS` and never
        # applied here, so a filter naming a platform silently matched every
        # post on every platform instead. `Post.targets` is the join —
        # `distinct()` below already covers the duplicate rows a post with two
        # matching targets would otherwise contribute.
        queryset = queryset.filter(targets__platform__in=platforms)

    # `distinct` because the label and campaign joins are many-to-many: without
    # it a post carrying two of the selected labels arrives twice, and the
    # unique constraint on the item rows turns that into a 500 at bulk_create.
    return list(queryset.distinct().order_by("pk").values_list("pk", flat=True))


def plan_operation(
    *,
    workspace: Workspace,
    actor: User,
    action: str,
    payload: dict[str, Any],
    post_ids: list[int] | None = None,
    filters: dict[str, Any] | None = None,
) -> BulkOperation:
    """Validates the whole batch, then writes the operation and its items.

    **Nothing is written until every id has been checked.** A partially
    created operation over a batch that was going to be refused is worse than
    no operation: it looks like work in progress and there is nothing to
    resume.
    """
    if action not in ACTIONS:
        raise ValidationError({"action": f"Unknown action {action!r}.", "allowed": sorted(ACTIONS)})
    if validate_payload := PAYLOAD_VALIDATORS.get(action):
        validate_payload(payload)

    # **Exactly one selection.** Two in one request is ambiguous, and guessing
    # which the caller meant is how a bulk delete hits the wrong set.
    if (post_ids is not None) == (filters is not None):
        raise ValidationError(
            {"post_ids": "Select either a list of posts or a filter, not both and not neither."}
        )

    if filters is not None:
        post_ids = resolve_filters(workspace, filters)
        if not post_ids:
            # Rather than an operation of zero items settling as DONE, which
            # reads as "I did what you asked" over an empty selection.
            raise ValidationError({"filters": "That filter matches no posts."})

    if not post_ids:
        raise ValidationError({"post_ids": "Select at least one post."})
    if len(post_ids) > MAX_BATCH:
        raise ValidationError(
            {"post_ids": f"At most {MAX_BATCH} posts in one operation; this named {len(post_ids)}."}
        )

    unique_ids = list(dict.fromkeys(post_ids))
    found = set(
        Post.objects.filter(pk__in=unique_ids, workspace=workspace).values_list("pk", flat=True)
    )
    missing = [post_id for post_id in unique_ids if post_id not in found]
    if missing:
        # 400 rather than 404: the caller reached this collection legitimately
        # and the *batch* is what is wrong. A workspace-scoped queryset already
        # made another tenant's rows unreachable, so "missing" here means
        # exactly that — not a tenancy probe that needs a 404's silence.
        raise ValidationError({"post_ids": f"Some posts are not in this workspace: {missing}."})

    with transaction.atomic():
        operation = BulkOperation.objects.create(
            workspace=workspace,
            action=action,
            payload=payload,
            requested_by=actor,
            total_count=len(unique_ids),
        )
        BulkOperationItem.objects.bulk_create(
            [
                BulkOperationItem(operation=operation, post_id=post_id, post_ref=post_id)
                for post_id in unique_ids
            ]
        )
    return operation


def dispatch(operation: BulkOperation) -> None:
    """One task per item (P3-12), queued after the rows are committed.

    `on_commit` rather than a bare `.delay()`: a worker is fast enough to pick
    up an item id before this transaction commits, and would then find no row.
    """
    from planning.tasks import run_bulk_item

    item_ids = list(operation.items.values_list("pk", flat=True))

    def _queue() -> None:
        for item_id in item_ids:
            run_bulk_item.delay(item_id)

    transaction.on_commit(_queue)


def _settle(item: BulkOperationItem, *, status: str, error: str = "") -> None:
    """Records one item's outcome and rolls the operation's counters forward.

    The counters move here, inside the item's own transaction, so a crash
    between "item done" and "operation updated" cannot exist.
    """
    # A `delete` item has just deleted the very post this row points at, and
    # Django sets `pk = None` on the in-memory instance — which then refuses to
    # save the item at all ("unsaved related object"). Clearing the cached
    # relation is not a workaround: `SET_NULL` is exactly what the database did,
    # and `post_ref` still says which post it was.
    deleted_post: Any = item.post
    if deleted_post is not None and deleted_post.pk is None:
        item.post = None

    item.status = status
    item.error = error
    item.settled_at = timezone.now()
    item.save(update_fields=["post", "status", "error", "settled_at"])

    operation = BulkOperation.objects.select_for_update().get(pk=item.operation_id)
    counts = dict(
        operation.items.values_list("status")
        .annotate(total=Count("status"))
        .values_list("status", "total")
    )
    operation.succeeded_count = counts.get(BulkStatus.DONE, 0)
    operation.failed_count = counts.get(BulkStatus.FAILED, 0)
    settled = operation.succeeded_count + operation.failed_count

    if settled < operation.total_count:
        operation.status = BulkStatus.RUNNING
    elif operation.failed_count == 0:
        operation.status = BulkStatus.DONE
    elif operation.succeeded_count == 0:
        operation.status = BulkStatus.FAILED
    else:
        operation.status = BulkStatus.PARTIAL

    operation.save(update_fields=["status", "succeeded_count", "failed_count", "updated_at"])


def run_item(item_id: int) -> None:
    """Runs one item and **records** the outcome — it does not raise.

    A failing item is data, not an exception. Raising would retry the whole
    item on the queue's schedule and leave the operation with no record of what
    went wrong, so the customer sees a spinner rather than a reason.

    Idempotent: an item that has already settled is left alone, so a Celery
    retry after a successful run whose ack was lost cannot count twice.
    """
    with transaction.atomic():
        # `of=("self",)` because `post` is a nullable FK: `select_related`
        # makes it a LEFT OUTER JOIN, and Postgres refuses `FOR UPDATE` on the
        # nullable side of one. The row being locked is the item, not the post.
        item = (
            BulkOperationItem.objects.select_for_update(of=("self",))
            .select_related("operation", "post")
            .filter(pk=item_id)
            .first()
        )
        if item is None or item.status in (BulkStatus.DONE, BulkStatus.FAILED):
            return

        operation = item.operation
        if item.post is None:
            # The post went away between planning and running — deleted by
            # someone else, or by an earlier item of this same batch. Recorded
            # rather than raised: it is a true outcome, not a system failure.
            _settle(item, status=BulkStatus.FAILED, error="post: no longer exists.")
            return

        try:
            ACTIONS[operation.action](item.post, operation.payload, operation.requested_by)
        except Exception as error:
            _settle(item, status=BulkStatus.FAILED, error=_message(error))
        else:
            _settle(item, status=BulkStatus.DONE)


def _message(error: Exception) -> str:
    """The reason, in the words the service used.

    A DRF `ValidationError` carries its message inside a dict-of-lists; the raw
    `str()` of one is `{'labels': [ErrorDetail(...)]}`, which is a traceback in
    a customer's progress report.
    """
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict):
        return "; ".join(
            f"{key}: {' '.join(str(value) for value in values)}"
            if isinstance(values, list)
            else f"{key}: {values}"
            for key, values in detail.items()
        )
    if isinstance(detail, list):
        return " ".join(str(value) for value in detail)
    return str(error)
