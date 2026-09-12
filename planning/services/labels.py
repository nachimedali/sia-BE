"""Labels (P3-10)."""

from __future__ import annotations

from collections.abc import Sequence

from rest_framework.exceptions import ValidationError

from billing.services.entitlements import entitlements_for
from content.models import Post
from planning.models import Label
from workspaces.models import Workspace


def create_label(*, workspace: Workspace, name: str, colour: str) -> Label:
    entitlements_for(workspace).check_quota(
        "max_labels", Label.objects.filter(workspace=workspace).count()
    )
    label = Label(workspace=workspace, name=name, colour=colour)
    label.full_clean(exclude=["workspace"])
    label.save()
    return label


def apply_labels(post: Post, labels: Sequence[Label]) -> Post:
    """**Replaces** the post's labels, and refuses another tenant's.

    Validating the ids is not the same as validating the *references*: a label
    id is an integer whichever workspace it belongs to, which is the hole P1-11
    found in platform options and P3-02 guards in image blocks.
    """
    foreign = [label.pk for label in labels if label.workspace_id != post.workspace_id]
    if foreign:
        raise ValidationError({"labels": f"No such label in this workspace: {sorted(foreign)}."})

    post.labels.set(labels)
    return post
