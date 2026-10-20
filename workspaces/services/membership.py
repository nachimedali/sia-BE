"""Team membership (design.md §6.1; implementation.md Phase 13).

**Adding a member requires an existing ComposeVision account.** design.md §6.1
declares `Membership.invited_by` and an `EmailToken` purpose for `INVITE`, and
`/accept-invite` exists as a landing page — the natural reading is a mint-a-
token, email-a-link flow that also on-boards someone with no account yet. This
implementation deliberately does not build that: `common.workspaces.
request_workspace` resolves a user's *single* workspace as the oldest
membership they hold, and every user already has one — the workspace
`workspaces.services.provisioning.provision_workspace` gave them at
registration. A second membership added by email is therefore reachable by
that user in the ORM and in every test, but there is no way for *them* to
switch into it: `request_workspace` has no per-request override yet (`common/
mixins.py`'s own comment flags this as "the single place that will need to
change" for a workspace-switcher, still unbuilt). Building the full
token-and-registration invite flow on top of a foundation that cannot let the
invitee act in the new workspace would ship more ceremony around the same
limitation, not more capability — so this ships the smaller, real half
(add an existing account, by email, to the team roster) and leaves the
email-invite/new-account path for whichever phase adds workspace switching.
`invited_by` is still recorded, so nothing here forecloses that migration
later.
"""

from __future__ import annotations

from typing import Any

from accounts.models import User
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError, StateConflict
from workspaces.models import Membership, Role, Workspace

#: `OWNER` is deliberately excluded: it is not a role this endpoint may grant
#: or revoke, because `Workspace.owner` — the FK Stripe billing identity and
#: every "who may see billing" check hangs off — is a separate field this
#: module never touches. Ownership transfer is a bigger, more sensitive
#: decision than a role change and is not this phase's to make.
ASSIGNABLE_ROLES = frozenset({Role.ADMIN, Role.EDITOR, Role.CONTRIBUTOR, Role.VIEWER})


class RoleNotAssignableError(OCCSError):
    default_code = "role_not_assignable"
    default_detail = "This role cannot be assigned through this endpoint."


class NoAccountForEmailError(OCCSError):
    default_code = "no_account_for_email"
    default_detail = "No ComposeVision account exists with this email yet."


class MemberAlreadyExistsError(StateConflict):
    """409, not 400: the request is well-formed, it just conflicts with the
    workspace's existing membership state."""

    default_code = "member_already_exists"
    default_detail = "This user is already a member of the workspace."


class CannotModifyOwnerError(OCCSError):
    default_code = "cannot_modify_owner"
    default_detail = "The workspace owner's membership cannot be changed here."


def _require_assignable(role: str) -> None:
    if role not in ASSIGNABLE_ROLES:
        raise RoleNotAssignableError(detail={"role": role})


def add_member(workspace: Workspace, *, email: str, role: str, invited_by: Any) -> Membership:
    _require_assignable(role)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        raise NoAccountForEmailError(detail={"email": email})

    if Membership.objects.filter(user=user, workspace=workspace).exists():
        raise MemberAlreadyExistsError(detail={"email": email})

    # I8: the cap itself lives on `Plan`, never a literal here.
    entitlements_for(workspace).check_quota(
        "max_workspace_members",
        current=Membership.objects.filter(workspace=workspace).count(),
    )
    return Membership.objects.create(
        user=user, workspace=workspace, role=role, invited_by=invited_by
    )


def _refuse_owner(membership: Membership) -> None:
    """The organization's owner holds everything and cannot be changed here."""
    if membership.user_id == membership.workspace.organization.owner_id:
        raise CannotModifyOwnerError(detail={"membership": membership.pk})


def change_role(membership: Membership, *, role: str) -> Membership:
    _refuse_owner(membership)
    _require_assignable(role)
    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    return membership


def set_permissions(membership: Membership, *, permissions: list[str]) -> Membership:
    """P0-48, P5: the authority set, one permission at a time. The role stays as
    the display preset it was. The owner always holds everything, so narrowing
    them is refused rather than silently ignored."""
    _refuse_owner(membership)
    membership.permissions = sorted(set(permissions))
    membership.save(update_fields=["permissions", "updated_at"])
    return membership


class OwnsItemsError(StateConflict):
    """409: removing this member would orphan threads assigned to them and
    posts they scheduled. Name who takes them over."""

    default_code = "owns_items"
    default_detail = "Choose who takes over their threads and scheduled posts."


class InvalidReassignError(OCCSError):
    default_code = "invalid_reassign"
    default_detail = "Pick another member of this workspace."


def _owned(membership: Membership) -> tuple[Any, Any]:
    from collaboration.models import Thread, ThreadStatus
    from content.models import Post, PostStatus

    threads = Thread.objects.filter(
        workspace=membership.workspace, assignee_id=membership.user_id
    ).exclude(status=ThreadStatus.DONE)
    scheduled = Post.objects.filter(
        workspace=membership.workspace, author_id=membership.user_id, status=PostStatus.SCHEDULED
    )
    return threads, scheduled


def ownership(membership: Membership) -> dict[str, Any]:
    """What this member still owns in this workspace (P5): open threads
    assigned to them, and posts they scheduled that have not gone out. Shown
    before removal so the person removing them knows what changes hands."""
    threads, scheduled = _owned(membership)
    items = [
        {"kind": "thread", "id": thread.pk, "title": thread.title}
        for thread in threads.order_by("-created_at")[:20]
    ] + [
        {"kind": "post", "id": post.pk, "title": str(post), "at": post.scheduled_at}
        for post in scheduled.order_by("scheduled_at")[:20]
    ]
    return {"threads": threads.count(), "scheduled": scheduled.count(), "items": items}


def remove_member(
    membership: Membership, *, reassign_to: Membership | None = None, actor: Any = None
) -> int:
    """Removes a member, handing their open work to `reassign_to` (P5).

    Refused (409 `owns_items`) when they still own threads or scheduled posts
    and nobody is named: a removal that orphans a scheduled post leaves it to
    fail at publish time with no one watching. Published history keeps its
    real author; only what is still in motion changes hands. Returns how many
    items moved.
    """
    from workspaces.services.approvals import log

    _refuse_owner(membership)

    threads, scheduled = _owned(membership)
    thread_count, scheduled_count = threads.count(), scheduled.count()
    owned = thread_count + scheduled_count
    if owned and reassign_to is None:
        raise OwnsItemsError(detail={"threads": thread_count, "scheduled": scheduled_count})
    if reassign_to is not None and (
        reassign_to.workspace_id != membership.workspace_id or reassign_to.pk == membership.pk
    ):
        raise InvalidReassignError(detail={"reassign_to": reassign_to.pk})

    moved = 0
    if owned and reassign_to is not None:
        moved += threads.update(assignee_id=reassign_to.user_id)
        moved += scheduled.update(author_id=reassign_to.user_id)

    log(
        workspace=membership.workspace,
        actor=actor,
        verb="member.removed",
        target_repr=membership.user.email,
        meta={"moved": moved, "to": reassign_to.user.email if reassign_to else None},
    )
    membership.delete()
    return moved
