"""The real mail sender: text always, HTML beside it when a design exists.

These go through `DjangoMailSender` into Django's locmem backend rather than
the fake, because the fake records the `Email` and never renders it — which is
how an invitation naming a template that does not exist shipped unnoticed.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core import mail
from django.test import override_settings

from common.mail import DjangoMailSender, Email

pytestmark = pytest.mark.django_db


@pytest.fixture
def sent() -> list[Any]:
    mail.outbox = []
    return mail.outbox


@override_settings(SITE_URL="https://app.example.com")
def test_a_template_with_a_design_is_sent_as_text_and_html(sent: list[Any]) -> None:
    DjangoMailSender().send(
        Email(
            to="ines@example.com",
            subject="Your ComposeVision code: 123456",
            template="verify_email",
            context={
                "code": "123456",
                "verify_url": "https://app.example.com/verify-email?token=abc",
                "expires_minutes": 15,
                "email": "ines@example.com",
                "first_name": "Ines",
            },
        )
    )

    message = sent[0]
    assert "123456" in message.body
    html, mimetype = message.alternatives[0]
    assert mimetype == "text/html"
    assert "123456" in html
    assert "https://app.example.com/verify-email?token=abc" in html
    # Every link in the design resolves against SITE_URL, never a placeholder.
    assert "{{" not in html and "{%" not in html
    assert "https://app.example.com/" in html


def test_a_template_without_a_design_is_text_only(sent: list[Any]) -> None:
    DjangoMailSender().send(
        Email(
            to="ines@example.com",
            subject="Reset your password",
            template="reset_password",
            context={"reset_url": "https://x/reset", "email": "ines@example.com"},
        )
    )

    assert sent[0].alternatives == []


@override_settings(DEFAULT_FROM_EMAIL="ComposeVision <hello@attractvision.com>")
def test_mail_is_sent_from_the_configured_address(sent: list[Any]) -> None:
    DjangoMailSender().send(
        Email(to="a@example.com", subject="s", template="reset_password", context={})
    )

    assert sent[0].from_email == "ComposeVision <hello@attractvision.com>"


@pytest.mark.parametrize(
    ("template", "context"),
    [
        (
            "welcome",
            {
                "first_name": "Ines",
                "email": "ines@example.com",
                "workspace_name": "Saveurs du Sud",
                "plan_name": "Pro",
                "credits": 150,
                "app_url": "https://x/app",
            },
        ),
        (
            "workspace_invitation",
            {
                "email": "sam@example.com",
                "workspace_name": "Saveurs du Sud",
                "workspace_initials": "SS",
                "inviter_name": "Ines",
                "inviter_email": "ines@example.com",
                "inviter_initials": "IN",
                "inviter_role": "Owner",
                "role": "Editor",
                "role_permissions": ["View", "Comment", "Edit"],
                "members": [{"initials": "IN", "color": "#4A36A0"}],
                "member_count": 1,
                "more_members": 0,
                "brands_text": "1 brand",
                "expires_on": "25 Oct 2026",
                "accept_url": "https://x/accept-invite?token=t",
            },
        ),
        (
            "post_heads_up",
            {
                "first_name": "Ines",
                "workspace_name": "Saveurs du Sud",
                "countdown": "2 hours",
                "lead_time": "2 hours",
                "slot_day": "Mon 12 Oct",
                "slot_time": "14:00",
                "timezone": "Africa/Tunis",
                "post_id": 7,
                "post_title": "First harvest",
                "caption_excerpt": "First harvest, pressed the same day.",
                "platforms": ["Instagram"],
                "format": "Feed",
                "auto_publish": True,
                "post_url": "https://x/app/posts/7",
            },
        ),
    ],
)
def test_every_design_renders_without_leftover_tags(
    sent: list[Any], template: str, context: dict[str, Any]
) -> None:
    DjangoMailSender().send(
        Email(to="a@example.com", subject="s", template=template, context=context)
    )

    html = sent[0].alternatives[0][0]
    assert "{{" not in html and "{%" not in html
    assert context["workspace_name"] in html
