"""Auth flows (implementation.md Phase 2.2, 2.3).

Business logic lives here; views parse and serialise (implementation.md §4.1).
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction

from accounts.models import EmailToken, EmailTokenPurpose, User
from accounts.tasks import send_templated_email
from billing.models import UNLIMITED
from billing.services.flags import TRANSACTIONAL_EMAIL, flag_enabled
from common.mail import first_name_of
from workspaces.models import Membership
from workspaces.services.provisioning import provision_workspace

logger = logging.getLogger(__name__)


def app_url(path: str) -> str:
    base = getattr(settings, "SITE_URL", "http://localhost:3000").rstrip("/")
    return f"{base}{path}"


def queue_email(to: str, subject: str, template: str, context: dict[str, Any]) -> None:
    """Always queued, never inline (implementation.md §4.3, Phase 2.3).

    An SMTP timeout must not become a failed registration.
    """
    send_templated_email.delay(to=to, subject=subject, template=template, context=context)


def _first_membership(user: User) -> Membership | None:
    """The workspace the account signed up into — the one account-level mail
    (the rollout flag, the welcome's plan figures) is read against, so both
    always describe the same workspace."""
    return (
        Membership.objects.filter(user=user)
        .select_related("workspace__organization")
        .order_by("created_at")
        .first()
    )


def transactional_email_enabled(user: User) -> bool:
    membership = _first_membership(user)
    return flag_enabled(
        membership.workspace.organization if membership else None, TRANSACTIONAL_EMAIL
    )


def send_verification_email(user: User) -> None:
    """The link always; the six-digit code beside it while the flag is on.

    Flag off sends the link-only email exactly as before the code existed.
    """
    _, raw = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    verify_url = app_url(f"/verify-email?token={raw}")
    if not transactional_email_enabled(user):
        queue_email(
            to=user.email,
            subject="Confirm your email",
            template="verify_email_link",
            context={"verify_url": verify_url, "email": user.email},
        )
        return

    token, code = EmailToken.issue_code(user)
    queue_email(
        to=user.email,
        subject=f"Your ComposeVision code: {code}",
        template="verify_email",
        context={
            "code": code,
            "verify_url": verify_url,
            "email": user.email,
            "first_name": first_name_of(user),
            "expires_minutes": int(
                EmailToken.DEFAULT_TTL[EmailTokenPurpose.VERIFY_CODE].total_seconds() // 60
            ),
            "requested_at": token.created_at.strftime("%d %b %Y, %H:%M UTC"),
        },
    )


def send_welcome_email(user: User) -> None:
    """Once, when the address is first proven — never at registration, so
    nobody is welcomed to an account at an address they do not own."""
    membership = _first_membership(user)
    if membership is None:
        return
    from billing.models import Subscription, SubscriptionStatus
    from billing.services.entitlements import entitlements_for

    workspace = membership.workspace
    organization = workspace.organization
    entitlements = entitlements_for(workspace)
    credits = entitlements.credits_remaining()
    trial_ends = getattr(organization, "trial_ends_at", None)
    # The same rule billing uses: the organization's newest live subscription.
    subscription = (
        Subscription.objects.filter(
            workspace__organization=organization, status__in=SubscriptionStatus.live()
        )
        .order_by("-created_at")
        .first()
    )
    renews_on = subscription.period_end if subscription else None
    first_name = first_name_of(user)
    queue_email(
        to=user.email,
        subject=f"Welcome to ComposeVision, {first_name}",
        template="welcome",
        context={
            "first_name": first_name,
            "email": user.email,
            "workspace_name": workspace.name,
            "plan_name": entitlements.plan.display_name,
            "credits": "Unlimited" if credits == UNLIMITED else credits,
            "trial_ends": trial_ends.strftime("%d %b %Y") if trial_ends else "",
            "renews_on": renews_on.strftime("%d %b %Y") if renews_on else "",
        },
    )


def _mark_verified(user: User) -> None:
    """The one place an address becomes verified by proof of the inbox, so the
    welcome cannot be sent twice or by a path that forgot it."""
    if user.is_email_verified:
        return
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "updated_at"])
    if transactional_email_enabled(user):
        transaction.on_commit(lambda: send_welcome_email(user))


@transaction.atomic
def register(email: str, password: str, workspace_name: str | None = None) -> tuple[User, Any]:
    """Creates the user, its workspace and OWNER membership, assigns Free, and
    queues verification. All or nothing."""
    user = User.objects.create_user(email=email, password=password)
    workspace = provision_workspace(user, name=workspace_name)

    # after_commit: the task must not run against a transaction that may still
    # roll back, or a user could receive a link to an account that never existed.
    transaction.on_commit(lambda: send_verification_email(user))

    logger.info("user registered", extra={"user_id": user.pk, "workspace_id": workspace.pk})
    return user, workspace


def verify_email(raw_token: str) -> bool:
    token = EmailToken.consume(raw_token, EmailTokenPurpose.VERIFY)
    if token is None:
        return False

    _mark_verified(token.user)
    return True


def verify_code(user: User, code: str) -> bool:
    """Onboarding step 1's path. An account already verified answers `True`
    without spending anything: the question is answered either way."""
    if user.is_email_verified:
        return True
    if not EmailToken.consume_code(user, code):
        return False
    _mark_verified(user)
    return True


def resend_verification(user: User) -> None:
    if user.is_email_verified:
        # Not an error: re-sending to an already-verified address would be a
        # free way to have us mail someone repeatedly.
        return
    send_verification_email(user)


def request_password_reset(email: str) -> None:
    """Always succeeds from the caller's point of view.

    Revealing whether an address exists turns this into an account-enumeration
    oracle, so the endpoint's response must not depend on the lookup.
    """
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        logger.info("password reset requested for unknown address")
        return

    _, raw = EmailToken.issue(user, EmailTokenPurpose.RESET)
    queue_email(
        to=user.email,
        subject="Reset your password",
        template="reset_password",
        context={"reset_url": app_url(f"/reset-password?token={raw}"), "email": user.email},
    )


def confirm_password_reset(raw_token: str, new_password: str) -> bool:
    token = EmailToken.consume(raw_token, EmailTokenPurpose.RESET)
    if token is None:
        return False

    user = token.user
    user.set_password(new_password)

    # Completing a reset from the inbox proves control of the address, so it is
    # also the moment to treat it as verified.
    fields = ["password"]
    if not user.is_email_verified:
        user.is_email_verified = True
        fields.append("is_email_verified")
    user.save(update_fields=[*fields, "updated_at"])
    return True
