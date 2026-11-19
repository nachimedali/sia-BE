"""X-01 — every email is written in its reader's language (L-6).

English keeps its designed layouts. French and Italian have their own words —
`templates/email/<fr|it>/<template>.txt` and a subject in
`common.mail_i18n.SUBJECTS` — inside the shared branded frame.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.test import override_settings

from accounts.models import User
from accounts.services.auth import request_password_reset
from common.mail import DjangoMailSender, Email, render
from common.mail_i18n import EVENTS, SUBJECTS, TRANSLATED, notification_subject

pytestmark = pytest.mark.django_db

EMAILS = Path(settings.BASE_DIR) / "templates" / "email"
TEMPLATES = sorted(path.stem for path in EMAILS.glob("*.txt"))
FIELDS = re.compile(r"\{(\w+)\}")


@pytest.mark.parametrize("language", TRANSLATED)
@pytest.mark.parametrize("template", TEMPLATES)
def test_every_email_has_its_words_and_subject_in_each_language(
    template: str, language: str
) -> None:
    assert (EMAILS / language / f"{template}.txt").exists()
    entry = SUBJECTS[template][language]
    assert (entry({"needs_approval": True}) if callable(entry) else entry).strip()


@pytest.mark.parametrize("template", TEMPLATES)
def test_french_and_italian_subjects_ask_for_the_same_fields(template: str) -> None:
    entries = {language: SUBJECTS[template][language] for language in TRANSLATED}
    fields = {
        language: set() if callable(entry) else set(FIELDS.findall(entry))
        for language, entry in entries.items()
    }
    assert fields["fr"] == fields["it"]


@pytest.mark.parametrize("language", TRANSLATED)
@pytest.mark.parametrize("template", TEMPLATES)
@override_settings(SITE_URL="https://site.test")
def test_every_translated_email_renders_in_the_branded_frame(template: str, language: str) -> None:
    subject, text, html = render(
        Email(to="a@example.com", subject="s", template=template, context={}, language=language)
    )
    assert isinstance(subject, str) and text.strip()
    assert f'<html lang="{language}">' in html
    assert 'src="cid:composevision-logo"' in html
    assert "{{" not in text + html and "{%" not in text + html


@override_settings(SITE_URL="https://app.example.com")
def test_a_french_verification_email_is_french_from_subject_to_footer() -> None:
    mail.outbox = []
    DjangoMailSender().send(
        Email(
            to="ines@example.com",
            subject="Your ComposeVision code: 123456",
            template="verify_email",
            context={
                "code": "123456",
                "verify_url": "https://app.example.com/v",
                "first_name": "Ines",
            },
            language="fr",
        )
    )
    message: Any = mail.outbox[0]
    assert message.subject == "Votre code ComposeVision : 123456"
    assert "Bonjour Ines" in message.body and "https://app.example.com/v" in message.body
    html = message.alternatives[0][0]
    assert "Centre d\u2019aide" in html and "Help centre" not in html


def test_english_and_an_unknown_language_keep_the_callers_subject_and_design() -> None:
    for language in ("en", "de"):
        subject, _text, html = render(
            Email(
                to="a@example.com",
                subject="Reset your password",
                template="reset_password",
                context={},
                language=language,
            )
        )
        assert subject == "Reset your password"
        assert '<html lang="en">' in html


def test_a_reset_link_goes_out_in_the_account_language(outbox: list[Any]) -> None:
    User.objects.create_user(email="lucia@example.com", password="x" * 12, language="it")

    request_password_reset("lucia@example.com")

    assert outbox[0].language == "it"
    assert outbox[0].subject == "Reimposta la password"


def test_registration_records_the_language_the_page_was_read_in(plans: Any) -> None:
    from rest_framework.test import APIClient

    response = APIClient().post(
        "/api/v1/auth/register/",
        {"email": "anne@example.com", "password": "a-long-passphrase-9", "workspace_name": "Anne"},
        format="json",
        HTTP_ACCEPT_LANGUAGE="fr",
    )
    assert response.status_code == 201, response.json()
    assert User.objects.get(email="anne@example.com").language == "fr"


def test_a_notification_line_is_written_in_each_readers_language() -> None:
    from notifications.services import _in_language

    payload = {"title": "Harvest week", "subject": "A post was approved: Harvest week"}
    assert (
        notification_subject("fr", "post.approved", payload)
        == "Une publication a été validée : Harvest week"
    )
    assert (
        notification_subject("it", "credits.low", {"balance": 12})
        == "I crediti stanno per finire: 12 crediti rimasti"
    )
    assert _in_language(payload, "post.approved", "en") is payload
    assert _in_language(payload, "post.approved", "it")["subject"].startswith("Un post è stato")


def test_every_notification_event_has_a_line_in_each_language() -> None:
    from notifications.models import EventKey

    for language in TRANSLATED:
        assert set(EVENTS[language]) == set(EventKey.values)


def test_the_heads_up_counts_down_in_the_readers_language() -> None:
    from datetime import timedelta

    from scheduling.heads_up import duration

    assert duration(timedelta(hours=2)) == "2 hours"
    assert duration(timedelta(hours=1, minutes=50), "fr") == "1 heure 50 minutes"
    assert duration(timedelta(days=2), "it") == "2 giorni"
    assert duration(timedelta(seconds=10), "it") == "meno di un minuto"
