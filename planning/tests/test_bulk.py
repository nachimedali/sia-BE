"""Bulk operations, and the P3-G1 ship gate: **partial failure reported honestly**.

Three rules, each protecting against a different plausible shortcut:

1. **One task per item, on the existing queues** — never one task per batch. A
   slow provider on item 3 must not stall items 4 to 200, and a batch that dies
   halfway must not be indistinguishable from one that never ran.
2. **Per-item outcome recorded**, so the UI can say "184 scheduled, 16 failed,
   here is why" rather than a green tick over a half-done job.
3. **Cross-workspace batches are checked up front and rejected whole.** A batch
   that runs until it hits the workspace the caller lacks permission in has
   already changed rows nobody authorised, and there is no undo.
"""

from __future__ import annotations

from typing import Any

import pytest

from content.services.posts import create_post
from planning.models import BulkOperation, BulkOperationItem, BulkStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/bulk-operations/"


def _posts(workspace: Any, user: Any, count: int = 3) -> list[Any]:
    return [
        create_post(workspace=workspace, author=user, master_body=f"Post {index}")
        for index in range(count)
    ]


class TestPlanning:
    def test_an_operation_records_one_item_per_post(self, workspace: Any, user: Any) -> None:
        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user)

        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="delete",
            post_ids=[p.id for p in posts],
            payload={},
        )

        assert operation.items.count() == 3
        assert operation.status == BulkStatus.PENDING

    def test_an_empty_batch_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        with pytest.raises(ValidationError):
            plan_operation(workspace=workspace, actor=user, action="label", post_ids=[], payload={})

    def test_an_unknown_action_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 1)

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="detonate",
                post_ids=[posts[0].id],
                payload={},
            )

    def test_a_batch_naming_another_workspaces_post_is_rejected_whole(
        self, workspace: Any, user: Any, other_user: Any
    ) -> None:
        """**Rejected up front, and nothing is written.**

        Discovering the foreign id halfway would mean rows already changed that
        nobody authorised — and a bulk operation has no undo.
        """
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        mine = _posts(workspace, user, 2)
        yours = create_post(workspace=theirs, author=other_user, master_body="Theirs")

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="delete",
                post_ids=[mine[0].id, yours.id, mine[1].id],
                payload={},
            )

        assert BulkOperation.objects.count() == 0
        assert BulkOperationItem.objects.count() == 0

    def test_the_batch_is_capped(self, workspace: Any, user: Any) -> None:
        # Unbounded, one request becomes a hundred thousand queued tasks.
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import MAX_BATCH, plan_operation

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="delete",
                post_ids=list(range(MAX_BATCH + 1)),
                payload={},
            )


