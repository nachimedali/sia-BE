"""S20-01: the ready-made scenes a storyboard is built from (`studio_storyboard.md`).

One row per scene, kind `story_scene`. `prompt_fragment` is the action the video
model reads (server-side, like every fragment); `{product}` and `{person}` are
filled at render time. `metadata.person` says whether the scene shows the brand's
avatar (`none`, `optional`, `required`), `metadata.image` is the card's picture
and `metadata.suggestions` the "your words" chips, per interface language (L-6).

A new scene is a row in admin (rule 18): nothing here is read by code but the seed.
"""

from __future__ import annotations

from typing import Any

SEED_VERSION = 1


def _scene(
    key: str,
    label: str,
    description: str,
    action: str,
    person: str,
    image: str,
    suggestions: dict[str, list[str]],
    fr: tuple[str, str],
    it: tuple[str, str],
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "description": description,
        "icon_paths": [],
        "colors": [],
        "prompt_fragment": action,
        "metadata": {
            "person": person,
            "image": f"/studio/templates/{image}.webp",
            "suggestions": suggestions,
            "seed": SEED_VERSION,
        },
        "translations": {
            "fr": {"label": fr[0], "description": fr[1]},
            "it": {"label": it[0], "description": it[1]},
        },
    }


SCENES: list[dict[str, Any]] = [
    _scene(
        "hook",
        "Opening hook",
        "The product fills the frame",
        "{product} fills the frame as the camera moves slowly closer.",
        "none",
        "flat-lay",
        {
            "en": ["the product spins into view", "a flash of light, then the product"],
            "fr": [
                "le produit tourne et entre dans le cadre",
                "un éclat de lumière, puis le produit",
            ],
            "it": ["il prodotto entra ruotando", "un lampo di luce, poi il prodotto"],
        },
        ("Accroche", "Le produit remplit le cadre"),
        ("Apertura", "Il prodotto riempie l'inquadratura"),
    ),
    _scene(
        "pour",
        "The pour",
        "Close on the product as it pours",
        "close on {product} as it pours, glossy and unhurried.",
        "optional",
        "pour-shot",
        {
            "en": ["pouring over a fresh salad", "golden drops catch the light"],
            "fr": ["versé sur une salade fraîche", "des gouttes dorées dans la lumière"],
            "it": ["versato su un'insalata fresca", "gocce dorate nella luce"],
        },
        ("Le versement", "Au plus près du produit qui coule"),
        ("Il versamento", "Da vicino mentre il prodotto scende"),
    ),
    _scene(
        "unbox",
        "Opening the box",
        "The lid lifts and the product appears",
        "the lid lifts and {product} appears in soft light.",
        "optional",
        "slow-unbox",
        {
            "en": ["hands open it slowly", "a ribbon is pulled first"],
            "fr": ["des mains l'ouvrent lentement", "on tire d'abord un ruban"],
            "it": ["mani che lo aprono piano", "prima si tira un nastro"],
        },
        ("Ouverture", "Le couvercle se lève, le produit apparaît"),
        ("Apertura della scatola", "Il coperchio si alza e appare il prodotto"),
    ),
    _scene(
        "gift",
        "Gift reveal",
        "A ribbon, then the reveal",
        "a ribbon is pulled and {product} is revealed.",
        "optional",
        "gift-reveal",
        {
            "en": ["wrapped in brand colours", "a gift tag turns to camera"],
            "fr": ["emballé aux couleurs de la marque", "une étiquette se tourne vers la caméra"],
            "it": ["incartato nei colori del brand", "un bigliettino si gira verso la camera"],
        },
        ("Le cadeau", "Un ruban, puis la surprise"),
        ("Il regalo", "Un nastro, poi la sorpresa"),
    ),
    _scene(
        "talk",
        "Talks to camera",
        "A face shows why it's good",
        "{person} holds {product} next to their face and smiles at the camera.",
        "required",
        "first-look",
        {
            "en": ["waves at the start", "laughing, relaxed"],
            "fr": ["fait coucou au début", "rit, détendu"],
            "it": ["saluta all'inizio", "ride, rilassato"],
        },
        ("Face caméra", "Un visage montre pourquoi c'est bon"),
        ("In camera", "Un volto mostra perché è buono"),
    ),
    _scene(
        "reaction",
        "First reaction",
        "Tries it and smiles",
        "{person} tries {product} for the first time and reacts with a smile.",
        "required",
        "reaction-reveal",
        {
            "en": ["eyes widen, then a smile", "nods slowly"],
            "fr": ["les yeux s'ouvrent, puis un sourire", "hoche lentement la tête"],
            "it": ["occhi spalancati, poi un sorriso", "annuisce piano"],
        },
        ("Première réaction", "Essaie et sourit"),
        ("Prima reazione", "Prova e sorride"),
    ),
    _scene(
        "walk",
        "Out and about",
        "Walking with it in hand",
        "{person} walks past with {product} in hand.",
        "required",
        "street-carry",
        {
            "en": ["walking with friends", "in the morning light"],
            "fr": ["en marchant avec des amis", "dans la lumière du matin"],
            "it": ["camminando con gli amici", "nella luce del mattino"],
        },
        ("En balade", "Marche, le produit à la main"),
        ("A passeggio", "Cammina con il prodotto in mano"),
    ),
    _scene(
        "market",
        "At the market",
        "Picks it up from a stall",
        "{person} picks {product} up from a market stall.",
        "required",
        "market-stroll",
        {
            "en": ["buys it and smiles", "chats with the seller"],
            "fr": ["l'achète et sourit", "discute avec le vendeur"],
            "it": ["lo compra e sorride", "parla con il venditore"],
        },
        ("Au marché", "Le prend sur un étal"),
        ("Al mercato", "Lo prende da una bancarella"),
    ),
    _scene(
        "table",
        "On the table",
        "Morning light, product only",
        "{product} on a table in morning light, steam rising from a cup nearby.",
        "none",
        "morning-ritual",
        {
            "en": ["sunlight moves across the table", "fresh bread next to it"],
            "fr": ["le soleil glisse sur la table", "du pain frais à côté"],
            "it": ["il sole scorre sul tavolo", "pane fresco accanto"],
        },
        ("Sur la table", "Lumière du matin, produit seul"),
        ("Sul tavolo", "Luce del mattino, solo il prodotto"),
    ),
    _scene(
        "beach",
        "By the sea",
        "Warm sand, sea behind",
        "{product} rests on warm sand with the sea moving behind it.",
        "optional",
        "beach-day",
        {
            "en": ["playing volleyball", "reading under a parasol"],
            "fr": ["en jouant au volley", "en lisant sous un parasol"],
            "it": ["giocando a pallavolo", "leggendo sotto l'ombrellone"],
        },
        ("Au bord de la mer", "Sable chaud, la mer derrière"),
        ("In riva al mare", "Sabbia calda, il mare dietro"),
    ),
    _scene(
        "turn",
        "A slow turn",
        "The label turns to camera",
        "a hand turns {product} slowly until the label faces the camera.",
        "optional",
        "hand-turn",
        {
            "en": ["drops of water on it", "turns on a marble top"],
            "fr": ["des gouttes d'eau dessus", "tourne sur un plan de marbre"],
            "it": ["gocce d'acqua sopra", "ruota su un piano di marmo"],
        },
        ("Un lent tour", "L'étiquette se tourne vers la caméra"),
        ("Un giro lento", "L'etichetta si gira verso la camera"),
    ),
    _scene(
        "closeup",
        "Close-up",
        "Texture and label",
        "a slow, close move across the texture and label of {product}.",
        "none",
        "swatch-test",
        {
            "en": ["light glints on the surface", "the finest detail in focus"],
            "fr": ["la lumière brille sur la surface", "le moindre détail net"],
            "it": ["la luce brilla sulla superficie", "ogni dettaglio a fuoco"],
        },
        ("Gros plan", "Texture et étiquette"),
        ("Primo piano", "Texture ed etichetta"),
    ),
]
