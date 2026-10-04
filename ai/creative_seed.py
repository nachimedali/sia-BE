"""The starting catalog for the Studio's controls (`seed_creative_options`).

Data, not behaviour: every choice on every control is a row here and, once
seeded, a row an operator can edit or add to in admin. The seed command only
creates what is missing, so a label or icon retuned in admin survives a
re-seed; `--refresh` overwrites deliberately.

Icons are SVG **path data** on a 24x24 grid, drawn stroked, because that is the
only icon format `CreativeOption` accepts (see its validator). A circle is
therefore a pair of arcs, built by `_circle` rather than written out.

The scenes, lights, cast and moods are the ones in `new_temp/studio.html`.
`prompt_fragment` is what the model is told; `metadata.grade` is how the fake
image provider tints its mock-up, in chroma only (see `ai.providers.fake`).
"""

from __future__ import annotations

from typing import Any


def _circle(cx: float, cy: float, r: float) -> str:
    return f"M{cx - r} {cy}a{r} {r} 0 1 0 {2 * r} 0a{r} {r} 0 1 0 {-2 * r} 0"


def _opt(
    key: str,
    label: str,
    icon: list[str],
    *,
    description: str = "",
    colors: list[str] | None = None,
    fragment: str = "",
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "description": description,
        "icon_paths": icon,
        "colors": colors or [],
        "prompt_fragment": fragment,
        "metadata": metadata,
    }


SUN_RAYS = "M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"
PERSON = [_circle(12, 7, 4), "M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"]
SPARKLE = "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"

SCENES = [
    _opt(
        "sidibou",
        "Sidi Bou Saïd terrace",
        ["M4 21V10l8-6 8 6v11", "M9 21v-6a3 3 0 016 0v6"],
        description="White walls, blue door",
        colors=["#BFE3F2", "#E9F5FA", "#E2D6C2"],
        fragment="on a whitewashed Sidi Bou Saïd terrace with a blue-painted door and a clear sky",
    ),
    _opt(
        "souk",
        "Medina souk",
        ["M12 2v3", "M8 5h8l1 4H7z", "M7 9v8a5 5 0 0010 0V9"],
        description="Arches and lanterns",
        colors=["#5A2E1A", "#A9622E", "#C98B4F"],
        fragment="in a medina souk under warm lantern light, with carved arches behind",
    ),
    _opt(
        "studio",
        "Seamless studio",
        ["M3 20h18", "M5 20V8l7-4 7 4v12"],
        description="Lavender sweep, soft box",
        colors=["#ECE7F8", "#DCD3F1", "#CFC6EC"],
        fragment="on a seamless lavender studio sweep lit by a large soft box",
    ),
    _opt(
        "djerba",
        "Djerba shoreline",
        [
            "M12 21V9",
            "M12 9c-3-4-6-3-8-2 3 0 5 1 8 2z",
            "M12 9c3-4 6-3 8-2-3 0-5 1-8 2z",
            "M3 21h18",
        ],
        description="Palm, sea, warm sand",
        colors=["#9FD8E0", "#DDF1F0", "#E2CA9C"],
        fragment="on the warm sand of a Djerba shoreline with a palm and turquoise sea behind",
    ),
    _opt(
        "dunes",
        "Douz dunes",
        ["M2 18c3-5 6-5 9 0s6 5 11-2", "M2 21h20"],
        description="Sahara gateway",
        colors=["#F6B26B", "#F8D49A", "#E9A35A"],
        fragment="among the rolling golden dunes of Douz at the edge of the Sahara",
    ),
    _opt(
        "rooftop",
        "Tunis rooftop",
        ["M3 21V12h5v9", "M8 21V8h4v13", "M12 21v-9h4v9", "M16 21V10h5v11", "M10 8V5"],
        description="Minaret skyline",
        colors=["#4A3A8C", "#B66A8E", "#F7C982"],
        fragment="on a Tunis rooftop at sunset with a minaret skyline behind",
    ),
]

