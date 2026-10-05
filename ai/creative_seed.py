"""The catalog behind every Studio and product-form control — version 2.

Data, not behaviour: every choice on every control is a row here and, once
seeded, a row an operator can edit or add to in admin. `seed_creative_options`
only creates what is missing, so a label or icon retuned in admin survives a
re-seed; `--refresh` overwrites deliberately. `ai/migrations/0011` replaced the
first-generation seed with this one.

**How the rows are written.** `prompt_fragment` is what the model is told, and
`ai.services.creative` places it after a lead-in ("Setting: …", "Lighting: …",
"Camera: …", "People: …", "Styling: …", "Colour palette: …", "Intensity: …",
"Variation: …"), so a fragment is a phrase without a closing full stop. Scene
fragments start with a preposition because the copywriter is also told "The
picture is set <fragment>." Caption-side fragments (language, voice, call to
action, toggles) are whole sentences.

The direction follows the ad-production practice in the GooseWorks ads skills
(product-photoshoot, create-image, verify-product-image, ad-angle-miner):

* **The product is the hero and is never redesigned.** Scenes and cameras say
  where it sits and how large it reads; nothing here describes the product
  itself — its shape, label, colours and logo come from the reference photos.
* **Photography, not "AI art".** Lights are named setups, cameras name the
  angle, lens and depth of field, moods name a grade. People get realism cues
  (natural skin texture, real proportions, unposed) because stock-smooth faces
  are the most common way a generated ad reads as fake.
* **No readable text in the picture.** Image models garble words; the headline
  is written separately and set over the image (`headline_on_image`).
* **No invented claims.** Calls to action and quick tags ask for nothing the
  brand cannot back — no "best seller", no fake urgency.

Icons are SVG **path data** on a 24x24 grid, drawn stroked — the only icon
format `CreativeOption` accepts. `metadata.grade` (chroma multiplier, Cb/Cr
shift) and `metadata.overlay` are how the fake image provider and the Studio's
preview tint a mock-up; real providers take their direction from the prompt.
Every row carries `metadata.seed = SEED_VERSION`, which is how a later catalog
migration tells a seeded row from one an operator added.
"""

from __future__ import annotations

from typing import Any

#: Marks every row this file seeds (see `ai/migrations/0011_catalog_v2.py`).
SEED_VERSION = "v2"


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
        "metadata": {**metadata, "seed": SEED_VERSION},
    }


# --- shared icon parts -----------------------------------------------------------
SUN_RAYS = "M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"
PERSON = [_circle(12, 7, 4), "M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"]
SPARKLE = "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"
FRAME = "M5 4h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z"
HOUSE = ["M3 11l9-7 9 7", "M5 10v10h14V10"]
TABLE = ["M3 10h18", "M5 10v10", "M19 10v10", "M8 10V7h8v3"]
LEAF = ["M5 19C5 10 10 5 19 5c0 9-5 14-14 14z", "M5 19l8-8"]
WAVES = ["M2 15c2.5-2 5-2 7.5 0s5 2 7.5 0 5-2 5-2", "M2 19c2.5-2 5-2 7.5 0s5 2 7.5 0 5-2 5-2"]

