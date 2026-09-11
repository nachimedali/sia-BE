"""Give every workspace a chain, grandfather what is already scheduled, and
drop `requires_approval` (P2-04, P2-06).

Three steps that have to happen in this order and in one deploy:

1. **A default chain per workspace**, carrying forward exactly what the old
   boolean meant — `blocks_publish = requires_approval`. Nobody's review
   posture changes; only where it is stored.
2. **A grandfathering `APPROVE` action** for every post that has already been
   committed to going out without one. C-02 makes approval universal from here
   on, and a post that got past the old rules would otherwise sit in a system
   whose central invariant it violates — *no `SCHEDULED` post without an
   `APPROVE` row* would be false on day one, and the test asserting it would
   have to carve out an exception that never expires.
3. **Drop the column**, now that nothing reads it.

**Grandfathered with `ApprovalAction`, not `Decision`.** BUILD-PLAN C-02 names
`Decision`; that model arrives in Phase 5 and every field it carries beyond
`verdict`/`actor` — candidate FK, taste profile version, prompt context — is
Phase 5 machinery. Building it here would ship a model with nine null columns
whose shape Phase 5 would then change. `ApprovalAction` is Phase 2's audit
vehicle, is already append-only, and answers exactly the question C-02 asks:
*how did this post get past approval?* Phase 5's `Decision` sits at a different
point in the pipeline — the accept/reject of a *candidate*, before a `Post`
exists at all — and does not replace this row.

**Collapsed under the pre-launch exemption** (Part 3, recorded 2026-09-07):
`app-BE/` has never been deployed, so there are no live rows to protect and no
old code to run beside. On a deployed system steps 1–2 would ship, run, and be
verified before step 3 went out separately.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations

#: Kept in step with `workspaces.services.approvals.DEFAULT_CHAIN_NAME`. A
#: literal rather than an import: a migration must describe the world as it was
#: when written, and importing today's constant would let a rename in the
#: service silently rewrite history.
DEFAULT_CHAIN_NAME = "Default"

#: Statuses that mean "this post has been committed to going out". `DRAFT`,
#: `PENDING_REVIEW`, `CHANGES_REQUESTED` and `REJECTED` are not here: nothing
#: has been promised, so there is nothing to grandfather.
COMMITTED_STATUSES = (
    "APPROVED",
    "SCHEDULED",
    "REMINDER_ARMED",
    "PUBLISHING",
    "PUBLISHED",
    "FAILED",
    "PAUSED",
)

GRANDFATHER_NOTE = "grandfathered"


def forwards(apps: Any, schema_editor: Any) -> None:
    Workspace = apps.get_model("workspaces", "Workspace")
    ApprovalChain = apps.get_model("workspaces", "ApprovalChain")
    ApprovalAction = apps.get_model("workspaces", "ApprovalAction")
    Post = apps.get_model("content", "Post")

    for workspace in Workspace.objects.all().iterator():
        ApprovalChain.objects.get_or_create(
            workspace=workspace,
            is_default=True,
            defaults={
                "name": DEFAULT_CHAIN_NAME,
                "blocks_publish": workspace.requires_approval,
            },
        )

    approved_post_ids = set(
        ApprovalAction.objects.filter(action="APPROVE").values_list("post_id", flat=True)
    )
    ApprovalAction.objects.bulk_create(
        ApprovalAction(post_id=post_id, actor=None, action="APPROVE", note=GRANDFATHER_NOTE)
        for post_id in Post.objects.filter(status__in=COMMITTED_STATUSES)
        .exclude(pk__in=approved_post_ids)
        .values_list("pk", flat=True)
    )


def backwards(apps: Any, schema_editor: Any) -> None:
    """Restores the boolean from the chain and removes only what this migration
    wrote.

    A grandfathering row is identified by having no actor *and* this note —
    both, because `actor=None` alone will mean other things once the guest and
    system paths grow, and a reverse that deletes by one loose predicate is how
    a rollback takes real approvals with it.
    """
    Workspace = apps.get_model("workspaces", "Workspace")
    ApprovalChain = apps.get_model("workspaces", "ApprovalChain")
    ApprovalAction = apps.get_model("workspaces", "ApprovalAction")

    for chain in ApprovalChain.objects.filter(is_default=True).select_related("workspace"):
        Workspace.objects.filter(pk=chain.workspace_id).update(
            requires_approval=chain.blocks_publish
        )

    # Not `.delete()` on the model, which `AppendOnly` refuses — but the
    # historical model a migration gets is a plain `models.Model` without that
    # guard, which is exactly why the predicate above is narrow.
    ApprovalAction.objects.filter(
        action="APPROVE", actor__isnull=True, note=GRANDFATHER_NOTE
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0013_approval_chains"),
        # The grandfathering reads `content.Post`, and `content.0011` adds the
        # approval columns that hang off `0013`'s new tables. Depending on it
        # keeps the two apps' halves of this change in one ordered run.
        ("content", "0011_post_approval_state"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="workspace", name="requires_approval"),
    ]
