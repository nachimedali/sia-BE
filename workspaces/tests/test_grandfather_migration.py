"""P2-06 — the grandfathering migration, run for real.

Not "the migration executes without error": what has to be true afterwards is
that **no post already committed to going out sits in a system whose central
invariant it violates**. Without this, `no SCHEDULED post without an APPROVE
row` would be false on the day Phase 2 shipped, and the test asserting it would
need an exception that never expires.

Rewinds the schema and rolls forward, the same shape `tests/test_phase0_gates.py`
uses and for the same reason: DDL cannot run inside the usual test transaction,
so these carry `transaction=True` and build their own rows through the
historical models.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

#: The state after the chain tables and the post columns exist, and before
#: anything has been backfilled into them — exactly where a database sits
#: mid-deploy.
#:
#: **Two targets, not one.** `workspaces.0014` is what is being rewound past,
#: and `content.0011` has to be named alongside it or the historical `Post`
#: model would be built from `workspaces:0013`'s ancestors only — which do not
#: include `content.0011`, so the model would lack the non-null columns the
#: table already has, and every insert here would fail on a constraint that has
#: nothing to do with the migration under test.
BEFORE_GRANDFATHER = [
    ("workspaces", "0013_approval_chains"),
    ("content", "0011_post_approval_state"),
]


def _migrate(targets: list[tuple[str, str]]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


def _migrate_fully_forward() -> None:
    """Head, every app. Leaf nodes rather than a named target so a migration
    added by a later phase is covered without anyone editing this."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture
def pre_grandfather_schema() -> Any:
    old_apps = _migrate(BEFORE_GRANDFATHER)
    try:
        yield old_apps
    finally:
        # In a `finally` because leaving the database on an old schema would
        # fail every test that ran after this one, with an error nowhere near
        # the cause.
        _migrate_fully_forward()


def _legacy_rows(old_apps: Any) -> dict[str, Any]:
    """A workspace holding one post of every interesting shape, built through
    the historical models — the only ones that still have `requires_approval`.
    """
    User = old_apps.get_model("accounts", "User")
    Organization = old_apps.get_model("workspaces", "Organization")
    Workspace = old_apps.get_model("workspaces", "Workspace")
    Post = old_apps.get_model("content", "Post")
    ApprovalAction = old_apps.get_model("workspaces", "ApprovalAction")

    user = User.objects.create(email="legacy@example.com", password="!", is_active=True)
    organization = Organization.objects.create(
        name="Legacy Co", slug="legacy-co", owner_id=user.pk, referral_code="legacy-code"
    )
    reviewed = Workspace.objects.create(
        organization=organization,
        name="Reviewed",
        slug="legacy-reviewed",
        requires_approval=True,
    )
    open_workspace = Workspace.objects.create(
        organization=organization,
        name="Open",
        slug="legacy-open",
        requires_approval=False,
    )

    def post(workspace: Any, status: str, body: str) -> Any:
        return Post.objects.create(
            workspace=workspace, author_id=user.pk, master_body=body, status=status
        )

    scheduled = post(open_workspace, "SCHEDULED", "Already going out")
    published = post(open_workspace, "PUBLISHED", "Already gone")
    draft = post(open_workspace, "DRAFT", "Nothing promised")
    rejected = post(open_workspace, "REJECTED", "Said no")

    properly_approved = post(reviewed, "SCHEDULED", "Reviewed the old way")
    ApprovalAction.objects.create(
        post_id=properly_approved.pk, actor_id=user.pk, action="APPROVE", note="looks good"
    )

    return {
        "user": user,
        "reviewed": reviewed,
        "open": open_workspace,
        "scheduled": scheduled,
        "published": published,
        "draft": draft,
        "rejected": rejected,
        "properly_approved": properly_approved,
    }


def test_every_workspace_lands_with_a_chain_that_means_what_the_boolean_did(
    pre_grandfather_schema: Any,
) -> None:
    rows = _legacy_rows(pre_grandfather_schema)

    _migrate_fully_forward()

    from workspaces.models import ApprovalChain

    reviewed = ApprovalChain.objects.get(workspace_id=rows["reviewed"].pk, is_default=True)
    opened = ApprovalChain.objects.get(workspace_id=rows["open"].pk, is_default=True)

    assert reviewed.blocks_publish is True
    assert opened.blocks_publish is False
    # Nobody's review posture changed; only where it is stored.
    assert reviewed.name == "Default"


def test_committed_posts_are_grandfathered_and_the_rest_are_left_alone(
    pre_grandfather_schema: Any,
) -> None:
    rows = _legacy_rows(pre_grandfather_schema)

    _migrate_fully_forward()

    from workspaces.models import ApprovalAction

    def approvals_for(post: Any) -> list[Any]:
        return list(ApprovalAction.objects.filter(post_id=post.pk, action="APPROVE"))

    # Committed without an approval: one synthetic row, naming nobody.
    for key in ("scheduled", "published"):
        (row,) = approvals_for(rows[key])
        assert row.actor_id is None
        assert row.note == "grandfathered"

    # Nothing was promised, so there is nothing to grandfather.
    assert approvals_for(rows["draft"]) == []
    assert approvals_for(rows["rejected"]) == []

    # Already approved by a person: untouched, and **not** duplicated.
    (existing,) = approvals_for(rows["properly_approved"])
    assert existing.actor_id == rows["user"].pk
    assert existing.note == "looks good"


def test_the_invariant_holds_across_the_whole_corpus_after_migrating(
    pre_grandfather_schema: Any,
) -> None:
    """The point of the whole migration, stated as the invariant itself."""
    _legacy_rows(pre_grandfather_schema)

    _migrate_fully_forward()

    from content.models import Post
    from workspaces.models import ApprovalAction

    committed = Post.objects.filter(
        status__in=["SCHEDULED", "REMINDER_ARMED", "PUBLISHING", "PUBLISHED"]
    )
    assert committed.exists()
    for post in committed:
        assert ApprovalAction.objects.filter(post=post, action="APPROVE").exists(), (
            f"post {post.pk} survived the migration in {post.status} with no approval"
        )
