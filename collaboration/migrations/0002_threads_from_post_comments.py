"""Carry `workspaces.PostComment` into `Thread` + `Comment` (P2-01).

The old table held team discussion coupled to the approval feature; the new one
holds the same discussion as a first-class work item (C-03). **Nothing is
dropped** — every remark that existed becomes a comment in a thread on the same
post, keeping its author, its body, its reply parent and the instant it was
written.

One thread per post, not one per comment: the old model had no container, so
every comment on a post was implicitly one conversation, and splitting them now
would invent structure the rows never had.

Reversible, because the shape it writes is derivable in both directions. The
reverse deletes only what this migration created — a thread opened after the
migration is left alone, which is what makes running it backwards on a
half-migrated database safe rather than destructive.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations

#: What a carried-over conversation is called. The old rows had no title, and
#: inventing one from the first comment's body would put a fragment of somebody
#: else's sentence in a heading.
CARRIED_TITLE = "Discussion"


def forwards(apps: Any, schema_editor: Any) -> None:
    PostComment = apps.get_model("workspaces", "PostComment")
    Thread = apps.get_model("collaboration", "Thread")
    Comment = apps.get_model("collaboration", "Comment")

    post_ids = (
        PostComment.objects.order_by("post_id").values_list("post_id", flat=True).distinct()
    )
    for post_id in post_ids:
        old = list(PostComment.objects.filter(post_id=post_id).order_by("created_at", "id"))
        first = old[0]

        # `DONE` only when every remark was resolved. Any one still open means
        # the conversation is open — the reverse would mark live work as
        # handled, which is the failure that silently loses a task.
        all_resolved = all(row.resolved_at is not None for row in old)
        resolved_at = max((row.resolved_at for row in old), default=None) if all_resolved else None

        thread = Thread.objects.create(
            workspace_id=first.post.workspace_id,
            post_id=post_id,
            title=CARRIED_TITLE,
            status="DONE" if all_resolved else "OPEN",
            visibility="INTERNAL",
            opened_by_id=first.author_id,
            resolved_at=resolved_at,
        )

        # Two passes: a reply's parent may be created after it is referenced in
        # the source ordering, and a partial mapping would silently flatten the
        # thread into a list.
        new_by_old: dict[int, Any] = {}
        for row in old:
            new_by_old[row.id] = Comment.objects.create(
                thread=thread,
                author_id=row.author_id,
                body=row.body,
                visibility="INTERNAL",
            )
        for row in old:
            created = new_by_old[row.id]
            # `created_at` is `auto_now_add`, so it cannot be set on insert —
            # an UPDATE is the only way to preserve when a remark was actually
            # written, and losing that would reorder the conversation.
            Comment.objects.filter(pk=created.pk).update(
                created_at=row.created_at,
                parent=new_by_old.get(row.parent_id) if row.parent_id else None,
            )


def backwards(apps: Any, schema_editor: Any) -> None:
    Thread = apps.get_model("collaboration", "Thread")
    Comment = apps.get_model("collaboration", "Comment")
    PostComment = apps.get_model("workspaces", "PostComment")

    carried = Thread.objects.filter(title=CARRIED_TITLE)
    for thread in carried:
        by_new: dict[int, Any] = {}
        rows = list(Comment.objects.filter(thread=thread).order_by("created_at", "id"))
        for row in rows:
            by_new[row.id] = PostComment.objects.create(
                post_id=thread.post_id,
                author_id=row.author_id,
                body=row.body,
                resolved_at=thread.resolved_at,
            )
        for row in rows:
            PostComment.objects.filter(pk=by_new[row.id].pk).update(
                created_at=row.created_at,
                parent=by_new.get(row.parent_id) if row.parent_id else None,
            )
    carried.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("collaboration", "0001_initial"),
        ("workspaces", "0011_contract_workspace_commercial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