class TestExecution:
    def test_each_item_is_its_own_task(
        self, workspace: Any, user: Any, django_capture_on_commit_callbacks: Any
    ) -> None:
        """One task per item, never one per batch (P3-12).

        Queued through `transaction.on_commit`, so the test has to run the
        callbacks: a worker is quick enough to pick up an item id before the
        planning transaction commits, and would then find no row.
        """
        from unittest.mock import patch

        from planning.services.bulk import dispatch, plan_operation

        posts = _posts(workspace, user)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="delete",
            post_ids=[p.id for p in posts],
            payload={},
        )

        with (
            patch("planning.tasks.run_bulk_item.delay") as delayed,
            django_capture_on_commit_callbacks(execute=True) as callbacks,
        ):
            dispatch(operation)

        assert len(callbacks) == 1, "dispatch registers one commit hook, not one per item"
        assert delayed.call_count == 3

    def test_an_item_that_succeeds_is_recorded_as_done(self, workspace: Any, user: Any) -> None:
        from planning.models import Label
        from planning.services.bulk import plan_operation, run_item

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        post = _posts(workspace, user, 1)[0]
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[post.id],
            payload={"labels": [label.id]},
        )
        item = operation.items.get()

        run_item(item.id)

        item.refresh_from_db()
        assert item.status == BulkStatus.DONE
        assert list(post.labels.all()) == [label]

    def test_an_item_that_fails_records_why_and_does_not_raise(
        self, workspace: Any, user: Any
    ) -> None:
        """The gate (P3-G1). A failing item is **data**, not an exception.

        Raising would retry the whole item forever and leave the operation with
        no record of what went wrong — the customer sees a spinner, not a
        reason.
        """
        from planning.services.bulk import plan_operation, run_item

        post = _posts(workspace, user, 1)[0]
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[post.id],
            payload={"labels": [999_999]},
        )
        item = operation.items.get()

        run_item(item.id)

        item.refresh_from_db()
        assert item.status == BulkStatus.FAILED
        assert item.error

    def test_a_partly_failed_operation_is_partial_not_done(self, workspace: Any, user: Any) -> None:
        """**The ship gate.** A batch where 2 of 3 worked says so."""
        from planning.models import Label
        from planning.services.bulk import plan_operation, run_item

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        posts = _posts(workspace, user, 3)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[p.id for p in posts],
            payload={"labels": [label.id]},
        )
        items = list(operation.items.order_by("id"))

        run_item(items[0].id)
        run_item(items[1].id)
        # The third is made to fail by removing the label it would apply.
        label.delete()
        run_item(items[2].id)

        operation.refresh_from_db()
        assert operation.status == BulkStatus.PARTIAL
        assert operation.succeeded_count == 2
        assert operation.failed_count == 1

    def test_an_operation_where_everything_worked_is_done(self, workspace: Any, user: Any) -> None:
        from planning.models import Label
        from planning.services.bulk import plan_operation, run_item

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        posts = _posts(workspace, user, 2)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[p.id for p in posts],
            payload={"labels": [label.id]},
        )
        for item in operation.items.all():
            run_item(item.id)

        operation.refresh_from_db()
        assert operation.status == BulkStatus.DONE
        assert operation.failed_count == 0

    def test_an_operation_where_nothing_worked_is_failed(self, workspace: Any, user: Any) -> None:
        # Distinguished from PARTIAL on purpose: "none of it worked" usually
        # means one systemic cause, and reporting it as partial success sends
        # the reader hunting through 200 identical errors.
        from planning.services.bulk import plan_operation, run_item

        posts = _posts(workspace, user, 2)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[p.id for p in posts],
            payload={"labels": [999_999]},
        )
        for item in operation.items.all():
            run_item(item.id)

        operation.refresh_from_db()
        assert operation.status == BulkStatus.FAILED

    def test_running_an_item_twice_does_not_double_count(self, workspace: Any, user: Any) -> None:
        # A Celery retry after a successful run that lost its ack would
        # otherwise report three successes from two posts.
        from planning.models import Label
        from planning.services.bulk import plan_operation, run_item

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        post = _posts(workspace, user, 1)[0]
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[post.id],
            payload={"labels": [label.id]},
        )
        item = operation.items.get()

        run_item(item.id)
        run_item(item.id)

        operation.refresh_from_db()
        assert operation.succeeded_count == 1


class TestActions:
    def test_bulk_labelling(self, workspace: Any, user: Any) -> None:
        from planning.models import Label
        from planning.services.bulk import plan_operation, run_item

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        posts = _posts(workspace, user, 2)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[p.id for p in posts],
            payload={"labels": [label.id]},
        )
        for item in operation.items.all():
            run_item(item.id)

        assert all(post.labels.count() == 1 for post in posts)

    def test_bulk_adding_to_a_campaign(self, workspace: Any, user: Any) -> None:
        import datetime as dt

        from django.utils import timezone

        from planning.models import Campaign, CampaignItem
        from planning.services.bulk import plan_operation, run_item

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring",
            starts_at=timezone.now(),
            ends_at=timezone.now() + dt.timedelta(days=7),
        )
        posts = _posts(workspace, user, 2)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="add_to_campaign",
            post_ids=[p.id for p in posts],
            payload={"campaign": campaign.id},
        )
        for item in operation.items.all():
            run_item(item.id)

        assert CampaignItem.objects.filter(campaign=campaign).count() == 2

    def test_bulk_deleting_drafts(self, workspace: Any, user: Any) -> None:
        from content.models import Post
        from planning.services.bulk import plan_operation, run_item

        posts = _posts(workspace, user, 2)
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="delete",
            post_ids=[p.id for p in posts],
            payload={},
        )
        for item in operation.items.all():
            run_item(item.id)

        assert Post.objects.filter(workspace=workspace).count() == 0

    def test_bulk_delete_refuses_a_published_post(self, workspace: Any, user: Any) -> None:
        # Reported per item rather than refused up front: the caller selected a
        # filter, not a list they read, so one ineligible post must not cancel
        # the other 199.
        from content.models import PostStatus
        from planning.services.bulk import plan_operation, run_item

        post = _posts(workspace, user, 1)[0]
        post.status = PostStatus.PUBLISHED
        post.save(update_fields=["status"])
        operation = plan_operation(
            workspace=workspace, actor=user, action="delete", post_ids=[post.id], payload={}
        )
        item = operation.items.get()

        run_item(item.id)

        item.refresh_from_db()
        assert item.status == BulkStatus.FAILED
        assert "published" in item.error.lower()