# --- scenes ------------------------------------------------------------------------
#: `metadata.group` sorts the scenes into sets the Studio can show together.
SCENES = [
    # studio sets
    _opt(
        "seamless-studio",
        "Seamless studio",
        ["M3 20h18", "M5 20V8l7-4 7 4v12"],
        description="Infinity cove, clean catalogue",
        colors=["#F2F0F7", "#E4E0EE", "#D3CDE3"],
        fragment="on a seamless infinity-cove studio backdrop in a soft neutral tone, the product "
        "centred and grounded by a gentle contact shadow, nothing else in the frame",
        group="studio",
        default=True,
    ),
    _opt(
        "colour-block",
        "Colour-block set",
        ["M4 4h7v16H4z", "M13 4h7v7h-7z", "M13 13h7v7h-7z"],
        description="Bold solid backdrop and plinth",
        colors=["#F4B942", "#E8604C", "#4A36A0"],
        fragment="on a bold colour-block set: a single saturated backdrop with a matching plinth, "
        "crisp graphic shadows and generous empty space around the product",
        group="studio",
    ),
    _opt(
        "stone-plinth",
        "Sculptural plinth",
        ["M4 20h16", "M6 20v-6h12v6", "M9 14V9h6v5"],
        description="Travertine steps, gallery calm",
        colors=["#E9DFD0", "#D2C3AE", "#B9A68C"],
        fragment="on stepped travertine and plaster plinths in a quiet gallery-like set, with "
        "soft-edged shadows and a single sculptural prop at most",
        group="studio",
    ),
    _opt(
        "water-reflection",
        "Water and reflection",
        WAVES,
        description="Shallow water, ripples, caustics",
        colors=["#BFE3F2", "#7CC4D8", "#E9F5FA"],
        fragment="in a few centimetres of still, clear water with gentle ripples, light caustics "
        "and a clean reflection beneath the product",
        group="studio",
    ),
    _opt(
        "botanical-set",
        "Botanical set",
        LEAF,
        description="Fresh leaves framing the product",
        colors=["#5E8C4A", "#A9C79A", "#EEF3E6"],
        fragment="among fresh leaves and stems placed close to the lens and softly out of focus, "
        "the product clear and unobstructed at the centre",
        group="studio",
    ),
    # at home
    _opt(
        "kitchen-counter",
        "Kitchen counter",
        TABLE,
        description="Marble or wood, ingredients nearby",
        colors=["#EDE7DF", "#C9B79C", "#8A6E4B"],
        fragment="on a lived-in kitchen counter of pale marble and oiled wood, with a few fresh "
        "ingredients and utensils softly blurred behind",
        group="home",
    ),
    _opt(
        "breakfast-table",
        "Breakfast table",
        [_circle(9, 13, 5), "M14 13h5", "M17 10v6"],
        description="Linen, ceramics, morning",
        colors=["#F6F1E6", "#E4D3B5", "#B98B5E"],
        fragment="on a breakfast table laid with crumpled linen, handmade ceramics and a cup of "
        "coffee, as if someone has just sat down",
        group="home",
    ),
    _opt(
        "bathroom-vanity",
        "Bathroom vanity",
        ["M4 13h16", "M6 13v6h12v-6", "M12 4v5", "M9 4h6"],
        description="Stone basin, towels, mirror",
        colors=["#F1EEEA", "#D9D2C9", "#A9B7B1"],
        fragment="on a calm bathroom vanity of honed stone beside a basin, folded towels and a "
        "mirror softly out of focus",
        group="home",
    ),
    _opt(
        "living-room",
        "Living room",
        ["M3 18v-5a2 2 0 012-2h14a2 2 0 012 2v5", "M3 18h18", "M6 11V8h12v3"],
        description="Sofa, rug, books, a plant",
        colors=["#E8E1D6", "#B9A58A", "#6F7F5E"],
        fragment="in a warm, uncluttered living room with a linen sofa, a wool rug, a stack of "
        "books and a plant, the product placed where it would really be used",
        group="home",
    ),
    _opt(
        "work-desk",
        "Work desk",
        ["M3 17h18", "M6 17V9h12v8", "M10 21h4"],
        description="Laptop, notebook, tidy desk",
        colors=["#ECEBE8", "#C7C4BD", "#3F4A5A"],
        fragment="on a tidy work desk with a laptop, a notebook and a lamp, natural daylight "
        "from a window to one side",
        group="home",
    ),
    # outdoors
    _opt(
        "beach-shoreline",
        "Beach shoreline",
        [
            "M12 21V9",
            "M12 9c-3-4-6-3-8-2 3 0 5 1 8 2z",
            "M12 9c3-4 6-3 8-2-3 0-5 1-8 2z",
            "M3 21h18",
        ],
        description="Warm sand, sea, palm shade",
        colors=["#9FD8E0", "#DDF1F0", "#E2CA9C"],
        fragment="on warm, fine sand at the water's edge with a turquoise sea and the dappled "
        "shade of a palm",
        group="outdoor",
    ),
    _opt(
        "city-street",
        "City street",
        ["M3 21V10h6v11", "M9 21V5h6v16", "M15 21v-8h6v8"],
        description="Pavement, shopfronts, bokeh",
        colors=["#5B6474", "#A7ADB8", "#E3C37E"],
        fragment="on a city street at pavement level with shopfronts and passers-by dissolved into "
        "soft background bokeh",
        group="outdoor",
    ),
    _opt(
        "garden-terrace",
        "Garden terrace",
        [*LEAF, "M3 21h18"],
        description="Olive and lemon trees, tiles",
        colors=["#7E9A5B", "#E6D9A8", "#C46A3E"],
        fragment="on a sunlit garden terrace of terracotta tiles under olive and lemon trees, "
        "leaves casting soft patterned shade",
        group="outdoor",
    ),
    _opt(
        "desert-dunes",
        "Desert dunes",
        ["M2 18c3-5 6-5 9 0s6 5 11-2", "M2 21h20"],
        description="Sahara ripples near Douz",
        colors=["#F6B26B", "#F8D49A", "#E9A35A"],
        fragment="on the wind-rippled crest of a golden Saharan dune near Douz, with a clear sky "
        "and long sand shadows",
        group="outdoor",
    ),
    # Tunisian signatures
    _opt(
        "sidi-bou-said",
        "Sidi Bou Saïd",
        ["M4 21V10l8-6 8 6v11", "M9 21v-6a3 3 0 016 0v6"],
        description="Whitewash and blue doors",
        colors=["#BFE3F2", "#E9F5FA", "#1F5FA8"],
        fragment="on a whitewashed terrace in Sidi Bou Saïd beside a studded blue door, with "
        "bougainvillea and a clear Mediterranean sky",
        group="tunisia",
    ),
    _opt(
        "medina-souk",
        "Medina souk",
        ["M12 2v3", "M8 5h8l1 4H7z", "M7 9v8a5 5 0 0010 0V9"],
        description="Arches, lanterns, textiles",
        colors=["#5A2E1A", "#A9622E", "#C98B4F"],
        fragment="in a medina souk under carved stone arches, with brass lanterns and hand-woven "
        "textiles warmly blurred behind",
        group="tunisia",
    ),
    _opt(
        "dar-courtyard",
        "Dar courtyard",
        [*HOUSE, "M10 20v-5h4v5"],
        description="Patterned tiles, fountain, arches",
        colors=["#F3EEE4", "#2E8C8A", "#C9A45C"],
        fragment="in the courtyard of a traditional Tunisian dar with patterned zellige tiles, "
        "horseshoe arches and a small fountain",
        group="tunisia",
    ),
    _opt(
        "tunis-rooftop",
        "Rooftop at dusk",
        ["M3 21V12h5v9", "M8 21V8h4v13", "M12 21v-9h4v9", "M16 21V10h5v11", "M10 8V5"],
        description="Minaret skyline, evening",
        colors=["#4A3A8C", "#B66A8E", "#F7C982"],
        fragment="on a Tunis rooftop at dusk with whitewashed domes and a minaret on the skyline",
        group="tunisia",
    ),
]