LIGHTS = [
    _opt(
        "dawn",
        "Dawn",
        ["M3 18h18", "M6 18a6 6 0 0112 0", "M12 6v3", "M5 11l2 1", "M19 11l-2 1"],
        fragment="in soft rose dawn light",
        grade={"chroma": 1.0, "cb": 2, "cr": 10},
        overlay={"color": "#FFBED7", "alpha": 0.38},
    ),
    _opt(
        "golden",
        "Golden hour",
        [_circle(12, 13, 4), "M12 3v2", "M4.2 8.2l1.4 1.4", "M19.8 8.2l-1.4 1.4", "M2 18h20"],
        fragment="in warm golden-hour light with long soft shadows",
        grade={"chroma": 1.1, "cb": -14, "cr": 18},
        overlay={"color": "#FFB85C", "alpha": 0.4},
        default=True,
    ),
    _opt(
        "noon",
        "Noon",
        [_circle(12, 12, 4), SUN_RAYS],
        fragment="in clean, even midday light",
        grade={"chroma": 1.0, "cb": 0, "cr": 0},
        overlay={"color": "#FFFFFF", "alpha": 0.28},
    ),
    _opt(
        "blue",
        "Blue hour",
        ["M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"],
        fragment="in cool blue-hour twilight",
        grade={"chroma": 0.9, "cb": 18, "cr": -12},
        overlay={"color": "#2E2170", "alpha": 0.4},
    ),
]

CAMERAS = [
    _opt(
        "eye",
        "Eye level",
        ["M2 12h20", _circle(12, 12, 4)],
        fragment="shot at eye level, straight on",
        default=True,
    ),
    _opt(
        "top",
        "Flat lay",
        [
            "M5 4h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z",
            "M4 12h16",
            "M12 4v16",
        ],
        fragment="shot from directly overhead as a flat lay",
    ),
    _opt(
        "low",
        "Low hero",
        ["M12 20V5", "M6 11l6-6 6 6"],
        fragment="shot from a low angle looking up, making the product heroic",
    ),
    _opt(
        "macro",
        "Macro",
        [_circle(11, 11, 7), "M21 21l-4.3-4.3", "M11 8v6", "M8 11h6"],
        fragment="a tight macro close-up showing material and texture",
    ),
]

CAST = [
    _opt(
        "none",
        "Product only",
        [_circle(12, 12, 8), "M6.5 6.5l11 11"],
        description="Still life",
        fragment="with no people in the frame, a clean still life",
    ),
    _opt(
        "hands",
        "Hands",
        [
            "M8 13V5.5a1.5 1.5 0 013 0V11",
            "M11 10V4.5a1.5 1.5 0 013 0V11",
            "M14 10.5V6a1.5 1.5 0 013 0v7c0 4-2.5 7-6 7-2.5 0-4-1.5-5.5-3.5L4.5 14.5",
        ],
        description="In use",
        fragment="with a person's hands holding or using the product, faces out of frame",
        default=True,
    ),
    _opt(
        "one",
        "Solo model",
        PERSON,
        description="One person",
        fragment="with one person modelling the product",
    ),
    _opt(
        "group",
        "Group",
        [
            _circle(9, 8, 3.5),
            _circle(17, 9, 2.5),
            "M2 20c1-3.5 3.8-5.5 7-5.5s6 2 7 5.5",
            "M15 14.5c2.8 0 5 1.6 6 4.5",
        ],
        description="Friends, family",
        fragment="with a small group of people sharing the product",
    ),
]

VIBES = [
    _opt(
        "casual",
        "Casual",
        [_circle(12, 12, 8), "M8 14s1.5 2 4 2 4-2 4-2"],
        fragment="relaxed, casual everyday styling",
        default=True,
    ),
    _opt(
        "elegant",
        "Elegant",
        ["M6 3h12l4 6-10 12L2 9z", "M2 9h20"],
        fragment="refined, elegant styling",
    ),
    _opt(
        "family",
        "Family",
        ["M12 21c-5-3-8-6-8-10a5 5 0 018-4 5 5 0 018 4c0 4-3 7-8 10z"],
        fragment="warm, family-oriented styling",
    ),
    _opt(
        "athletic",
        "Athletic",
        ["M13 2L4 14h7l-1 8 9-12h-7z"],
        fragment="energetic, athletic styling",
    ),
]

