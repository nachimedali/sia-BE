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

from common.mail import Email, get_mail_sender
from workspaces.models import Invitation, Membership, OrganizationMembership, Role, Workspace

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

    get_mail_sender().send(
        Email(
            to=invitation.email,
            subject=f"You have been invited to {workspace.name}",
            template="email/workspace_invitation.txt",
            context={
                "workspace_name": workspace.name,
                "inviter": getattr(invited_by, "email", ""),
                "accept_url": f"{base_url.rstrip('/')}/accept-invite?token={raw}",
            },
        )
    )
    logger.info(
        "workspace invitation sent",
        extra={"workspace_id": workspace.pk, "invitation_id": invitation.pk},
    )
    return invitation


@transaction.atomic
def accept(*, raw_token: str, password: str | None = None) -> Membership:
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
    if user is None:
        if not password:
            raise InvitationNeedsPasswordError(
                "This invitation is for a new account; choose a password to accept it."
            )
        user = user_model.objects.create_user(email=invitation.email, password=password)
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
