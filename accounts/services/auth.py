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
from workspaces.services.provisioning import provision_workspace

logger = logging.getLogger(__name__)


def _app_url(path: str) -> str:
    base = getattr(settings, "SITE_URL", "http://localhost:3000").rstrip("/")
    return f"{base}{path}"


def _queue(to: str, subject: str, template: str, context: dict[str, str]) -> None:
    """Always queued, never inline (implementation.md §4.3, Phase 2.3).

    An SMTP timeout must not become a failed registration.
    """
    send_templated_email.delay(to=to, subject=subject, template=template, context=context)


def send_verification_email(user: User) -> None:
    _, raw = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    _queue(
        to=user.email,
        subject="Confirm your email",
        template="verify_email",
        context={"verify_url": _app_url(f"/verify-email?token={raw}"), "email": user.email},
    )


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

    user = token.user
    if not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "updated_at"])
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
    _queue(
        to=user.email,
        subject="Reset your password",
        template="reset_password",
        context={"reset_url": _app_url(f"/reset-password?token={raw}"), "email": user.email},
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