MOODS = [
    _opt(
        "editorial",
        "Editorial",
        ["M4 5h16", "M4 9h10", "M4 13h16", "M4 17h10"],
        fragment="editorial magazine framing with generous negative space",
        grade={},
        default=True,
    ),
    _opt(
        "minimal",
        "Minimal",
        ["M5 12h14"],
        fragment="minimal, uncluttered composition",
        grade={"chroma": 0.75},
    ),
    _opt(
        "warm",
        "Warm",
        [_circle(12, 12, 4), SUN_RAYS],
        fragment="a warm colour temperature",
        grade={"cb": -6, "cr": 8},
        default=True,
    ),
    _opt(
        "vibrant", "Vibrant", [SPARKLE], fragment="saturated, vibrant colour", grade={"chroma": 1.3}
    ),
    _opt(
        "cinematic",
        "Cinematic",
        ["M4 6h16v12H4z", "M4 10h16", "M8 6v4", "M13 6v4"],
        fragment="cinematic contrast with shallow depth of field",
        grade={"chroma": 0.92, "cb": 6, "cr": -4},
    ),
    _opt(
        "film",
        "Film grain",
        [
            "M5 5h.01",
            "M12 5h.01",
            "M19 5h.01",
            "M8 10h.01",
            "M16 10h.01",
            "M5 15h.01",
            "M12 15h.01",
            "M19 15h.01",
            "M9 20h.01",
            "M15 20h.01",
        ],
        fragment="analogue film grain",
        grade={"chroma": 0.88, "cb": -2, "cr": 3},
    ),
]

PALETTE_ICON = [
    "M12 3a9 9 0 100 18c1.5 0 2-1 1.5-2.2-.5-1.3.2-2.3 1.5-2.3H17a4 4 0 004-4A9 9 0 0012 3z",
    "M8 10h.01",
    "M12 7h.01",
    "M16 10h.01",
]
PALETTES = [
    _opt(
        "brand",
        "Brand indigo and teal",
        PALETTE_ICON,
        colors=["#4A36A0", "#23918E"],
        fragment="a palette of deep indigo and teal",
        ink="#FFFFFF",
        grade={},
        default=True,
    ),
    _opt(
        "sea",
        "Sidi Bou blue",
        PALETTE_ICON,
        colors=["#1F5FA8", "#5EC0B8"],
        fragment="a palette of Mediterranean blue and sea-green",
        ink="#FFFFFF",
        grade={"cb": 10, "cr": -6},
    ),
    _opt(
        "sunset",
        "Saharan sunset",
        PALETTE_ICON,
        colors=["#D9662F", "#F2C46B"],
        fragment="a palette of burnt orange and sand gold",
        ink="#FFF4DE",
        grade={"cb": -10, "cr": 12},
    ),
    _opt(
        "mono",
        "Ink and paper",
        PALETTE_ICON,
        colors=["#1E1745", "#E9E6F2"],
        fragment="a restrained ink-and-paper monochrome palette",
        ink="#FFFFFF",
        grade={"chroma": 0.35},
    ),
]

LANG_ICON = [
    "M3 5h8",
    "M7 3v2",
    "M5 9c1 3 3 5 6 6",
    "M9 5c-.5 4-2.5 7-6 9",
    "M13 21l4-10 4 10",
    "M14.5 18h5",
]
LANGUAGES = [
    _opt(
        "fr",
        "FR",
        LANG_ICON,
        description="French",
        fragment="Write the headline and caption in French.",
        code="fr",
        name="French",
        default=True,
    ),
    _opt(
        "en",
        "EN",
        LANG_ICON,
        description="English",
        fragment="Write the headline and caption in English.",
        code="en",
        name="English",
    ),
]