# --- lights ------------------------------------------------------------------------
LIGHTS = [
    _opt(
        "soft-daylight",
        "Soft daylight",
        ["M4 4h16v16H4z", "M4 12h16", "M12 4v16"],
        description="Large window, gentle shadows",
        fragment="soft natural daylight from a large window to one side, gentle wraparound "
        "shadows and true-to-life colour",
        grade={"chroma": 1.0, "cb": 0, "cr": 2},
        overlay={"color": "#FFFFFF", "alpha": 0.22},
        default=True,
    ),
    _opt(
        "golden-hour",
        "Golden hour",
        [_circle(12, 13, 4), "M12 3v2", "M4.2 8.2l1.4 1.4", "M19.8 8.2l-1.4 1.4", "M2 18h20"],
        description="Low warm sun, long shadows",
        fragment="low golden-hour sunlight raking across the scene, warm highlights, long soft "
        "shadows and a gentle backlit glow on the product's edges",
        grade={"chroma": 1.1, "cb": -14, "cr": 18},
        overlay={"color": "#FFB85C", "alpha": 0.36},
    ),
    _opt(
        "high-key",
        "High-key studio",
        [_circle(12, 12, 4), SUN_RAYS],
        description="Bright, even, catalogue-clean",
        fragment="bright high-key studio lighting from a large softbox and fill, almost "
        "shadowless, clean whites and accurate product colour",
        grade={"chroma": 0.95, "cb": 0, "cr": 0},
        overlay={"color": "#FFFFFF", "alpha": 0.32},
    ),
    _opt(
        "low-key",
        "Low-key dramatic",
        [_circle(12, 12, 8), "M12 4a8 8 0 010 16z"],
        description="Dark set, sculpted rim light",
        fragment="low-key dramatic lighting on a dark set: a single hard key light and a crisp rim "
        "light carving the product's silhouette out of deep shadow",
        grade={"chroma": 0.9, "cb": 4, "cr": -2},
        overlay={"color": "#1E1745", "alpha": 0.42},
    ),
    _opt(
        "hard-sun",
        "Mediterranean sun",
        [_circle(12, 12, 3), SUN_RAYS],
        description="Midday sun, crisp shadows",
        fragment="strong midday Mediterranean sun with crisp, graphic shadows and saturated blue "
        "and white tones",
        grade={"chroma": 1.15, "cb": 6, "cr": 0},
        overlay={"color": "#FFF6E0", "alpha": 0.24},
    ),
    _opt(
        "overcast",
        "Overcast diffused",
        ["M7 18a4 4 0 010-8 5 5 0 019.6-1.5A3.5 3.5 0 1117 18z"],
        description="Even, shadowless, true colour",
        fragment="soft overcast light, evenly diffused with almost no shadows and muted, natural "
        "colour",
        grade={"chroma": 0.88, "cb": 3, "cr": -1},
        overlay={"color": "#E6E8EE", "alpha": 0.3},
    ),
    _opt(
        "blue-hour",
        "Blue hour",
        ["M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"],
        description="Cool twilight, warm practicals",
        fragment="cool blue-hour twilight with a few warm practical lights glowing in the "
        "background",
        grade={"chroma": 0.9, "cb": 18, "cr": -12},
        overlay={"color": "#2E2170", "alpha": 0.38},
    ),
    _opt(
        "neon-gel",
        "Coloured gels",
        [
            "M9 18h6",
            "M10 21h4",
            "M12 3a6 6 0 00-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0012 3z",
        ],
        description="Two-tone gel lighting, night",
        fragment="two-tone coloured gel lighting, magenta from one side and teal from the other, "
        "with glossy reflections on a dark set",
        grade={"chroma": 1.3, "cb": 10, "cr": 10},
        overlay={"color": "#B13AA0", "alpha": 0.3},
    ),
]

# --- camera ------------------------------------------------------------------------
CAMERAS = [
    _opt(
        "eye-level",
        "Eye-level hero",
        ["M2 12h20", _circle(12, 12, 4)],
        description="Straight on, 50 mm",
        fragment="straight on at the product's eye level with a 50 mm lens, the product filling "
        "about half the frame and in sharp focus",
        default=True,
    ),
    _opt(
        "three-quarter",
        "Three-quarter",
        ["M4 18L12 6l8 12z", "M12 6v12"],
        description="45° turn, 85 mm, shallow focus",
        fragment="a three-quarter view with the product turned 45 degrees, 85 mm lens at f/2.8, "
        "background falling softly out of focus",
    ),
    _opt(
        "flat-lay",
        "Flat lay",
        [FRAME, "M4 12h16", "M12 4v16"],
        description="Top-down, styled composition",
        fragment="a top-down flat lay from directly overhead, the product as the anchor of a tidy, "
        "deliberate composition with a few supporting props",
    ),
    _opt(
        "forty-five",
        "45° tabletop",
        ["M4 20h16", "M6 20l8-12", "M14 8l4 4"],
        description="The food and tabletop angle",
        fragment="a 45-degree overhead tabletop angle, the classic food-and-product view that "
        "shows both the top and the front of the product",
    ),
    _opt(
        "low-hero",
        "Low hero",
        ["M12 20V5", "M6 11l6-6 6 6"],
        description="Looking up, monumental",
        fragment="a low angle looking up at the product with a 35 mm lens, making it feel "
        "monumental against the background",
    ),
    _opt(
        "macro",
        "Macro detail",
        [_circle(11, 11, 7), "M21 21l-4.3-4.3", "M11 8v6", "M8 11h6"],
        description="100 mm macro, texture",
        fragment="a 100 mm macro close-up on the product's material, finish and texture, a "
        "razor-thin plane of focus",
    ),
    _opt(
        "close-crop",
        "Tight crop",
        ["M4 9V4h5", "M15 4h5v5", "M20 15v5h-5", "M9 20H4v-5"],
        description="Fills the frame, social-first",
        fragment="a tight crop where the product fills most of the frame, edges cropped "
        "confidently for a bold, scroll-stopping read",
    ),
    _opt(
        "wide-environment",
        "Wide environment",
        ["M3 6h18v12H3z", _circle(15, 12, 2)],
        description="Product in its world, 28 mm",
        fragment="a wider environmental shot with a 28 mm lens, the product clearly placed in the "
        "scene and occupying about a third of the frame",
    ),
    _opt(
        "pov",
        "First-person view",
        [_circle(12, 12, 3), "M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"],
        description="Through the user's eyes",
        fragment="a first-person point-of-view shot, as if through the eyes of the person using "
        "the product, hands entering the frame naturally",
    ),
]

