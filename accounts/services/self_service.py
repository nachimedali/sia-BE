"""The person's own account (P2): language, password, email, export, deletion.

Kept apart from `auth.py` (sign-up, sign-in, recovery) because everything here
starts from a signed-in person acting on themselves, and each change that
matters asks for proof first — the current password, or a typed phrase.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import DataExport, DataExportState, EmailToken, EmailTokenPurpose, User
from accounts.passwords import password_problems
from accounts.services.auth import app_url, queue_email
from common.exceptions import OCCSError, StateConflict
from common.mail import Email, get_mail_sender


class WrongPasswordError(OCCSError):
    default_code = "wrong_password"
    default_detail = "That is not your current password."


class WeakPasswordError(OCCSError):
    default_code = "invalid"
    default_detail = "Choose a stronger password."


class EmailTakenError(OCCSError):
    default_code = "email_taken"
    default_detail = "Another account already uses that email."


class SameEmailError(OCCSError):
    default_code = "same_email"
    default_detail = "That is already your email."


class InvalidLinkError(OCCSError):
    default_code = "invalid_token"
    default_detail = "This link is invalid, expired or was cancelled."


class ExportInProgressError(StateConflict):
    default_code = "export_in_progress"
    default_detail = "An export is already being prepared."


class ConfirmationMismatchError(OCCSError):
    default_code = "confirmation_mismatch"
    default_detail = "Type DELETE followed by your email, exactly."


class SoleOwnerError(StateConflict):
    default_code = "sole_owner"
    default_detail = "You are the only owner of an organization other people work in."


class ActiveSubscriptionError(StateConflict):
    default_code = "active_subscription"
    default_detail = "Cancel your paid plan in Credits & plan before deleting your account."


# -----------------------------------------------------------------------------
# Password
# -----------------------------------------------------------------------------
@transaction.atomic
def change_password(user: User, *, current: str, new: str) -> None:
    """Sets a new password and signs out every other session.

    Every outstanding refresh token is blacklisted, so other devices fall out
    when their short access token next expires. The caller's own session goes
    too; the frontend signs straight back in with the new password.
    """
    if not user.check_password(current):
        raise WrongPasswordError(
            detail={"fields": {"current": [str(WrongPasswordError.default_detail)]}}
        )
    if current == new:
        raise WeakPasswordError(
            "Choose a password you have not used here.",
            detail={"fields": {"new": ["Choose a password you have not used here."]}},
        )
    problems = password_problems(new, user=user)
    if problems:
        raise WeakPasswordError(" ".join(problems), detail={"fields": {"new": problems}})
    user.set_password(new)
    user.save(update_fields=["password", "updated_at"])
    _sign_out_everywhere(user)


def _sign_out_everywhere(user: User) -> None:
    BlacklistedToken.objects.bulk_create(
        [
            BlacklistedToken(token=token)
            for token in OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
        ],
        ignore_conflicts=True,
    )


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------
def request_email_change(user: User, *, email: str, password: str) -> User:
    """Sends a confirmation link to the new address. Nothing moves until it is
    clicked: a typo here must not lock the person out of their own account."""
    normalised = email.strip().lower()
    if not user.check_password(password):
        raise WrongPasswordError(
            "Enter your current password to change your email.",
            detail={"fields": {"password": ["Enter your current password to change your email."]}},
        )
    if normalised == user.email.lower():
        raise SameEmailError(detail={"fields": {"email": [str(SameEmailError.default_detail)]}})
    if User.objects.filter(email__iexact=normalised).exclude(pk=user.pk).exists():
        raise EmailTakenError(detail={"fields": {"email": [str(EmailTakenError.default_detail)]}})

    user.pending_email = normalised
    user.save(update_fields=["pending_email", "updated_at"])
    _token, raw = EmailToken.issue(user, EmailTokenPurpose.EMAIL_CHANGE)
    queue_email(
        to=normalised,
        subject="Confirm your new email",
        template="email_change",
        context={
            "old_email": user.email,
            "new_email": normalised,
            "confirm_url": app_url(f"/confirm-email?token={raw}"),
        },
    )
    return user


def cancel_email_change(user: User) -> User:
    EmailToken._supersede(user, EmailTokenPurpose.EMAIL_CHANGE)
    user.pending_email = ""
    user.save(update_fields=["pending_email", "updated_at"])
    return user


@transaction.atomic
def confirm_email_change(raw: str) -> User:
    token = EmailToken.consume(raw, EmailTokenPurpose.EMAIL_CHANGE)
    if token is None:
        raise InvalidLinkError()
    user: User = User.objects.select_for_update().get(pk=token.user_id)
    if not user.pending_email:
        raise InvalidLinkError()
    if User.objects.filter(email__iexact=user.pending_email).exclude(pk=user.pk).exists():
        raise EmailTakenError()
    user.email = user.pending_email
    user.pending_email = ""
    user.is_email_verified = True
    user.save(update_fields=["email", "pending_email", "is_email_verified", "updated_at"])
    return user


def pending_email(user: User) -> dict[str, Any] | None:
    if not user.pending_email:
        return None
    token = (
        EmailToken.objects.filter(
            user=user, purpose=EmailTokenPurpose.EMAIL_CHANGE, used_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )
    if token is None or not token.is_usable:
        return None
    return {"email": user.pending_email, "expires_at": token.expires_at}


# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------
def request_export(user: User) -> DataExport:
    """Queues a copy of everything linked to this person. One at a time."""
    from accounts.tasks import build_data_export

    if DataExport.objects.filter(user=user, state=DataExportState.PREPARING).exists():
        raise ExportInProgressError()
    export = DataExport.objects.create(user=user, sent_to=user.email)
    transaction.on_commit(lambda: build_data_export.delay(export.pk))
    return export


def export_payload(user: User) -> dict[str, Any]:
    """Everything linked to this person, and nothing about anyone else: their
    profile, memberships, the posts they wrote, their comments, the approval
    decisions they made, and their entries in the audit log."""
    from collaboration.models import Comment
    from content.models import Post
    from workspaces.models import ApprovalAction, AuditLog, Membership

    def when(value: Any) -> str | None:
        return value.isoformat() if value else None

    return {
        "generated_at": timezone.now().isoformat(),
        "profile": {
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "language": user.language,
            "created_at": when(user.created_at),
        },
        "memberships": [
            {
                "workspace": m.workspace.name,
                "role": m.role,
                "permissions": m.permissions,
                "since": when(m.created_at),
            }
            for m in Membership.objects.filter(user=user).select_related("workspace")
        ],
        "posts": [
            {
                "id": p.pk,
                "workspace": p.workspace.name,
                "status": p.status,
                "body": p.master_body,
                "created_at": when(p.created_at),
                "scheduled_at": when(p.scheduled_at),
            }
            for p in Post.objects.filter(author=user).select_related("workspace")
        ],
        "comments": [
            {"id": c.pk, "thread": c.thread.title, "body": c.body, "created_at": when(c.created_at)}
            for c in Comment.objects.filter(author=user).select_related("thread")
        ],
        "approvals": [
            {"post": a.post_id, "action": a.action, "note": a.note, "at": when(a.created_at)}
            for a in ApprovalAction.objects.filter(actor=user)
        ],
        "activity": [
            {"workspace": e.workspace.name, "verb": e.verb, "at": when(e.created_at)}
            for e in AuditLog.objects.filter(actor=user).select_related("workspace")
        ],
    }


def build_export(export: DataExport) -> DataExport:
    data = json.dumps(export_payload(export.user), indent=2, ensure_ascii=False)
    export.file.save(f"{uuid.uuid4().hex}.json", ContentFile(data.encode()), save=False)
    export.state = DataExportState.EMAILED
    export.ready_at = timezone.now()
    export.save(update_fields=["file", "state", "ready_at"])
    get_mail_sender().send(
        Email(
            to=export.sent_to,
            subject="Your ComposeVision data is ready",
            template="data_export",
            context={
                "download_url": app_url(f"/app/settings/account?export={export.pk}#data"),
                "days": DataExport.LINK_TTL.days,
            },
        )
    )
    return export


def downloadable(user: User, export_id: int) -> DataExport | None:
    """An export this person may still download: theirs, built, within its
    7 days. Anything else is a 404 to the caller."""
    export = DataExport.objects.filter(
        pk=export_id, user=user, state=DataExportState.EMAILED
    ).first()
    if (
        export is None
        or not export.file
        or export.requested_at < timezone.now() - DataExport.LINK_TTL
    ):
        return None
    return export


def recent_exports(user: User) -> list[DataExport]:
    return list(DataExport.objects.filter(user=user)[:3])


# -----------------------------------------------------------------------------
# Deletion
# -----------------------------------------------------------------------------
def sole_owner_of(user: User) -> list[str]:
    """Organizations this person owns that other people work in: deleting
    them would leave those people with no owner."""
    from django.db.models import Exists, OuterRef

    from workspaces.models import Membership, Organization, OrganizationMembership

    org_others = OrganizationMembership.objects.filter(organization=OuterRef("pk")).exclude(
        user=user
    )
    brand_others = Membership.objects.filter(workspace__organization=OuterRef("pk")).exclude(
        user=user
    )
    return list(
        Organization.objects.filter(owner=user)
        .filter(Exists(org_others) | Exists(brand_others))
        .values_list("name", flat=True)
    )


def _paid_plan_running(user: User) -> bool:
    from billing.models import Subscription, SubscriptionStatus

    return Subscription.objects.filter(
        workspace__organization__owner=user, status__in=SubscriptionStatus.live()
    ).exists()


@transaction.atomic
def delete_account(user: User, *, confirm: str) -> None:
    """Removes the person: their details, their sign-in, their memberships.

    Refused while they are the only owner of an organization other people
    work in (ownership must move first), and while a paid plan is running (it
    would go on charging with nobody able to stop it). What they made stays
    with the workspaces it belongs to, attributed to a former member: the row
    is kept, anonymised and deactivated, rather than deleted, so the brand's
    posts, approvals and audit trail keep their history.
    """
    from workspaces.models import Membership, OrganizationMembership
    from workspaces.services.approvals import log

    if confirm.strip() != f"DELETE {user.email}":
        raise ConfirmationMismatchError(
            detail={"fields": {"confirm": [str(ConfirmationMismatchError.default_detail)]}}
        )
    sole = sole_owner_of(user)
    if sole:
        raise SoleOwnerError(
            f"You are the only owner of {', '.join(sole)}. Make someone else an owner there first.",
            detail={"organizations": sole},
        )
    if _paid_plan_running(user):
        raise ActiveSubscriptionError()

    for membership in Membership.objects.filter(user=user).select_related("workspace"):
        log(
            workspace=membership.workspace,
            actor=None,
            verb="member.account_deleted",
            target_repr="Former member",
        )
    Membership.objects.filter(user=user).delete()
    OrganizationMembership.objects.filter(user=user).delete()
    EmailToken.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
    _sign_out_everywhere(user)

    user.email = f"deleted-{user.pk}-{uuid.uuid4().hex[:8]}@deleted.invalid"
    user.first_name, user.last_name = "Former", "member"
    user.pending_email = ""
    user.is_active = False
    user.is_staff = False
    user.set_unusable_password()
    user.save()