CTAS = [
    _opt(
        "shop",
        "Shop now",
        ["M5 12h14", "M13 6l6 6-6 6"],
        fragment="End with a clear call to action to shop now.",
        short="Shop now",
        default=True,
    ),
    _opt(
        "learn",
        "Learn more",
        [_circle(12, 12, 9), "M12 11v5", "M12 8h.01"],
        fragment="End with a call to action to learn more.",
        short="Learn more",
    ),
    _opt(
        "visit",
        "Visit us",
        ["M12 21s-7-6.2-7-11a7 7 0 0114 0c0 4.8-7 11-7 11z", _circle(12, 10, 2.5)],
        fragment="End with a call to action to visit the shop.",
        short="Get directions",
    ),
    _opt(
        "dm",
        "DM to order",
        ["M20 12a8 8 0 01-11.8 7L4 20l1-4.2A8 8 0 1120 12z"],
        fragment="End with a call to action to send a direct message to order.",
        short="Send message",
    ),
]

FRAME = "M5 4h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z"
FORMATS = [
    _opt(
        "single",
        "Single",
        [FRAME],
        description="Image post, 4:5",
        fragment="",
        kind="IMAGE",
        aspect="4:5",
        post_format="FEED",
        default=True,
    ),
    _opt(
        "square",
        "Square",
        ["M5 5h14v14H5z"],
        description="Image post, 1:1",
        fragment="",
        kind="IMAGE",
        aspect="1:1",
        post_format="FEED",
    ),
    _opt(
        "story",
        "Reel 9:16",
        ["M7 2h10a1 1 0 011 1v18a1 1 0 01-1 1H7a1 1 0 01-1-1V3a1 1 0 011-1z"],
        description="Vertical cover frame, 9:16",
        fragment="",
        kind="IMAGE",
        aspect="9:16",
        post_format="REEL",
    ),
    _opt(
        "wide",
        "Wide",
        ["M3 6h18v12H3z"],
        description="Image post, 16:9",
        fragment="",
        kind="IMAGE",
        aspect="16:9",
        post_format="FEED",
    ),
    _opt(
        "text",
        "Text only",
        ["M4 6h16", "M4 12h16", "M4 18h10"],
        description="Captions, no image",
        fragment="",
        kind="TEXT",
        aspect="1:1",
        post_format="FEED",
    ),
]

TEMPO_ICON = ["M8 21h8l-2-17h-4z", "M12 14l4-8"]
TEMPOS = [
    _opt(
        "adagio",
        "Adagio",
        TEMPO_ICON,
        description="Faithful to the brief",
        fragment="Stay very close to the brief; no surprises.",
    ),
    _opt(
        "andante",
        "Andante",
        TEMPO_ICON,
        description="Gentle variation",
        fragment="Vary gently between versions.",
    ),
    _opt(
        "allegro",
        "Allegro",
        TEMPO_ICON,
        description="Lively variation",
        fragment="Take some creative liberty; make each version distinct.",
        default=True,
    ),
    _opt(
        "presto",
        "Presto",
        TEMPO_ICON,
        description="Bold variation",
        fragment="Be bold and experimental; make every version clearly different.",
    ),
]

DYN_ICON = ["M3 17V9", "M9 17V5", "M15 17v-6", "M21 17V3"]
DYNAMICS = [
    _opt(
        "pianissimo",
        "pianissimo",
        DYN_ICON,
        fragment="Very subdued colour intensity.",
        grade={"chroma": 0.6},
    ),
    _opt("piano", "piano", DYN_ICON, fragment="Soft colour intensity.", grade={"chroma": 0.8}),
    _opt(
        "mezzo-forte",
        "mezzo-forte",
        DYN_ICON,
        fragment="Natural colour intensity.",
        grade={"chroma": 1.0},
    ),
    _opt(
        "forte",
        "forte",
        DYN_ICON,
        fragment="Strong colour intensity.",
        grade={"chroma": 1.2},
        default=True,
    ),
    _opt(
        "fortissimo",
        "fortissimo",
        DYN_ICON,
        fragment="Very intense colour.",
        grade={"chroma": 1.45},
    ),
]

TONE_ICON = [_circle(12, 12, 9), "M8 14s1.5 2 4 2 4-2 4-2", "M9 9h.01", "M15 9h.01"]
TONES = [
    _opt("playful", "Playful", TONE_ICON, fragment="Voice: playful and light-hearted."),
    _opt(
        "warm",
        "Warm, balanced",
        TONE_ICON,
        fragment="Voice: warm, friendly and balanced.",
        default=True,
    ),
    _opt("formal", "Formal", TONE_ICON, fragment="Voice: formal and composed."),
]