# --- cast --------------------------------------------------------------------------
_REALISM = (
    "natural skin texture, real proportions and relaxed, unposed body language; no stock-photo "
    "smiles, never presented as a real customer or endorser"
)
CAST = [
    _opt(
        "product-only",
        "Product only",
        [_circle(12, 12, 8), "M6.5 6.5l11 11"],
        description="Still life, no people",
        fragment="no people in the frame, a clean still life where the product is the only subject",
        default=True,
    ),
    _opt(
        "hands",
        "Hands",
        [
            "M8 13V5.5a1.5 1.5 0 013 0V11",
            "M11 10V4.5a1.5 1.5 0 013 0V11",
            "M14 10.5V6a1.5 1.5 0 013 0v7c0 4-2.5 7-6 7-2.5 0-4-1.5-5.5-3.5L4.5 14.5",
        ],
        description="In use, faces out of frame",
        fragment=f"a person's hands holding or using the product, face out of frame; {_REALISM}",
    ),
    _opt(
        "model",
        "Solo model",
        PERSON,
        description="One person with the product",
        fragment=f"one person naturally using or wearing the product, the product clearly visible "
        f"and in focus; {_REALISM}",
    ),
    _opt(
        "duo",
        "Duo",
        [
            _circle(8, 8, 3),
            _circle(16, 8, 3),
            "M2 20c1-3 3.5-5 6-5s5 2 6 5",
            "M10 20c1-3 3.5-5 6-5s5 2 6 5",
        ],
        description="Two people, a shared moment",
        fragment=f"two people sharing a moment with the product between them; {_REALISM}",
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
        description="Friends or family together",
        fragment=f"a small group of friends or family enjoying the product together; {_REALISM}",
    ),
    _opt(
        "creator",
        "Creator selfie",
        [
            "M7 2h10a1 1 0 011 1v18a1 1 0 01-1 1H7a1 1 0 01-1-1V3a1 1 0 011-1z",
            _circle(12, 9, 2.5),
            "M9 15c1-1.5 5-1.5 6 0",
        ],
        description="Phone-shot, authentic UGC",
        fragment=f"a creator filming themselves on a phone and showing the product to camera, "
        f"handheld phone-camera look with authentic framing; {_REALISM}",
    ),
]

# --- styling -----------------------------------------------------------------------
VIBES = [
    _opt(
        "everyday",
        "Everyday",
        [_circle(12, 12, 8), "M8 14s1.5 2 4 2 4-2 4-2"],
        description="Relaxed and real",
        fragment="relaxed everyday styling with natural, slightly imperfect details",
        default=True,
    ),
    _opt(
        "elevated-minimal",
        "Elevated minimal",
        ["M5 12h14", "M8 8h8", "M8 16h8"],
        description="Few props, clean lines",
        fragment="elevated minimal styling with very few props, clean lines and calm "
        "negative space",
    ),
    _opt(
        "luxe",
        "Luxe",
        ["M6 3h12l4 6-10 12L2 9z", "M2 9h20"],
        description="Rich materials, refined",
        fragment="refined luxury styling with rich materials such as silk, brass, marble and glass",
    ),
    _opt(
        "active",
        "Active",
        ["M13 2L4 14h7l-1 8 9-12h-7z"],
        description="Energy and movement",
        fragment="energetic, active styling with a sense of movement and momentum",
    ),
    _opt(
        "family",
        "Warm family",
        ["M12 21c-5-3-8-6-8-10a5 5 0 018-4 5 5 0 018 4c0 4-3 7-8 10z"],
        description="Homely, generous, shared",
        fragment="warm, generous family styling that feels homely and shared",
    ),
    _opt(
        "street",
        "Street",
        ["M4 20l4-16", "M20 20L16 4", "M12 6v2", "M12 12v2", "M12 18v2"],
        description="Urban, layered, current",
        fragment="current streetwear-inspired styling, layered and urban with a confident attitude",
    ),
]

