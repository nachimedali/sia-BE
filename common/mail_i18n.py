"""The words the app sends out, in French and Italian (X-01, L-6).

English stays where it always was — each sender's own subject, the
`templates/email/*.txt` bodies and their designs. French and Italian bodies are
`templates/email/<language>/<template>.txt`; their subjects, the branded
frame's footer and each notification's one line are here. A subject reads its
fields from the email's context (`{code}`); a missing field reads blank.

The FR/IT words await a native speaker's review (X-01).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

TRANSLATED = ("fr", "it")

#: The shared frame's own words (`email/_layout.html`).
FRAME: dict[str, dict[str, str]] = {
    "en": {"help_label": "Help centre", "security_label": "Security"},
    "fr": {"help_label": "Centre d\u2019aide", "security_label": "Sécurité"},
    "it": {"help_label": "Centro assistenza", "security_label": "Sicurezza"},
}

Subject = str | Callable[[dict[str, Any]], str]

SUBJECTS: dict[str, dict[str, Subject]] = {
    "data_export": {
        "fr": "Vos données ComposeVision sont prêtes",
        "it": "I tuoi dati ComposeVision sono pronti",
    },
    "email_change": {
        "fr": "Confirmez votre nouvelle adresse e-mail",
        "it": "Conferma il tuo nuovo indirizzo email",
    },
    # Already in the reader's language: `notify` writes it per recipient.
    "notification": {"fr": "{subject}", "it": "{subject}"},
    "post_heads_up": {
        "fr": "En ligne dans {countdown} : {post_title}",
        "it": "Online tra {countdown}: {post_title}",
    },
    "publish_blocked": {
        "fr": "Une publication programmée n'a pas pu être publiée",
        "it": "Un post programmato non è stato pubblicato",
    },
    "publish_failed": {
        "fr": "Une publication programmée n'a pas pu être publiée",
        "it": "Un post programmato non è stato pubblicato",
    },
    "reminder_packet": {"fr": "C'est l'heure de publier", "it": "È ora di pubblicare"},
    "report_share": {"fr": "Votre rapport : {report_name}", "it": "Il tuo report: {report_name}"},
    "reset_password": {"fr": "Réinitialisez votre mot de passe", "it": "Reimposta la password"},
    "review_link": {
        "fr": lambda context: (
            "Une publication attend votre validation"
            if context.get("needs_approval")
            else "Une publication a été partagée avec vous"
        ),
        "it": lambda context: (
            "Un post è in attesa della tua approvazione"
            if context.get("needs_approval")
            else "Un post è stato condiviso con te"
        ),
    },
    "verify_email": {
        "fr": "Votre code ComposeVision : {code}",
        "it": "Il tuo codice ComposeVision: {code}",
    },
    "verify_email_link": {
        "fr": "Confirmez votre adresse e-mail",
        "it": "Conferma il tuo indirizzo email",
    },
    "welcome": {
        "fr": "Bienvenue sur ComposeVision, {first_name}",
        "it": "Ti diamo il benvenuto su ComposeVision, {first_name}",
    },
    "workspace_invitation": {
        "fr": "{inviter_name} vous invite à rejoindre {workspace_name}",
        "it": "{inviter_name} ti invita in {workspace_name}",
    },
}

#: "Unlimited" credits in the welcome email.
UNLIMITED = {"en": "Unlimited", "fr": "Illimités", "it": "Illimitati"}

#: `notifications.models.EventKey` labels.
EVENTS: dict[str, dict[str, str]] = {
    "fr": {
        "post.submitted": "Une publication a été soumise pour validation",
        "post.approved": "Une publication a été validée",
        "post.changes_requested": "Des modifications ont été demandées",
        "post.rejected": "Une publication a été refusée",
        "thread.assigned": "Une discussion vous a été attribuée",
        "thread.commented": "Quelqu'un a répondu dans une discussion",
        "account.reconnect": "Un compte connecté doit être reconnecté",
        "template.slot_skipped": "Un créneau de modèle a été sauté",
        "template.slot_at_risk": "Un créneau de modèle ne partira pas",
        "credits.low": "Vos crédits s'épuisent",
        "billing.renewal_due": "Votre offre doit être renouvelée",
    },
    "it": {
        "post.submitted": "Un post è stato inviato per la revisione",
        "post.approved": "Un post è stato approvato",
        "post.changes_requested": "Sono state richieste modifiche",
        "post.rejected": "Un post è stato rifiutato",
        "thread.assigned": "Ti è stata assegnata una discussione",
        "thread.commented": "Qualcuno ha risposto in una discussione",
        "account.reconnect": "Un account collegato va ricollegato",
        "template.slot_skipped": "Uno slot di un modello è stato saltato",
        "template.slot_at_risk": "Uno slot di un modello non partirà",
        "credits.low": "I crediti stanno per finire",
        "billing.renewal_due": "Il tuo piano va rinnovato",
    },
}
_CREDITS_LEFT = {"fr": "{n} crédits restants", "it": "{n} crediti rimasti"}
_PLAN_ENDS = {"fr": "votre offre se termine le {date}", "it": "il tuo piano scade il {date}"}
_COLON = {"fr": " : ", "it": ": "}


def notification_subject(language: str, event_key: str, payload: dict[str, Any]) -> str | None:
    """One notification's line in `language`, or None to keep the event site's
    English words (English, or an event with no translation yet)."""
    label = EVENTS.get(language, {}).get(event_key)
    if label is None:
        return None
    if event_key == "credits.low":
        detail = _CREDITS_LEFT[language].format(n=payload.get("balance", ""))
    elif event_key == "billing.renewal_due":
        detail = _PLAN_ENDS[language].format(date=payload.get("ends_at", ""))
    else:
        detail = str(payload.get("title") or "")
    return f"{label}{_COLON[language]}{detail}" if detail else label


def day(value: Any, language: str, fmt: str = "d M Y") -> str:
    """A date in the reader's language: "05 Nov 2026", "05 nov. 2026". The
    English output is what `strftime("%d %b %Y")` gave."""
    from django.utils import formats, translation

    with translation.override(language):
        return str(formats.date_format(value, fmt))