TOGGLES = [
    _opt(
        "headline_on_image",
        "Headline on image",
        ["M4 7V4h16v3", "M9 20h6", "M12 4v16"],
        description="Serif title over the shot",
        default=True,
        fragment="Also write a short headline of at most eight words to set over the image.",
    ),
    _opt(
        "hashtags",
        "Hashtags",
        ["M4 9h16", "M4 15h16", "M10 3L8 21", "M16 3l-2 18"],
        description="Observed tags from your category",
        default=True,
    ),
    _opt(
        "logo_mark",
        "Logo watermark",
        ["M12 3l9 4.5v9L12 21l-9-4.5v-9z"],
        description="Bottom-right, 15% width",
        default=True,
    ),
]

PRESETS = [
    _opt(
        "summer-launch",
        "Summer launch",
        [SPARKLE],
        description="Djerba · golden · solo",
        values={
            "scene": "djerba",
            "light": "golden",
            "cast": "one",
            "tempo": "allegro",
            "moods": ["vibrant", "warm"],
            "vibe": "casual",
            "camera": "eye",
        },
    ),
    _opt(
        "ramadan-nights",
        "Ramadan nights",
        [SPARKLE],
        description="Rooftop · blue · group",
        values={
            "scene": "rooftop",
            "light": "blue",
            "cast": "group",
            "tempo": "andante",
            "moods": ["cinematic", "warm", "editorial"],
            "vibe": "family",
            "camera": "low",
        },
    ),
    _opt(
        "evergreen",
        "Evergreen catalogue",
        [SPARKLE],
        description="Studio · noon · still",
        values={
            "scene": "studio",
            "light": "noon",
            "cast": "none",
            "tempo": "adagio",
            "moods": ["minimal"],
            "camera": "eye",
        },
    ),
    _opt(
        "souk-story",
        "Souk story",
        [SPARKLE],
        description="Medina · hands · grain",
        values={
            "scene": "souk",
            "light": "golden",
            "cast": "hands",
            "tempo": "presto",
            "moods": ["film", "editorial"],
            "camera": "low",
        },
    ),
]

QUICK_TAGS = [
    _opt(key, label, [], fragment=fragment)
    for key, label, fragment in [
        ("launch", "Launch", "a product launch"),
        ("seasonal", "Seasonal", "the current season"),
        ("behind-the-scenes", "Behind the scenes", "a behind-the-scenes look"),
        ("gift-idea", "Gift idea", "a gift idea"),
        ("ramadan", "Ramadan", "Ramadan"),
    ]
]

#: The post editor's "What should change?" chips (`content.services.regeneration`).
REVISE_REASONS = [
    _opt(key, label, [], fragment=fragment)
    for key, label, fragment in [
        ("wrong-scene", "Wrong scene", "put the product in a different scene"),
        ("product-too-small", "Product too small", "make the product larger in the frame"),
        ("off-brand-colours", "Off-brand colours", "keep to the brand's own colours"),
        ("caption-tone", "Caption tone", "rework the tone of the caption"),
        ("text-on-image", "Text on image", "keep text off the image"),
        ("more-variety", "More variety", "make the images more varied from each other"),
    ]
]

#: kind -> rows. Order within a kind is `sort_order`, assigned by position.

# --- The product form's choice lists ---------------------------------------
#
# Language-neutral on purpose: a brand's own words belong in its product, these
# are only the one-click starting points under each free-text list.

AUDIENCES = [
    _opt(key, label, [])
    for key, label in (
        ("home-cooks", "Home cooks"),
        ("young-professionals", "Young professionals"),
        ("families", "Families"),
        ("tourists", "Tourists"),
        ("gift-buyers", "Gift buyers"),
        ("diaspora", "Diaspora"),
        ("b2b-buyers", "B2B buyers"),
    )
]

