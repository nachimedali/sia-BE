"""X-01: French and Italian words for plans and packs (L-6).

`translations` holds `{"fr": {"tagline", "support_response"}, "it": {…}}`;
English stays the row's own fields. The seeded rows get their words here, and
only while their English is still the seeded English — a row an operator has
retold is left for the operator, and a row that already has translations is
never touched (an admin edit is never overwritten).
"""

from typing import Any

from django.db import migrations, models

#: code → the English the words below translate.
SOURCE: dict[str, str] = {
    "trial": "Six posts to see whether this works for you. No card.",
    "free": "Plan and get reminded. No card required.",
    "pro": "Auto-publish, trends and autopilot.",
    "advanced": "Approval chains, API access and the most video.",
    "spark": "One post a week, planned and reminded. Free, no card.",
    "discovery": "Two posts a week on two channels, with five short videos.",
    "essential": "Three posts a week on three channels, with short videos.",
    "growth": "Six posts a week on five channels, autopilot and 1080p video.",
    "scale": "Twelve posts a week, every channel, approval chains and replies.",
    "credits-100": "Images, captions and video. Bought credits do not expire monthly.",
    "credits-500": "Text and image generation. Bought credits do not expire monthly.",
    "videos-4": "Prepaid 8-second videos, on top of your monthly allowance.",
    "templates-premium": (
        "Every premium ad template, for every brand of your organization. Paid once."
    ),
}

PLANS: dict[str, dict[str, str]] = {
    "trial": {
        "fr": "Six publications pour voir si cela vous convient. Sans carte bancaire.",
        "it": "Sei post per capire se fa per te. Senza carta.",
    },
    "free": {
        "fr": "Planifiez et recevez des rappels. Sans carte bancaire.",
        "it": "Pianifica e ricevi promemoria. Nessuna carta richiesta.",
    },
    "pro": {
        "fr": "Publication automatique, tendances et pilote automatique.",
        "it": "Pubblicazione automatica, tendenze e pilota automatico.",
    },
    "advanced": {
        "fr": "Circuits de validation, accès API et le plus de vidéo.",
        "it": "Catene di approvazione, accesso API e più video.",
    },
    "spark": {
        "fr": "Une publication par semaine, planifiée avec rappel. Gratuit, sans carte bancaire.",
        "it": "Un post a settimana, pianificato e con promemoria. Gratis, senza carta.",
    },
    "discovery": {
        "fr": "Deux publications par semaine sur deux réseaux, avec cinq vidéos courtes.",
        "it": "Due post a settimana su due canali, con cinque video brevi.",
    },
    "essential": {
        "fr": "Trois publications par semaine sur trois réseaux, avec des vidéos courtes.",
        "it": "Tre post a settimana su tre canali, con video brevi.",
    },
    "growth": {
        "fr": "Six publications par semaine sur cinq réseaux, pilote automatique et vidéo 1080p.",
        "it": "Sei post a settimana su cinque canali, pilota automatico e video 1080p.",
    },
    "scale": {
        "fr": "Douze publications par semaine, tous les réseaux, circuits de validation "
        "et réponses.",
        "it": "Dodici post a settimana, tutti i canali, catene di approvazione e risposte.",
    },
}

PACKS: dict[str, dict[str, str]] = {
    "credits-100": {
        "fr": "Images, légendes et vidéo. Les crédits achetés n’expirent pas chaque mois.",
        "it": "Immagini, didascalie e video. I crediti acquistati non scadono ogni mese.",
    },
    "credits-500": {
        "fr": "Génération de textes et d’images. Les crédits achetés n’expirent pas "
        "chaque mois.",
        "it": "Generazione di testi e immagini. I crediti acquistati non scadono ogni mese.",
    },
    "videos-4": {
        "fr": "Vidéos de 8 secondes prépayées, en plus de votre quota mensuel.",
        "it": "Video prepagati da 8 secondi, in aggiunta alla tua quota mensile.",
    },
    "templates-premium": {
        "fr": "Tous les modèles publicitaires premium, pour toutes les marques de votre "
        "organisation. Payé une fois.",
        "it": "Tutti i modelli pubblicitari premium, per tutti i brand della tua "
        "organizzazione. Pagato una volta.",
    },
}

#: The help page's reply times, by their seeded English.
SUPPORT: dict[str, dict[str, str]] = {
    "2 working days": {"fr": "2 jours ouvrés", "it": "2 giorni lavorativi"},
    "1 working day": {"fr": "1 jour ouvré", "it": "1 giorno lavorativo"},
    "Same working day": {
        "fr": "Dans la journée (jours ouvrés)",
        "it": "In giornata (giorni lavorativi)",
    },
}


def _words(row: Any, taglines: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    words: dict[str, dict[str, str]] = {"fr": {}, "it": {}}
    if row.code in taglines and row.tagline == SOURCE[row.code]:
        for language, text in taglines[row.code].items():
            words[language]["tagline"] = text
    support = SUPPORT.get(getattr(row, "support_response", ""))
    for language, text in (support or {}).items():
        words[language]["support_response"] = text
    return {language: fields for language, fields in words.items() if fields}


def forwards(apps: Any, schema_editor: Any) -> None:
    for name, taglines in (("Plan", PLANS), ("Pack", PACKS)):
        for row in apps.get_model("billing", name).objects.filter(translations={}):
            words = _words(row, taglines)
            if words:
                row.translations = words
                row.save(update_fields=["translations"])


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0034_ad_prompt_edit"),
    ]

    operations = [
        migrations.AddField(
            model_name="pack",
            name="translations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="plan",
            name="translations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