class TestApi:
    def test_starting_a_bulk_operation(self, auth_client: Any, workspace: Any, user: Any) -> None:
        from unittest.mock import patch

        posts = _posts(workspace, user, 2)

        with patch("planning.tasks.run_bulk_item.delay"):
            response = auth_client.post(
                URL,
                {"action": "delete", "post_ids": [p.id for p in posts], "payload": {}},
                format="json",
            )

        assert response.status_code == 202, response.json()
        assert response.json()["status"] == "PENDING"

    def test_a_batch_with_a_foreign_post_is_400_and_writes_nothing(
        self, auth_client: Any, workspace: Any, user: Any, other_user: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        foreign = create_post(workspace=theirs, author=other_user, master_body="Theirs")

        response = auth_client.post(
            URL, {"action": "delete", "post_ids": [foreign.id], "payload": {}}, format="json"
        )

        assert response.status_code == 400
        assert BulkOperation.objects.count() == 0

    def test_reading_back_the_per_item_outcome(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        from planning.services.bulk import plan_operation, run_item

        post = _posts(workspace, user, 1)[0]
        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[post.id],
            payload={"labels": [999_999]},
        )
        run_item(operation.items.get().id)

        response = auth_client.get(f"{URL}{operation.id}/")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["items"][0]["error"]

    def test_another_workspaces_operation_is_404(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        from planning.services.bulk import plan_operation
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        post = create_post(workspace=theirs, author=other_user, master_body="Theirs")
        operation = plan_operation(
            workspace=theirs, actor=other_user, action="delete", post_ids=[post.id], payload={}
        )

        assert auth_client.get(f"{URL}{operation.id}/").status_code == 404


class TestSelectAllMatchingFilter:
    """P3-09: selection by **filter**, resolved on the server.

    The list page caps at 100, so a client that collects ids from what it has
    rendered silently operates on the first page and reports success over the
    other 900. The fix is not a bigger page — it is never asking the client for
    the list at all.
    """

    def test_a_filter_selects_everything_that_matches(self, workspace: Any, user: Any) -> None:
        from content.models import PostStatus
        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 5)
        for post in posts[:3]:
            post.status = PostStatus.PENDING_REVIEW
            post.save(update_fields=["status"])

        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="delete",
            filters={"status": ["PENDING_REVIEW"]},
            payload={},
        )

        assert operation.total_count == 3

    def test_a_filter_reaches_past_the_page_cap(self, workspace: Any, user: Any) -> None:
        """The whole point. 120 posts is past the 100-row page."""
        from planning.services.bulk import plan_operation

        _posts(workspace, user, 120)

        operation = plan_operation(
            workspace=workspace, actor=user, action="delete", filters={}, payload={}
        )

        assert operation.total_count == 120

    def test_a_filter_never_crosses_the_workspace(
        self, workspace: Any, user: Any, other_user: Any
    ) -> None:
        # The filter is applied to a workspace-scoped queryset, so there is no
        # value a caller can send that widens it.
        from planning.services.bulk import plan_operation
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        create_post(workspace=theirs, author=other_user, master_body="Theirs")
        _posts(workspace, user, 2)

        operation = plan_operation(
            workspace=workspace, actor=user, action="delete", filters={}, payload={}
        )

        assert operation.total_count == 2

    def test_an_unknown_filter_key_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="delete",
                filters={"nope": ["x"]},
                payload={},
            )

    def test_a_filter_matching_nothing_is_refused(self, workspace: Any, user: Any) -> None:
        # Rather than an operation of zero items reporting DONE, which reads as
        # "I did what you asked" over a selection that was empty.
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="delete",
                filters={"status": ["PUBLISHED"]},
                payload={},
            )

    def test_ids_and_a_filter_together_are_refused(self, workspace: Any, user: Any) -> None:
        # Two selections in one request is ambiguous, and guessing which the
        # user meant is how a bulk delete hits the wrong set.
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 1)

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="delete",
                post_ids=[posts[0].id],
                filters={},
                payload={},
            )

    def test_neither_ids_nor_a_filter_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        with pytest.raises(ValidationError):
            plan_operation(workspace=workspace, actor=user, action="delete", payload={})

    def test_a_filter_wider_than_the_cap_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import MAX_BATCH, plan_operation

        _posts(workspace, user, MAX_BATCH + 1)

        with pytest.raises(ValidationError):
            plan_operation(workspace=workspace, actor=user, action="delete", filters={}, payload={})

    def test_a_platform_filter_actually_narrows_the_selection(
        self, workspace: Any, user: Any
    ) -> None:
        """Found in review: `platform` is declared valid in `FILTER_KEYS` — a
        caller filtering by it gets no error — but `resolve_filters` never
        applied it, so the selection silently widened to every post on every
        platform regardless of what was asked for.
        """
        from content.models import Platform, PostTarget
        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 3)
        PostTarget.objects.create(post=posts[0], platform=Platform.INSTAGRAM)
        PostTarget.objects.create(post=posts[1], platform=Platform.LINKEDIN)
        # posts[2] carries no target at all.

        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="delete",
            filters={"platform": ["instagram"]},
            payload={},
        )

        assert operation.total_count == 1
        assert operation.items.get().post_ref == posts[0].id

    def test_the_api_accepts_a_filter(self, auth_client: Any, workspace: Any, user: Any) -> None:
        from unittest.mock import patch

        _posts(workspace, user, 3)

        with patch("planning.tasks.run_bulk_item.delay"):
            response = auth_client.post(
                URL,
                {"action": "delete", "filters": {}, "payload": {}},
                format="json",
            )

        assert response.status_code == 202, response.json()
        assert response.json()["total_count"] == 3