# --- moods (several may be chosen) -------------------------------------------------
MOODS = [
    _opt(
        "editorial",
        "Editorial",
        ["M4 5h16", "M4 9h10", "M4 13h16", "M4 17h10"],
        description="Magazine framing, negative space",
        fragment="editorial magazine composition with generous negative space for a headline",
        grade={},
        default=True,
    ),
    _opt(
        "clean",
        "Clean minimal",
        ["M5 12h14"],
        description="Uncluttered and precise",
        fragment="clean, uncluttered and precise",
        grade={"chroma": 0.8},
    ),
    _opt(
        "warm",
        "Warm",
        [_circle(12, 12, 4), SUN_RAYS],
        description="Warm colour temperature",
        fragment="a warm, inviting colour temperature",
        grade={"cb": -6, "cr": 8},
        default=True,
    ),
    _opt(
        "airy",
        "Light and airy",
        ["M4 14h10a4 4 0 100-8", "M4 18h14a3 3 0 110 6", "M4 10h6"],
        description="Bright, soft, fresh",
        fragment="light and airy, lifted shadows and soft pastel highlights",
        grade={"chroma": 0.85, "cb": 2, "cr": 2},
    ),
    _opt(
        "vibrant",
        "Vibrant pop",
        [SPARKLE],
        description="Saturated and punchy",
        fragment="vibrant and punchy, saturated colour and strong contrast",
        grade={"chroma": 1.3},
    ),
    _opt(
        "cinematic",
        "Cinematic",
        ["M4 6h16v12H4z", "M4 10h16", "M8 6v4", "M13 6v4"],
        description="Contrast, shallow depth",
        fragment="cinematic, with filmic contrast, shallow depth of field and a subtle "
        "teal-and-amber grade",
        grade={"chroma": 0.92, "cb": 6, "cr": -4},
    ),
    _opt(
        "moody",
        "Dark and moody",
        ["M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z", "M4 20h16"],
        description="Deep shadows, rich tones",
        fragment="dark and moody, deep shadows and rich, low-key tones",
        grade={"chroma": 0.85, "cb": 4, "cr": 0},
    ),
    _opt(
        "film",
        "Analogue film",
        [
            "M5 5h.01",
            "M12 5h.01",
            "M19 5h.01",
            "M8 10h.01",
            "M16 10h.01",
            "M5 15h.01",
            "M12 15h.01",
            "M19 15h.01",
        ],
        description="Grain, soft highlights",
        fragment="the look of 35 mm analogue film, fine natural grain and gently rolled-off "
        "highlights",
        grade={"chroma": 0.88, "cb": -2, "cr": 3},
    ),
    _opt(
        "natural",
        "Natural and organic",
        LEAF,
        description="Earthy, honest textures",
        fragment="natural and organic, earthy textures and honest, unretouched materials",
        grade={"chroma": 0.92, "cb": -3, "cr": 2},
    ),
    _opt(
        "playful",
        "Playful",
        [_circle(12, 12, 9), "M8 14s1.5 2 4 2 4-2 4-2", "M9 9h.01", "M15 9h.01"],
        description="Bold, witty, unexpected",
        fragment="playful and witty, with a bold, unexpected arrangement",
        grade={"chroma": 1.15},
    ),
]

# --- palettes ----------------------------------------------------------------------
PALETTE_ICON = [
    "M12 3a9 9 0 100 18c1.5 0 2-1 1.5-2.2-.5-1.3.2-2.3 1.5-2.3H17a4 4 0 004-4A9 9 0 0012 3z",
    "M8 10h.01",
    "M12 7h.01",
    "M16 10h.01",
]
PALETTES = [
    _opt(
        "brand",
        "Your brand colours",
        PALETTE_ICON,
        description="From the product and logo",
        colors=["#4A36A0", "#23918E"],
        fragment="the brand's own colours as they appear on the product, its packaging and logo, "
        "used for props and backdrop accents",
        ink="#FFFFFF",
        grade={},
        default=True,
    ),
    _opt(
        "warm-neutrals",
        "Warm neutrals",
        PALETTE_ICON,
        description="Sand, oat, cream",
        colors=["#D9C7A7", "#F3EBDD"],
        fragment="warm neutrals of sand, oat and cream",
        ink="#2B2118",
        grade={"chroma": 0.8, "cb": -4, "cr": 4},
    ),
    _opt(
        "mediterranean",
        "Mediterranean blue",
        PALETTE_ICON,
        description="Cobalt, white, sea-green",
        colors=["#1F5FA8", "#5EC0B8"],
        fragment="Mediterranean cobalt blue, chalk white and sea-green",
        ink="#FFFFFF",
        grade={"cb": 10, "cr": -6},
    ),
    _opt(
        "terracotta",
        "Terracotta sunset",
        PALETTE_ICON,
        description="Burnt orange, sand gold",
        colors=["#C4623A", "#F2C46B"],
        fragment="terracotta, burnt orange and sand gold",
        ink="#FFF4DE",
        grade={"cb": -10, "cr": 12},
    ),
    _opt(
        "olive-sage",
        "Olive and sage",
        PALETTE_ICON,
        description="Greens with natural linen",
        colors=["#6B7A3A", "#C9D2B4"],
        fragment="olive, sage and natural linen",
        ink="#FFFFFF",
        grade={"chroma": 0.9, "cb": -2, "cr": -2},
    ),
    _opt(
        "pastel",
        "Soft pastels",
        PALETTE_ICON,
        description="Blush, lilac, mint",
        colors=["#F4C7D0", "#C9E7DC"],
        fragment="soft pastels of blush, lilac and mint",
        ink="#2E2170",
        grade={"chroma": 0.75, "cb": 2, "cr": 4},
    ),
    _opt(
        "jewel",
        "Jewel tones",
        PALETTE_ICON,
        description="Emerald, sapphire, garnet",
        colors=["#0F6B4F", "#3A2A8C"],
        fragment="deep jewel tones of emerald, sapphire and garnet",
        ink="#FFFFFF",
        grade={"chroma": 1.15, "cb": 4, "cr": 2},
    ),
    _opt(
        "monochrome",
        "Ink and paper",
        PALETTE_ICON,
        description="Restrained monochrome",
        colors=["#1E1745", "#E9E6F2"],
        fragment="a restrained ink-and-paper monochrome",
        ink="#FFFFFF",
        grade={"chroma": 0.35},
    ),
]

# --- languages (locked to FR/EN, L-6) ---------------------------------------------
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
        fragment="Write the headline and caption in natural, idiomatic French as a native "
        "copywriter would, not as a translation.",
        code="fr",
        name="French",
        default=True,
    ),
    _opt(
        "en",
        "EN",
        LANG_ICON,
        description="English",
        fragment="Write the headline and caption in natural, idiomatic English as a native "
        "copywriter would, not as a translation.",
        code="en",
        name="English",
    ),
]