#: `values` are the four voice sliders, 0-100. Presets are a starting point the
#: user then moves; the product stores the numbers, not the preset's name alone.
TONE_PRESETS = [
    _opt(
        "artisan",
        "Warm artisan",
        [],
        description="Craft, origin, patience",
        values={"formal": 35, "bold": 35, "modern": 25, "poetic": 70},
    ),
    _opt(
        "premium",
        "Premium minimal",
        [],
        description="Few words, high polish",
        values={"formal": 80, "bold": 25, "modern": 80, "poetic": 30},
    ),
    _opt(
        "playful",
        "Playful pop",
        [],
        description="Bright, quick, friendly",
        values={"formal": 12, "bold": 85, "modern": 75, "poetic": 20},
    ),
    _opt(
        "heritage",
        "Heritage storyteller",
        [],
        description="Roots, family, tradition",
        values={"formal": 60, "bold": 40, "modern": 10, "poetic": 85},
    ),
]

#: A claim is only usable once proof is attached (see `products.brief`).
CLAIMS = [
    _opt(key, label, [], description=proof)
    for key, label, proof in (
        ("organic", "Organic", "Needs a certificate"),
        ("handmade", "Hand-made", "Workshop photo or statement"),
        ("natural", "100% natural", "Ingredient list"),
        ("made-in-tunisia", "Made in Tunisia", "Origin certificate"),
        ("award-winning", "Award-winning", "Award name and year"),
    )
]

SHOT_TAGS = [
    _opt(key, label, [])
    for key, label in (
        ("front", "Front"),
        ("side-back", "Side / back"),
        ("detail", "Detail"),
        ("packaging", "Packaging"),
        ("in-use", "In use"),
        ("lifestyle", "Lifestyle"),
    )
]

ASPECTS = [
    _opt(key, label, [], aspect=key.replace("-", ":"))
    for key, label in (
        ("1-1", "Square 1:1"),
        ("4-5", "Portrait 4:5"),
        ("9-16", "Vertical 9:16"),
        ("16-9", "Landscape 16:9"),
    )
]

#: `metadata.field` names the product list the chip adds to.
SUGGESTIONS = [
    _opt(f"{field}-{slug}", label, [], field=field)
    for field, entries in (
        (
            "features",
            (
                ("handmade", "Hand-made"),
                ("small-batch", "Small batch"),
                ("locally-sourced", "Locally sourced"),
                ("gift-ready", "Gift-ready"),
                ("sustainable", "Sustainably made"),
            ),
        ),
        (
            "use_words",
            (
                ("made-in-tunisia", "made in Tunisia"),
                ("craft", "craft"),
                ("new-season", "new season"),
                ("our-story", "our story"),
                ("small-batch", "small batch"),
            ),
        ),
        (
            "avoid_words",
            (
                ("cheap", "cheap"),
                ("best-ever", "best ever"),
                ("miracle", "miracle"),
                ("guaranteed", "guaranteed"),
                ("limited-time", "limited time only"),
            ),
        ),
        (
            "must_include",
            (
                ("logo", "Logo visible"),
                ("focus", "Product in focus"),
                ("origin", "Origin mentioned"),
                ("cta", "Call to action"),
            ),
        ),
        (
            "restrictions",
            (
                ("alcohol", "Alcohol"),
                ("competitors", "Competitor brands"),
                ("health-claims", "Health claims"),
                ("politics", "Political symbols"),
                ("pork", "Pork products"),
                ("smoking", "Smoking"),
            ),
        ),
    )
    for slug, label in entries
]

CATALOG: dict[str, list[dict[str, Any]]] = {
    "scene": SCENES,
    "light": LIGHTS,
    "camera": CAMERAS,
    "cast": CAST,
    "vibe": VIBES,
    "mood": MOODS,
    "palette": PALETTES,
    "language": LANGUAGES,
    "cta": CTAS,
    "format": FORMATS,
    "tempo": TEMPOS,
    "dynamics": DYNAMICS,
    "tone": TONES,
    "toggle": TOGGLES,
    "preset": PRESETS,
    "quick_tag": QUICK_TAGS,
    "audience": AUDIENCES,
    "tone_preset": TONE_PRESETS,
    "claim": CLAIMS,
    "shot_tag": SHOT_TAGS,
    "aspect": ASPECTS,
    "suggestion": SUGGESTIONS,
    "revise_reason": REVISE_REASONS,
}