class TestLabelPayloadCannotSilentlyWipe:
    """Found in review: `_apply_label` read `payload.get("labels", [])`, and
    `apply_labels` **replaces** — so a batch with no `labels` key, or an
    explicitly empty one, stripped every label from every selected post with
    `status: DONE` and no error anywhere. Refused up front, before any row is
    written, matching every other whole-batch check `plan_operation` already
    does.
    """

    def test_a_missing_labels_key_is_refused_up_front(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 1)

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="label",
                post_ids=[posts[0].id],
                payload={},
            )

        assert BulkOperation.objects.count() == 0

    def test_an_explicitly_empty_labels_list_is_refused(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 1)

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="label",
                post_ids=[posts[0].id],
                payload={"labels": []},
            )

    def test_the_batch_is_rejected_before_any_item_is_written(
        self, workspace: Any, user: Any
    ) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.bulk import plan_operation

        posts = _posts(workspace, user, 3)

        with pytest.raises(ValidationError):
            plan_operation(
                workspace=workspace,
                actor=user,
                action="label",
                post_ids=[p.id for p in posts],
                payload={"labels": []},
            )

        assert BulkOperationItem.objects.count() == 0

    def test_a_real_label_still_applies(self, workspace: Any, user: Any) -> None:
        # The fix must not regress the ordinary case.
        from planning.models import Label
        from planning.services.bulk import plan_operation

        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        posts = _posts(workspace, user, 1)

        operation = plan_operation(
            workspace=workspace,
            actor=user,
            action="label",
            post_ids=[posts[0].id],
            payload={"labels": [label.id]},
        )

        assert operation.total_count == 1


class TestViewOnlyCanReadBulkOperations:
    """Found in review: `BulkOperationViewSet` required `edit` for every
    action including `list`/`retrieve`, unlike the sibling `Label`/`SavedView`/
    `Timetable` ViewSets in the same file, which split `view` for reads and
    `edit` for writes. The class's own docstring calls itself "Create-and-read
    only" — a `view`-only member could not read it at all.
    """

    def test_a_view_only_member_can_list_operations(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any, admin_user: Any
    ) -> None:
        from planning.services.bulk import plan_operation

        post = create_post(workspace=advanced_workspace, author=admin_user, master_body="One")
        plan_operation(
            workspace=advanced_workspace,
            actor=admin_user,
            action="delete",
            post_ids=[post.id],
            payload={},
        )

        response = client_as(viewer_user).get(URL)

        assert response.status_code == 200

    def test_a_view_only_member_can_read_one_operation(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any, admin_user: Any
    ) -> None:
        from planning.services.bulk import plan_operation

        post = create_post(workspace=advanced_workspace, author=admin_user, master_body="One")
        operation = plan_operation(
            workspace=advanced_workspace,
            actor=admin_user,
            action="delete",
            post_ids=[post.id],
            payload={},
        )

        response = client_as(viewer_user).get(f"{URL}{operation.id}/")

        assert response.status_code == 200

    def test_a_view_only_member_still_cannot_start_one(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any
    ) -> None:
        response = client_as(viewer_user).post(
            URL, {"action": "delete", "post_ids": [1], "payload": {}}, format="json"
        )

        assert response.status_code == 403