# --- calls to action -----------------------------------------------------------------
#: `short` is the button-length label platforms show; the fragment asks for a
#: closing line that promises nothing the brand has not stated.
CTAS = [
    _opt(
        "shop-now",
        "Shop now",
        ["M5 8h14l-1 12H6L5 8z", "M9 8a3 3 0 016 0"],
        fragment="Close with a clear, friendly call to shop now.",
        short="Shop now",
        default=True,
    ),
    _opt(
        "discover",
        "Discover",
        [_circle(12, 12, 9), "M15.5 8.5l-2 5-5 2 2-5z"],
        fragment="Close by inviting people to discover the product or collection.",
        short="Learn more",
    ),
    _opt(
        "dm-order",
        "DM to order",
        ["M20 12a8 8 0 01-11.8 7L4 20l1-4.2A8 8 0 1120 12z"],
        fragment="Close by inviting people to send a direct message to order.",
        short="Send message",
    ),
    _opt(
        "visit-store",
        "Visit the shop",
        ["M12 21s-7-6.2-7-11a7 7 0 0114 0c0 4.8-7 11-7 11z", _circle(12, 10, 2.5)],
        fragment="Close by inviting people to visit the shop in person.",
        short="Get directions",
    ),
    _opt(
        "book-now",
        "Book now",
        ["M4 6h16v14H4z", "M4 10h16", "M8 3v4", "M16 3v4"],
        fragment="Close with a call to book an appointment or a slot.",
        short="Book now",
    ),
    _opt(
        "see-offer",
        "See the offer",
        ["M20 12l-8 8-9-9V3h8z", "M7.5 7.5h.01"],
        fragment="Close by pointing to the current offer, stated exactly as the brief gives it and "
        "with no invented deadline.",
        short="Get offer",
    ),
    _opt(
        "save-post",
        "Save for later",
        ["M6 3h12v18l-6-4-6 4z"],
        fragment="Close by inviting people to save the post for later.",
        short="Save",
    ),
    _opt(
        "tag-friend",
        "Tag a friend",
        [_circle(12, 12, 4), "M16 12v1.5a2.5 2.5 0 005 0V12a9 9 0 10-3.5 7.1"],
        fragment="Close by inviting people to tag a friend who would love it.",
        short="Comment",
    ),
]

# --- formats -------------------------------------------------------------------------
#: `kind` and `aspect` are binding: a Studio run is refused if its kind does not
#: match the format, and the aspect is taken from here (`ai.serializers`).
FORMATS = [
    _opt(
        "feed-portrait",
        "Feed portrait",
        [FRAME],
        description="Image post, 4:5 — most feed space",
        kind="IMAGE",
        aspect="4:5",
        post_format="FEED",
        default=True,
    ),
    _opt(
        "feed-square",
        "Feed square",
        ["M5 5h14v14H5z"],
        description="Image post, 1:1",
        kind="IMAGE",
        aspect="1:1",
        post_format="FEED",
    ),
    _opt(
        "vertical",
        "Story / Reel cover",
        ["M7 2h10a1 1 0 011 1v18a1 1 0 01-1 1H7a1 1 0 01-1-1V3a1 1 0 011-1z"],
        description="Vertical frame, 9:16",
        kind="IMAGE",
        aspect="9:16",
        post_format="REEL",
    ),
    _opt(
        "landscape",
        "Landscape",
        ["M3 6h18v12H3z"],
        description="Image post, 16:9",
        kind="IMAGE",
        aspect="16:9",
        post_format="FEED",
    ),
    _opt(
        "text-only",
        "Text only",
        ["M4 6h16", "M4 12h16", "M4 18h10"],
        description="Caption, no image",
        kind="TEXT",
        aspect="1:1",
        post_format="FEED",
    ),
]

# --- tempo (how far variants stray) and dynamics (colour intensity) ---------------------
TEMPO_ICON = ["M8 21h8l-2-17h-4z", "M12 14l4-8"]
TEMPOS = [
    _opt(
        "adagio",
        "Adagio",
        TEMPO_ICON,
        description="Faithful to the brief",
        fragment="stay very close to the brief; versions differ only in small details",
    ),
    _opt(
        "andante",
        "Andante",
        TEMPO_ICON,
        description="Gentle variation",
        fragment="vary composition and framing gently between versions",
    ),
    _opt(
        "allegro",
        "Allegro",
        TEMPO_ICON,
        description="Lively variation",
        fragment="make each version clearly distinct in angle, arrangement and styling",
        default=True,
    ),
    _opt(
        "presto",
        "Presto",
        TEMPO_ICON,
        description="Bold exploration",
        fragment="explore boldly; every version takes a different creative route while keeping the "
        "product exactly as it is",
    ),
]

DYN_ICON = ["M3 17V9", "M9 17V5", "M15 17v-6", "M21 17V3"]
DYNAMICS = [
    _opt(
        "pianissimo",
        "pianissimo",
        DYN_ICON,
        fragment="very subdued, desaturated colour",
        grade={"chroma": 0.6},
    ),
    _opt("piano", "piano", DYN_ICON, fragment="soft, gentle colour", grade={"chroma": 0.8}),
    _opt(
        "mezzo-forte",
        "mezzo-forte",
        DYN_ICON,
        fragment="natural, true-to-life colour",
        grade={"chroma": 1.0},
        default=True,
    ),
    _opt("forte", "forte", DYN_ICON, fragment="rich, strong colour", grade={"chroma": 1.2}),
    _opt(
        "fortissimo",
        "fortissimo",
        DYN_ICON,
        fragment="very intense, saturated colour",
        grade={"chroma": 1.45},
    ),
]

