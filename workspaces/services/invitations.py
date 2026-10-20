"""Inviting someone into a workspace (P0-16, P0-47).

The ship gate is precise about the hard case: *"invites someone with no
account"*. Everything here is shaped by that — the invitation is addressed to
an email, the acceptance path creates the account when there is not one, and
both halves land in a single transaction so a half-accepted invite cannot
exist.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from accounts.passwords import password_problems
from common.mail import Email, first_name_of, get_mail_sender
from workspaces.models import (
    Invitation,
    Membership,
    OrganizationMembership,
    Permission,
    Role,
    Workspace,
    resolved_permissions,
)

logger = logging.getLogger(__name__)


def invite(
    *, workspace: Workspace, email: str, role: str, invited_by: Any = None, base_url: str
) -> Invitation:
    """Mints an invitation and emails the link.

    Mail is sent after the row is committed, not inside the transaction: a
    delivery failure must not roll back an invitation the admin can simply
    resend, and a rollback after a successful send would leave a live link
    pointing at nothing.
    """
    invitation, raw = Invitation.issue(
        workspace=workspace, email=email, role=role, invited_by=invited_by
    )

    context = _invitation_context(invitation, invited_by)
    get_mail_sender().send(
        Email(
            to=invitation.email,
            subject=f"{context['inviter_name']} invited you to {workspace.name}",
            template="workspace_invitation",
            context={
                **context,
                "accept_url": f"{base_url.rstrip('/')}/accept-invite?token={raw}",
            },
        )
    )
    logger.info(
        "workspace invitation sent",
        extra={"workspace_id": workspace.pk, "invitation_id": invitation.pk},
    )
    return invitation


#: Avatar colours for the member strip, cycled. Display only.
_MEMBER_COLOURS = ("#4A36A0", "#23918E", "#6B57C4", "#2E2170", "#1A7472")
_MEMBERS_SHOWN = 5


def _initials(text: str) -> str:
    words = [w for w in text.replace("@", " ").replace(".", " ").split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _invitation_context(invitation: Invitation, invited_by: Any) -> dict[str, Any]:
    """What the invitation design shows: who is asking, the brand, the people
    already in it, and exactly what the role will be allowed to do."""
    workspace = invitation.workspace
    memberships = Membership.objects.filter(workspace=workspace)
    shown = list(memberships.select_related("user").order_by("created_at")[:_MEMBERS_SHOWN])
    member_count = memberships.count()
    inviter_membership = memberships.filter(user=invited_by).first() if invited_by else None
    inviter_name = first_name_of(invited_by) if invited_by else workspace.name
    granted = resolved_permissions(invitation.role, invitation.permissions)
    brands = workspace.organization.workspaces.count() if workspace.organization_id else 1
    return {
        "email": invitation.email,
        "workspace_name": workspace.name,
        "workspace_initials": _initials(workspace.name),
        "inviter_name": inviter_name,
        "inviter_email": getattr(invited_by, "email", ""),
        "inviter_initials": _initials(inviter_name) if invited_by else "",
        "inviter_role": inviter_membership.get_role_display() if inviter_membership else "",
        "role": invitation.get_role_display(),
        "role_permissions": [p.label for p in Permission if p.value in granted],
        "members": [
            {
                "initials": _initials(m.user.email),
                "color": _MEMBER_COLOURS[i % len(_MEMBER_COLOURS)],
            }
            for i, m in enumerate(shown)
        ],
        "member_count": member_count,
        "more_members": max(0, member_count - _MEMBERS_SHOWN),
        "brands_text": f"{brands} brand{'' if brands == 1 else 's'}",
        "expires_on": invitation.expires_at.strftime("%d %b %Y"),
    }


def revoke(invitation: Invitation, *, actor: Any = None) -> Invitation:
    """Withdraws a pending invite (P5); its link reads as not valid from now."""
    from workspaces.services.approvals import log

    if invitation.revoked_at is None:
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at"])
        log(
            workspace=invitation.workspace,
            actor=actor,
            verb="invitation.revoked",
            target_repr=invitation.email,
        )
    return invitation


def preview(raw_token: str) -> tuple[Invitation | None, str]:
    """What the invite page shows as it opens (P6), before anything is accepted.

    Returns the invitation and its state: ``ok``, ``expired``, ``used`` or
    ``invalid`` (never existed, or revoked). Saying *why* a link is dead tells
    the reader to ask for a new one, and tells nothing to anyone who does not
    already hold the token. A revoked invite reads as invalid: the sender
    withdrew it, and "revoked" would invite an argument the page can't settle.
    """
    invitation = (
        Invitation.objects.select_related("workspace", "invited_by")
        .filter(token_hash=Invitation.hash_token(raw_token))
        .first()
    )
    if invitation is None or invitation.revoked_at is not None:
        return None, "invalid"
    if invitation.accepted_at is not None:
        return invitation, "used"
    if invitation.expires_at <= timezone.now():
        return invitation, "expired"
    return invitation, "ok"


def has_account(email: str) -> bool:
    return get_user_model().objects.filter(email__iexact=email).exists()


def inviter_name(invitation: Invitation) -> str:
    """The person who sent it, as the invitee would recognise them."""
    sender = invitation.invited_by
    if sender is None:
        return invitation.workspace.name
    return sender.get_full_name().strip() or sender.email


@transaction.atomic
def accept(*, raw_token: str, password: str | None = None, name: str = "") -> Membership:
    """Spends an invitation, creating the account if there is not one yet.

    One transaction covering account creation, both memberships and spending
    the token. Any other arrangement can leave a user who exists but belongs
    to nothing, or a spent invite that granted no access — and the second is
    unrecoverable without an admin.

    The organization membership is created alongside the workspace one because
    a collaborator who can see a brand but is invisible to the company above it
    would be missing from every org-level roster and quota.
    """
    invitation = (
        Invitation.objects.select_for_update()
        .filter(token_hash=Invitation.hash_token(raw_token))
        .first()
    )
    if invitation is None or not invitation.is_usable:
        raise InvitationInvalidError("This invitation is no longer valid.")

    user_model = get_user_model()
    user = user_model.objects.filter(email__iexact=invitation.email).first()
    if user is not None:
        # P6: holding the email is not holding the account. An address that
        # already has one signs in to join, or a forwarded invite would be
        # enough to attach somebody else's account to this workspace.
        if not password or not user.check_password(password):
            raise InvitationWrongPasswordError("Wrong email or password.")
    else:
        if not password:
            raise InvitationNeedsPasswordError(
                "This invitation is for a new account; choose a password to accept it."
            )
        problems = password_problems(
            password, user=user_model(email=invitation.email, first_name=name)
        )
        if problems:
            raise InvitationWeakPasswordError(problems)
        user = user_model.objects.create_user(
            email=invitation.email, password=password, first_name=name.strip()[:150]
        )
        # Accepting an emailed invitation proves control of the address as
        # convincingly as a verification link does, so asking for a second one
        # would be ceremony.
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])

    membership, _ = Membership.objects.get_or_create(
        user=user,
        workspace=invitation.workspace,
        defaults={
            "role": invitation.role,
            "permissions": invitation.permissions,
            "invited_by": invitation.invited_by,
        },
    )

    organization = invitation.workspace.organization
    if organization is not None:
        OrganizationMembership.objects.get_or_create(
            organization=organization,
            user=user,
            defaults={
                "role": invitation.role if invitation.role != Role.OWNER else Role.ADMIN,
                "invited_by": invitation.invited_by,
            },
        )

    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    return membership


class InvitationInvalidError(Exception):
    """Expired, revoked, already accepted, or never existed — all one answer
    to the caller. Distinguishing them would tell an attacker which tokens are
    real."""


class InvitationNeedsPasswordError(Exception):
    """The invited address has no account yet, so accepting has to create one."""


class InvitationWrongPasswordError(Exception):
    """The invited address has an account, and this is not its password."""


class InvitationWeakPasswordError(Exception):
    """The new account's password fails the validators; carries their messages."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__(" ".join(messages))
        self.messages = messages
