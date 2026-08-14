"""Team membership (design.md §6.1; implementation.md Phase 13).

**Adding a member requires an existing OCCS account.** design.md §6.1
declares `Membership.invited_by` and an `EmailToken` purpose for `INVITE`, and
`/accept-invite` exists as a landing page — the natural reading is a mint-a-
token, email-a-link flow that also on-boards someone with no account yet. This
implementation deliberately does not build that: `common.workspaces.
active_workspace` resolves a user's *single* workspace as the oldest
membership they hold, and every user already has one — the workspace
`workspaces.services.provisioning.provision_workspace` gave them at
registration. A second membership added by email is therefore reachable by
that user in the ORM and in every test, but there is no way for *them* to
switch into it: `active_workspace` has no per-request override yet (`common/
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
    default_detail = "No OCCS account exists with this email yet."


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


def change_role(membership: Membership, *, role: str) -> Membership:
    if membership.user_id == membership.workspace.owner_id:
        raise CannotModifyOwnerError(detail={"membership": membership.pk})
    _require_assignable(role)
    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    return membership


def remove_member(membership: Membership) -> None:
    if membership.user_id == membership.workspace.owner_id:
        raise CannotModifyOwnerError(detail={"membership": membership.pk})
    membership.delete()