# --- voice ---------------------------------------------------------------------------
TONE_ICON = [_circle(12, 12, 9), "M8 14s1.5 2 4 2 4-2 4-2", "M9 9h.01", "M15 9h.01"]
TONES = [
    _opt(
        "warm",
        "Warm and friendly",
        TONE_ICON,
        fragment="Voice: warm, friendly and human, like a trusted shopkeeper.",
        default=True,
    ),
    _opt(
        "playful",
        "Playful",
        TONE_ICON,
        fragment="Voice: playful and light-hearted, with a touch of wit and no forced jokes.",
    ),
    _opt(
        "confident",
        "Bold and confident",
        TONE_ICON,
        fragment="Voice: bold and confident; short, punchy sentences that lead with the benefit.",
    ),
    _opt(
        "refined",
        "Refined",
        TONE_ICON,
        fragment="Voice: refined and understated; few words, chosen carefully, never loud.",
    ),
    _opt(
        "expert",
        "Expert",
        TONE_ICON,
        fragment="Voice: clear and knowledgeable; explain what the product does and why it "
        "matters, using only facts from the brief.",
    ),
]

TOGGLES = [
    _opt(
        "headline_on_image",
        "Headline on image",
        ["M4 7V4h16v3", "M9 20h6", "M12 4v16"],
        description="Set as type over the shot, never painted in",
        default=True,
        fragment="Also write a headline of at most eight words to be set as type over the image; "
        "it must work without the caption.",
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
        description="Your real logo, bottom-right",
        default=True,
    ),
]

# --- presets: one click sets several controls --------------------------------------------
#: `values` maps control -> key (or `moods` -> keys); unknown keys are ignored
#: by the Studio, so a preset survives an operator retiring one of its choices.
PRESETS = [
    _opt(
        "clean-catalogue",
        "Clean catalogue",
        [SPARKLE],
        description="Studio · high-key · product only",
        values={
            "scene": "seamless-studio",
            "light": "high-key",
            "camera": "eye-level",
            "cast": "product-only",
            "vibe": "elevated-minimal",
            "moods": ["clean"],
            "tempo": "adagio",
        },
    ),
    _opt(
        "morning-ritual",
        "Morning ritual",
        [SPARKLE],
        description="Breakfast table · daylight · hands",
        values={
            "scene": "breakfast-table",
            "light": "soft-daylight",
            "camera": "forty-five",
            "cast": "hands",
            "vibe": "everyday",
            "moods": ["warm", "airy"],
            "tempo": "andante",
        },
    ),
    _opt(
        "summer-campaign",
        "Summer campaign",
        [SPARKLE],
        description="Shoreline · golden hour · model",
        values={
            "scene": "beach-shoreline",
            "light": "golden-hour",
            "camera": "three-quarter",
            "cast": "model",
            "vibe": "everyday",
            "moods": ["vibrant", "warm"],
            "tempo": "allegro",
        },
    ),
    _opt(
        "luxe-night",
        "Luxe night",
        [SPARKLE],
        description="Plinth · low-key · low hero",
        values={
            "scene": "stone-plinth",
            "light": "low-key",
            "camera": "low-hero",
            "cast": "product-only",
            "vibe": "luxe",
            "moods": ["moody", "cinematic"],
            "tempo": "andante",
        },
    ),
    _opt(
        "creator-ugc",
        "Creator UGC",
        [SPARKLE],
        description="Living room · selfie · authentic",
        values={
            "scene": "living-room",
            "light": "soft-daylight",
            "camera": "pov",
            "cast": "creator",
            "vibe": "everyday",
            "moods": ["natural"],
            "tempo": "allegro",
        },
    ),
    _opt(
        "flat-lay-feed",
        "Flat-lay feed",
        [SPARKLE],
        description="Botanical · overcast · top-down",
        values={
            "scene": "botanical-set",
            "light": "overcast",
            "camera": "flat-lay",
            "cast": "product-only",
            "vibe": "elevated-minimal",
            "moods": ["airy", "editorial"],
            "tempo": "andante",
        },
    ),
    _opt(
        "ramadan-evenings",
        "Ramadan evenings",
        [SPARKLE],
        description="Rooftop · blue hour · family",
        values={
            "scene": "tunis-rooftop",
            "light": "blue-hour",
            "camera": "three-quarter",
            "cast": "group",
            "vibe": "family",
            "moods": ["warm", "cinematic"],
            "tempo": "andante",
        },
    ),
    _opt(
        "medina-story",
        "Medina story",
        [SPARKLE],
        description="Souk · golden hour · hands · film",
        values={
            "scene": "medina-souk",
            "light": "golden-hour",
            "camera": "close-crop",
            "cast": "hands",
            "vibe": "everyday",
            "moods": ["film", "editorial"],
            "tempo": "presto",
        },
    ),
]

# --- brief quick tags (the occasion a post is for) -------------------------------------
QUICK_TAGS = [
    _opt(key, label, [], fragment=fragment)
    for key, label, fragment in [
        ("launch", "Launch", "the launch of a new product"),
        ("new-season", "New season", "a new season's arrival"),
        ("behind-the-scenes", "Behind the scenes", "a behind-the-scenes look at how it is made"),
        ("how-to", "How to use", "a simple how-to showing the product in use"),
        ("gift-guide", "Gift idea", "a thoughtful gift idea"),
        ("restock", "Back in stock", "the product being back in stock"),
        ("summer", "Summer", "summer days"),
        ("ramadan", "Ramadan", "Ramadan evenings and iftar gatherings"),
        ("eid", "Eid", "Eid celebrations"),
        ("rentree", "Back to school", "the back-to-school rentrée"),
        ("weekend", "Weekend", "a slow weekend moment"),
    ]
]

