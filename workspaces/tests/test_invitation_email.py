"""The invitation email: what it says, and that it actually renders.

Before this, `invite()` named its template `email/workspace_invitation.txt`
while the sender already prefixes `email/` and appends `.txt`, so a real SMTP
send raised `TemplateDoesNotExist`. The fake outbox records without rendering,
which is why only the second test below would have caught it.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from workspaces.models import Role
from workspaces.services import invitations

pytestmark = pytest.mark.django_db


def _invite(workspace: Any, user: Any) -> None:
    invitations.invite(
        workspace=workspace,
        email="newcomer@example.com",
        role=Role.EDITOR,
        invited_by=user,
        base_url="https://app.example.com",
    )


def test_the_invitation_names_who_sent_it_and_what_the_role_allows(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    _invite(workspace, user)

    email = outbox[-1]
    assert email.to == "newcomer@example.com"
    assert email.template == "workspace_invitation"
    context = email.context
    assert context["accept_url"].startswith("https://app.example.com/accept-invite?token=")
    assert context["workspace_name"] == workspace.name
    assert context["inviter_email"] == user.email
    assert context["inviter_name"]
    assert context["role"] == "Editor"
    assert "Create and edit content" in context["role_permissions"]
    assert "Approve content" not in context["role_permissions"]
    assert context["member_count"] == 1
    assert context["expires_on"]


@override_settings(USE_FAKE_MAIL=False)
def test_the_invitation_renders_through_the_real_sender(workspace: Any, user: Any) -> None:
    mail.outbox = []

    _invite(workspace, user)

    message = mail.outbox[-1]
    assert isinstance(message, EmailMultiAlternatives)
    assert "accept-invite?token=" in message.body
    html = str(message.alternatives[0][0])
    assert "accept-invite?token=" in html
    assert workspace.name in html