#: The post editor's "What should change?" chips (`content.services.regeneration`).
REVISE_REASONS = [
    _opt(key, label, [], fragment=fragment)
    for key, label, fragment in [
        (
            "product-changed",
            "Product looks different",
            "keep the product exactly as in the reference photos: same shape, label, colours and "
            "logo",
        ),
        ("product-too-small", "Product too small", "make the product larger and more central"),
        ("wrong-scene", "Wrong scene", "put the product in a different, more fitting scene"),
        ("too-busy", "Too busy", "simplify the background and remove distracting props"),
        ("lighting", "Lighting is off", "improve the lighting so the product reads clearly"),
        ("off-brand-colours", "Off-brand colours", "keep to the brand's own colours"),
        ("people-unnatural", "People look fake", "make people look natural and unposed"),
        ("text-on-image", "Text on image", "keep all text off the image itself"),
        ("caption-tone", "Caption tone", "rework the tone of the caption"),
        ("caption-length", "Caption too long", "make the caption shorter and tighter"),
        ("more-variety", "More variety", "make the versions more varied from each other"),
    ]
]

# --- the product form's choice lists ----------------------------------------------------
#
# Language-neutral on purpose: a brand's own words belong in its product, these
# are only the one-click starting points under each free-text list.

AUDIENCES = [
    _opt(key, label, [])
    for key, label in (
        ("home-cooks", "Home cooks"),
        ("young-professionals", "Young professionals"),
        ("students", "Students"),
        ("young-parents", "Young parents"),
        ("families", "Families"),
        ("seniors", "Seniors"),
        ("gift-buyers", "Gift buyers"),
        ("tourists", "Tourists"),
        ("diaspora", "Diaspora"),
        ("wellness", "Wellness seekers"),
        ("design-lovers", "Design lovers"),
        ("eco-conscious", "Eco-conscious shoppers"),
        ("b2b-buyers", "B2B buyers"),
    )
]

#: `values` are the four voice sliders, 0-100 (`products.brief.TONE_KEYS`).
#: Presets are a starting point the user then moves.
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
    _opt(
        "expert",
        "Trusted expert",
        [],
        description="Clear, factual, reassuring",
        values={"formal": 70, "bold": 45, "modern": 60, "poetic": 15},
    ),
    _opt(
        "bold-modern",
        "Bold modern",
        [],
        description="Direct, confident, current",
        values={"formal": 30, "bold": 90, "modern": 90, "poetic": 25},
    ),
]

#: A claim is only usable once proof is attached (see `products.brief`).
#: `phrases` are what a product-page import looks for (S1), in English and
#: French, matched case-insensitively. A phrase found on a page only *proposes*
#: the claim: it still needs its proof before the orchestra may use it.
CLAIM_PHRASES: dict[str, list[str]] = {
    "organic": ["organic", "certified organic", "biologique", "agriculture biologique"],
    "handmade": [
        "handmade",
        "hand-made",
        "hand made",
        "hand-painted",
        "fait main",
        "fait à la main",
        "peint à la main",
    ],
    "natural": ["100% natural", "all natural", "100% naturel", "100% naturelle"],
    "made-in-tunisia": [
        "made in tunisia",
        "produit de tunisie",
        "fabriqué en tunisie",
        "fait en tunisie",
    ],
    "award-winning": [
        "award-winning",
        "award winning",
        "gold medal",
        "médaille d'or",
        "great taste",
    ],
    "vegan": ["vegan", "végétalien", "végane", "100% végétal"],
    "cruelty-free": ["cruelty-free", "cruelty free", "non testé sur les animaux"],
    "halal": ["halal", "certifié halal"],
    "eco-packaging": [
        "recyclable packaging",
        "recycled packaging",
        "plastic-free",
        "emballage recyclable",
        "emballage recyclé",
        "sans plastique",
    ],
    "dermatologically-tested": [
        "dermatologically tested",
        "dermatologist tested",
        "testé dermatologiquement",
        "testé sous contrôle dermatologique",
    ],
}

CLAIMS = [
    _opt(key, label, [], description=proof, phrases=CLAIM_PHRASES[key])
    for key, label, proof in (
        ("organic", "Organic", "Certificate from an accredited body"),
        ("handmade", "Hand-made", "Workshop photo or statement"),
        ("natural", "100% natural", "Full ingredient list"),
        ("made-in-tunisia", "Made in Tunisia", "Origin certificate or label"),
        ("award-winning", "Award-winning", "Award name and year"),
        ("vegan", "Vegan", "Ingredient list or certification"),
        ("cruelty-free", "Cruelty-free", "Certification or supplier statement"),
        ("halal", "Halal", "Halal certificate"),
        ("eco-packaging", "Eco packaging", "Packaging specification"),
        ("dermatologically-tested", "Dermatologically tested", "Test report"),
    )
]

SHOT_TAGS = [
    _opt(key, label, [])
    for key, label in (
        ("front", "Front"),
        ("side-back", "Side / back"),
        ("detail", "Detail"),
        ("texture", "Texture / material"),
        ("packaging", "Packaging"),
        ("scale", "In hand / scale"),
        ("in-use", "In use"),
        ("lifestyle", "Lifestyle"),
        ("range", "Range / set"),
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
                ("refillable", "Refillable"),
                ("long-lasting", "Built to last"),
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
                ("everyday", "everyday"),
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
                ("number-one", "number one"),
                ("cure", "cure"),
            ),
        ),
        (
            "must_include",
            (
                ("logo", "Logo visible"),
                ("focus", "Product in focus"),
                ("label", "Label readable on the product"),
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
                ("before-after", "Before / after images"),
                ("politics", "Political symbols"),
                ("religious", "Religious symbols"),
                ("pork", "Pork products"),
                ("smoking", "Smoking"),
            ),
        ),
    )
    for slug, label in entries
]

#: kind -> rows. Order within a kind is `sort_order`, assigned by position.
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
