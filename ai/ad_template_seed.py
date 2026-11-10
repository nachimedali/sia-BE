"""The Studio's video ad templates and avatar traits (S17).

The same contract as `ai/creative_seed.py` and `ai/video_seed.py`: data, not
behaviour, and every row an operator can retune in admin. `ai/migrations/
0026_seed_ad_templates` creates them with `get_or_create`, so an edited row is
never overwritten; a new template is a row in admin, with no deploy.

**A template** (`ad_template`) is a short vertical ad shape: what is on screen,
how the camera moves, the beats it plays, and the words it starts from. The
metadata is validated on save (`ai.services.ad_templates.validate_metadata`):

* `category` — the gallery's rail: unbox, hand, tryon, app, talk, interview, fx
* `fits` — the `Product.offer_kind` values it suits; a blank kind fits all
* `person` — `required`, `optional` or `none` (effects only)
* `tier` — `product`, `people` or `talking`: which plan feature unlocks it. A
  person added to a `product` template is a `people` render
* `lengths` — `video_length` keys it renders at; `units` the video units each
  one costs (a unit is about one 10-second clip, `avatar_upgrade/cost_analysis.md`)
* `beats` — the shot strip the person sees, in playing order
* `keyframe_prompt` — the first frame, for the image model; `{product}`,
  `{location}` and `{person}` are filled in by the service
* `motion_prompt` — how the first frame moves, for the video model
* `script` — the starting hook, body and call to action, in English; the person
  edits them, and they become the post's caption
* `hooks`, `locations` — what "Make 3" and "Remix" vary
* `shots` — optional, a multi-shot template (T3): each `{use, text}`, where
  `frame` animates the first frame, `photo` the product's next photo and
  `screen` sets its app screenshot in as a still; `text` is the shot's kinetic
  line (`{product}`, `{hook}`, `{body}`, `{cta}`). The reel closes on an end card
* `needs` — `app_screenshot` when the frame needs one from the product
* `available` — false while the format needs speech (T4): shown, never rendered
* `premium` — true for a premium template: open on the plans with
  `ad_templates_premium`, or to an organization that bought the template pack

Locations are neutral on purpose (`0018_drop_tunisian_scenes`): the brand's
own setting comes from its kit and its product photos.

**Avatar traits** (`avatar_trait`) describe a new invented person: an archetype
(a starting point that sets the others) and four groups of chips. The fragment
is what the portrait prompt says.
"""

from __future__ import annotations

from typing import Any

from ai.creative_seed import SEED_VERSION

_PLAY = ["M8 5v14l11-7z"]

#: The words a place had before places were rows (S17-27). Read only for a key
#: with no `place` row — an operator's own list, or a database this seed's
#: migration left alone — so such a template renders as it always did.
LOCATIONS: dict[str, str] = {
    "studio": "a clean studio set with a soft seamless backdrop",
    "kitchen": "a bright home kitchen counter in morning light",
    "rooftop": "a city rooftop terrace at golden hour",
    "terrace": "a sunlit café terrace with whitewashed walls",
    "market": "a lively covered market aisle with warm lamps",
    "beach": "a quiet beach with soft afternoon light",
    "dunes": "rolling sand dunes under a clear sky",
    "loft": "a loft living room with high windows and a patterned rug",
    "living-room": "a lived-in living room with a sofa, plants and soft lamp light",
    "office": "a busy desk by an office window, papers and a laptop around",
    "workshop": "a small maker's workshop with shelves, tools and daylight",
    "trail": "a rocky hillside trail with dry grass under a cloudy sky",
    "street": "a city street of stone façades and shop windows",
    "gym": "a bright gym with machines and high windows",
    "airport": "an airport terminal with departure boards and big windows",
    "seaside": "a terrace above the sea, green hills and red roofs below",
}

#: Placeholder in a place's `set` filled with the brand kit's primary colour.
BRAND_COLOUR = "{brand_colour}"


def _place(
    key: str,
    label: str,
    description: str,
    *,
    set: str,
    light: str,
    zone: str,
    forbid: str,
    image: str,
) -> dict[str, Any]:
    """A place a template is set in (S17-27, `studio_content_audit.md` §3.1).

    `set` is what the camera sees (never anything with readable text), `light`
    the one light it is locked to unless the person picks their own, `zone`
    where the product stands, `forbid` what the place must never show — added
    to the negative. `image` is the Studio still that pictures it."""
    return {
        "key": key,
        "label": label,
        "description": description,
        "icon_paths": [],
        "colors": [],
        "prompt_fragment": set,
        "metadata": {
            "set": set,
            "light": light,
            "zone": zone,
            "forbid": forbid,
            "image": f"/studio/scene/{image}.webp",
            "seed": SEED_VERSION,
        },
    }


_TABLETOP = "on the surface at the front, lower-middle of the frame, about a quarter of its height"
_NO_TEXT = "no readable text or signs, no other brand's logo"

PLACES: list[dict[str, Any]] = [
    _place(
        "studio",
        "Studio",
        "Seamless backdrop, nothing else",
        set="a clean studio set with a soft seamless backdrop and a low matte plinth",
        light="a large soft key light from the front left, gentle fill, no hard shadows",
        zone="on the plinth, centred, lower-middle of the frame, about a third of its height",
        forbid="no props, no text, no second product",
        image="seamless-studio",
    ),
    _place(
        "brand-colour",
        "Brand colour set",
        "A seamless set in your brand colour",
        set=f"a seamless set in {BRAND_COLOUR} with a matching low plinth",
        light="a soft studio key light from the front left and a thin rim light behind",
        zone="on the plinth, centred, lower-middle of the frame, about a third of its height",
        forbid="no props, no text, no second product",
        image="colour-block",
    ),
    _place(
        "kitchen",
        "Kitchen",
        "Marble counter, morning light",
        set=(
            "a home kitchen counter of pale marble and oiled oak, a ceramic jug and a folded "
            "linen cloth, a white tiled wall behind"
        ),
        light="morning window light from the left, normal exposure",
        zone="on the front third of the counter, lower-middle of the frame",
        forbid="no readable packaging other than the product, no clocks or lit screens",
        image="kitchen-counter",
    ),
    _place(
        "cafe-table",
        "Café table",
        "Marble bistro table, big window",
        set="a round marble bistro table with a small cup and saucer, a quiet café softly behind",
        light="daylight from a big window on the right",
        zone=_TABLETOP,
        forbid="no menus, no logos on cups, " + _NO_TEXT,
        image="breakfast-table",
    ),
    _place(
        "terrace",
        "Café terrace",
        "Whitewashed walls, sunlit table",
        set=(
            "a sunlit café terrace with whitewashed walls, a small round metal table and a blue "
            "shutter"
        ),
        light="bright Mediterranean sun from the right, crisp soft-edged shadows",
        zone="on the table, front and centre",
        forbid="no menus, " + _NO_TEXT,
        image="garden-terrace",
    ),
    _place(
        "rooftop",
        "Rooftop",
        "Whitewashed terrace, late sun",
        set=(
            "a whitewashed rooftop terrace with a low parapet and a potted olive tree, the city "
            "far below"
        ),
        light="low late-afternoon sun from the left, long soft shadows",
        zone="on the parapet ledge, lower-middle of the frame",
        forbid="no billboards, " + _NO_TEXT,
        image="garden-terrace",
    ),
    _place(
        "seaside",
        "Seaside terrace",
        "Above the sea, red roofs below",
        set="a terrace above the sea with a low stone wall, green hills and red roofs below",
        light="golden late-afternoon sun from the right",
        zone="on the stone wall, lower-middle of the frame",
        forbid="no boats with names, " + _NO_TEXT,
        image="water-reflection",
    ),
    _place(
        "beach",
        "Beach",
        "Straw mat on fine sand, in shade",
        set=(
            "a straw mat on fine pale sand with a folded towel beside it, the sea a calm band of "
            "colour behind"
        ),
        light="late-afternoon side light, the product in soft shade",
        zone="on the mat, lower-middle of the frame",
        forbid="no crowds, no branded umbrellas, " + _NO_TEXT,
        image="beach-shoreline",
    ),
    _place(
        "picnic",
        "Picnic",
        "Blanket on grass, dappled shade",
        set="a checked blanket on short grass in dappled shade, a wicker basket and two glasses",
        light="dappled afternoon sun through leaves",
        zone="on the blanket, front and centre",
        forbid="no crowds, no other brand's bottles, " + _NO_TEXT,
        image="botanical-set",
    ),
    _place(
        "market",
        "Market stall",
        "A quiet stall corner, warm lamp",
        set=(
            "a quiet corner of a covered market stall: wooden crates, woven baskets and a linen "
            "cloth"
        ),
        light="a warm hanging lamp from the right, soft falloff",
        zone="on the cloth at the front of the stall",
        forbid="no crowds, at most one vendor's hands, no price tags, " + _NO_TEXT,
        image="breakfast-table",
    ),
    _place(
        "shop-counter",
        "Shop counter",
        "Your counter, ready to serve",
        set=(
            "a small shop counter of pale wood with a paper bag, a ribbon roll and a card reader "
            "turned away"
        ),
        light="warm shop light from above and soft daylight from the door on the left",
        zone="on the counter, front and centre",
        forbid="no lit screens, no price labels, no shop names, " + _NO_TEXT,
        image="breakfast-table",
    ),
    _place(
        "living-room",
        "Living room",
        "Sofa, plants, lamp light",
        set="a lived-in living room with a linen sofa, a wool rug, a stack of books and a plant",
        light="warm lamp light from the right with soft window fill",
        zone="on the coffee table, lower-middle of the frame",
        forbid="no lit TV screen, no readable book covers, " + _NO_TEXT,
        image="living-room",
    ),
    _place(
        "loft",
        "Loft",
        "High windows, patterned rug",
        set=(
            "a loft living room with high steel windows, a patterned wool rug and an open-tread "
            "staircase"
        ),
        light="soft daylight from the high windows on the left",
        zone="on the rug or a low table, lower third of the frame",
        forbid="no lit screens, no posters, " + _NO_TEXT,
        image="living-room",
    ),
    _place(
        "entryway",
        "Entryway",
        "Door, mat and a delivery box",
        set=(
            "a home entryway with the front door ajar, a woven doormat and a plain shipping box "
            "on the floor"
        ),
        light="daylight from the open door on the left",
        zone="on or beside the box, lower third of the frame",
        forbid="no shipping labels, no courier logos, " + _NO_TEXT,
        image="living-room",
    ),
    _place(
        "bedside",
        "Bedside",
        "Night table, lamp glow",
        set="a bedside table with a linen lamp, a book face down and a glass of water",
        light="warm lamp light from the left, the room dim around it",
        zone="on the table beside the lamp",
        forbid="no lit phone screens, no readable book covers, " + _NO_TEXT,
        image="living-room",
    ),
    _place(
        "bathroom-shelf",
        "Bathroom shelf",
        "Honed stone, folded towels",
        set="a bathroom shelf of honed stone beside a basin, folded towels and a small plant",
        light="soft daylight from a frosted window on the left",
        zone="on the shelf, front and centre",
        forbid="no other brand's bottles facing the camera, no mirror showing the camera",
        image="bathroom-vanity",
    ),
    _place(
        "salon",
        "Salon chair",
        "Mirror, chair, soft light",
        set="a calm salon station with a leather chair, a large mirror and a trolley of brushes",
        light="soft even light from bulbs around the mirror, no glare",
        zone="on the trolley or in hand, mid-frame",
        forbid="no reflection of the camera, no other product brands, " + _NO_TEXT,
        image="bathroom-vanity",
    ),
    _place(
        "office",
        "Desk",
        "A desk by the window",
        set="a tidy desk by an office window with papers, a notebook and a closed laptop",
        light="daylight from the window on the left",
        zone="on the desk, front right, about a quarter of the frame height",
        forbid="no readable documents, no screen content, " + _NO_TEXT,
        image="work-desk",
    ),
    _place(
        "workshop",
        "Workshop",
        "Maker's bench, tools, daylight",
        set=(
            "a small maker's workshop: a worn wooden bench, hand tools on a pegboard, wood shavings"
        ),
        light="daylight from a side window on the left",
        zone="on the bench, front and centre",
        forbid="no power-tool brand logos, " + _NO_TEXT,
        image="work-desk",
    ),
    _place(
        "gym",
        "Gym",
        "Bright gym, high windows",
        set="a bright gym with a padded bench, a rack of dumbbells and high windows",
        light="daylight from the high windows, even fill",
        zone="on the bench or in hand, mid-frame",
        forbid="no machine brand logos, no lit screens, no mirror showing the camera",
        image="colour-block",
    ),
    _place(
        "street",
        "Street",
        "Stone façades, empty pavement",
        set=(
            "an empty stone pavement in front of a shop window with no lettering, early morning "
            "in a city street"
        ),
        light="early-morning side light from the left",
        zone="carried or held at chest height, mid-frame",
        forbid="no passers-by, no shop names, no cars with plates, " + _NO_TEXT,
        image="city-street",
    ),
    _place(
        "trail",
        "Hillside trail",
        "Rocky path, dry grass, cloud",
        set=("a rocky hillside trail with dry grass and a few low shrubs under a soft cloudy sky"),
        light="soft overcast daylight, even and natural",
        zone="held in hand or on a flat rock, mid-frame",
        forbid="no other hikers, no signposts, " + _NO_TEXT,
        image="desert-dunes",
    ),
    _place(
        "car-interior",
        "Car",
        "Front seats, daylight",
        set="the front seats of a parked car, fabric seats, the dashboard edge in view",
        light="soft daylight through the windscreen",
        zone="held at chest height or on the passenger seat",
        forbid="no car brand badges, no lit screens, no number plates, " + _NO_TEXT,
        image="city-street",
    ),
]

_NEGATIVE = (
    "no resemblance to any real or famous person, no brand logos except the product's own, "
    "no readable text except the product's own, no extra fingers, no warped hands, no product "
    "redesign, no watermark"
)

#: How a template is filmed (S17-26, `studio_content_audit.md` §3.1): the words
#: that end its first frame, and what that capture must never look like, added
#: to the negative. A phone video and a studio hero shot fail in different ways;
#: one finish for all asked a levitating product to look like a phone video.
CAPTURES: dict[str, dict[str, str]] = {
    "phone_ugc": {
        "finish": (
            "Shot on a phone's main camera: deep focus with no background blur, one soft "
            "light from a window or lamp to one side, normal exposure, natural matte skin; "
            "authentic and unpolished, like a real social video."
        ),
        "avoid": (
            "no waxy or airbrushed skin, no beauty-filter smoothing, no HDR look, no "
            "over-sharpening, no studio lighting, no portrait blur, no stock-photo smile, "
            "no third hand"
        ),
    },
    "studio_hero": {
        "finish": (
            "A clean commercial product photograph: controlled studio light, the product "
            "tack-sharp, a simple softly graded background, crisp natural reflections."
        ),
        "avoid": (
            "no dust or fingerprints, no blown-out highlight across the label, no cluttered "
            "props, no duplicate product"
        ),
    },
    "macro_tabletop": {
        "finish": (
            "Tactile macro tabletop photography: one plane of sharp focus on the texture, "
            "warm side light, real materials, true-to-life colour."
        ),
        "avoid": (
            "no plastic-looking surfaces or food, no oversaturated colour, no liquid, "
            "crumbs or powder floating without a visible cause"
        ),
    },
    "device_screen": {
        "finish": (
            "A real phone held in a real hand, the screen facing the camera and evenly lit, "
            "deep focus, natural light."
        ),
        "avoid": "no invented app screens, no made-up interface text, no warped or cracked phone",
    },
}


#: What an ad is for (S17-32): the gallery's goal chips, and how the gallery
#: recommends for a product's campaign goal.
GOALS = ("sell", "explain", "show", "trust")
_GOAL_BY_CATEGORY = {
    "text": "explain",
    "unbox": "show",
    "hand": "show",
    "tryon": "show",
    "use": "show",
    "app": "explain",
    "tour": "show",
    "maker": "trust",
    "promo": "sell",
    "fx": "show",
    "talk": "trust",
    "interview": "trust",
}
_GOAL_OVERRIDE = {
    "value-stack": "explain",
    "offer-drop": "sell",
    "lineup": "sell",
    "before-after": "explain",
    "swatch-test": "explain",
    "three-reasons": "explain",
    "myth-vs-fact": "explain",
    "quick-tip": "explain",
    "pour-shot": "explain",
    "bag-check": "trust",
    "proof-card": "trust",
}
#: Talking formats that cannot be made yet and are not shown (S17-32): three
#: stay as "soon" (founder story, three reasons, comment reply).
_HIDDEN = frozenset(
    {
        "problem-to-app",
        "myth-vs-fact",
        "quick-tip",
        "customer-letter",
        "street-question",
        "two-chairs",
        "expert-take",
        "investigation",
        "underdog",
        "green-screen",
        "car-chat",
    }
)
#: The one detail that kills each idea if lost (goose-skills `ideas.md` §2 E):
#: sent with the first frame, word for word, and the first thing to check.
_PROTECT: dict[str, str] = {
    "slow-unbox": (
        "The lid is only just lifting: the product inside is half revealed, never fully out."
    ),
    "gift-reveal": "One hand holds the ribbon end, mid-pull: the wrapping is still closed.",
    "first-look": "The box is still sealed and held toward the camera: nothing is open yet.",
    "pour-shot": "The stream comes from the tilted product into the glass, never from nowhere.",
    "market-stroll": "The hand is reaching for the product on the stall, not already holding it.",
    "hand-turn": "Only one hand, holding the product at eye level with the label square to camera.",
    "morning-ritual": "The product is in the person's hand at the moment of use, not on a shelf.",
    "bag-check": "The bag is open and the hand is going in: the product is not out yet.",
    "mirror-try-on": "The mirror shows the person's reflection, never the camera or the phone.",
    "before-after": "The same hands and the same light in both halves; only the method differs.",
    "get-ready": "The product is the one in the person's hand; the others stay in the background.",
    "tap-through": "The phone screen faces the camera, flat and evenly lit, nothing covering it.",
    "feature-in-15": (
        "The screen is readable and the hand is about to lift the phone, not holding it yet."
    ),
    "problem-to-app": "The phone screen with the app is turned toward the lens.",
    "founder-story": "The person speaks to the camera with the product clearly beside them.",
    "three-reasons": "The person speaks to the camera with the product clearly beside them.",
    "myth-vs-fact": "The person speaks to the camera with the product clearly beside them.",
    "quick-tip": "The product is in the person's hands while they show the tip.",
    "customer-letter": (
        "The person speaks to the camera; the product is beside them, label visible."
    ),
    "street-question": "The microphone points at the passer-by, who holds the product.",
    "two-chairs": "Two people face each other; the product sits between them, label to camera.",
    "app-walkthrough": "The phone is held toward the camera with its screen fully visible.",
    "space-tour": (
        "The doors are open and light comes in: the space is shown wide, empty of people."
    ),
    "liquid-splash": (
        "The product stands upright and unchanged; the liquid moves around it, never on it."
    ),
    "levitate": "The product hovers just above the plinth, upright, with its shadow beneath it.",
    "stair-unbox": "The person sits on the stairs with the box on their knees, the lid just open.",
    "doorstep-delivery": (
        "The parcel is still taped on the doormat; the key is about to cut the tape."
    ),
    "close-detail": (
        "The texture and the label are sharp; the hand holds it close, nothing else in focus."
    ),
    "trail-test": "The product is held up beside the person's face, the trail behind them.",
    "street-carry": "The person walks toward the camera with the product in plain view.",
    "flat-lay": (
        "The product sits in the empty space in the middle; everything else stays around it."
    ),
    "seaside-wear": "The person wears the product while looking out at the sea, not at the camera.",
    "beach-day": "Seen from above: the product is worn or held, the towel flat beneath.",
    "floor-read": "The camera is on the floor looking up; the product is in use, not posed.",
    "desk-break": "The work is pushed aside; the product is in hand at the moment of the break.",
    "made-by-hand": "The hands are finishing the product itself, with real tools, on the bench.",
    "packing-order": "The handwritten note sits beside the product in the open box.",
    "room-reveal": "The product sits where it is really used, part of the room, not on display.",
    "gate-app": "The phone screen faces the camera, readable, held steady while on the move.",
    "gym-tablet": "The tablet screen faces the camera, readable, the gym behind it.",
    "reaction-reveal": "The person looks just out of frame at something we have not seen yet.",
    "reaction-demo": "The person looks at their phone; the screen is not shown in this frame.",
    "value-stack": "The product alone, upright and centred, the label square to camera.",
    "offer-drop": "The product alone, upright and centred, the label square to camera.",
    "lineup": "Every variant stands in one row, labels to camera, none hiding another.",
    "hero-orbit": "The product alone on the plinth, upright, the reflection beneath it unbroken.",
    "macro-sizzle": "The crumbs and texture come from the product itself, broken by the hands.",
    "swatch-test": "One stroke of the product on the skin, the product held just above it.",
    "expert-take": "The expert holds the product in their own workplace, not a studio.",
    "investigation": "The person holds the product up to the selfie camera while on the move.",
    "underdog": "The founder sits in their real workspace with the product beside them.",
    "green-screen": "The person is in the lower half; the product's photo fills the space above.",
    "comment-reply": "The person speaks to the phone camera with the product in hand.",
    "car-chat": (
        "Two people in the front seats, the product between them, the dashboard camera wide."
    ),
    # S17-33: graphic formats, drawn in code around the product's real photos.
    "offer-ad": "The offer card states the real offer and its end date; nothing is invented.",
    "value-prop": "Each benefit is one short line next to the real product, one per shot.",
    "variant-swipe": "Each variant is the real product cut out on its own colour, one per shot.",
    "mosaic-reveal": "Every tile is one of the product's real photos, none generated.",
    "myth-fact": "The myth is struck through and the fact answers it, both in plain words.",
    "chat-reveal": "Plain chat bubbles only: no phone, no app chrome, no notification.",
    "shop-tour": "The places are the brand's own photos of its own space.",
    "ramadan-offer": "The offer card states the real Ramadan offer and its end date.",
    "eid-offer": "The offer card states the real Eid offer and its end date.",
    "back-to-school": "The offer card states the real back-to-school offer and its end date.",
    "summer-sale": "The offer card states the real summer offer and its end date.",
    "black-friday": "The offer card states the real Black Friday offer and its end date.",
    "proof-card": (
        "The quote is a real customer's own words, shown only with their consent, never rewritten."
    ),
}


def _capture(category: str, person: str) -> str:
    """The capture a template is filmed in unless it names its own: effects
    and offers are studio shots, a phone screen with no presenter is a device
    shot, everything else is a phone video."""
    if category in ("fx", "promo"):
        return "studio_hero"
    if category == "app" and person != "required":
        return "device_screen"
    return "phone_ugc"


_CLIP = ("clip-5", "clip-10", "clip-15")
_UNITS = {"clip-5": 1, "clip-10": 1, "clip-15": 2}


def _units(length: str, tier: str, category: str) -> int:
    """Talking formats carry speech and lip sync (T4): 3 units, and 5 for an
    interview, which has two people on screen."""
    if category == "interview":
        return 5
    return 3 if tier == "talking" else _UNITS[length]


def _template(
    key: str,
    label: str,
    category: str,
    fits: tuple[str, ...],
    person: str,
    tier: str,
    *,
    lengths: tuple[str, ...],
    beats: tuple[str, ...],
    frame: str,
    motion: str,
    script: tuple[str, str, str],
    hooks: tuple[str, ...],
    locations: tuple[str, ...],
    description: str = "",
    needs: str = "",
    available: bool = True,
    scene: str = "",
    sort: int = 0,
    shots: tuple[tuple[str, str] | tuple[str, str, dict[str, Any]], ...] = (),
    premium: bool = False,
    capture: str = "",
    land: bool = False,
    transition: str = "",
    script_hint: str = "",
) -> dict[str, Any]:
    person_line = {
        "required": " {person} is in the frame, natural and unposed.",
        "optional": "{person_optional}",
        "none": " No person in frame; hands only where the action needs them.",
    }[person]
    capture = capture or _capture(category, person)
    keyframe = (
        f"Vertical 9:16 photo for a short social ad. {frame} Setting: {{location}}."
        f"{person_line} The {{product}} is clearly visible and exactly as in the reference "
        f"photos: same shape, label and colours. {CAPTURES[capture]['finish']}"
    )
    return {
        "key": key,
        "label": label,
        "description": description or " · ".join(beats),
        "icon_paths": _PLAY,
        "colors": [],
        "prompt_fragment": "",
        "sort_order": sort,
        "metadata": {
            "category": category,
            "fits": list(fits),
            "person": person,
            "tier": tier,
            "lengths": list(lengths),
            "units": {length: _units(length, tier, category) for length in lengths},
            "beats": list(beats),
            "keyframe_prompt": keyframe,
            "motion_prompt": f"{motion} The product never changes shape, label or colour.",
            "capture": capture,
            # S17-32: what it is for, the detail that must not be lost, and
            # whether the gallery shows it at all.
            "goal": _GOAL_OVERRIDE.get(key, _GOAL_BY_CATEGORY[category]),
            "protect": _PROTECT[key],
            **({"hidden": True} if key in _HIDDEN else {}),
            # S17-31: filmed towards its first frame, which the clip ends on.
            **({"land": True} if land else {}),
            "negative": f"{_NEGATIVE}, {CAPTURES[capture]['avoid']}",
            "script": {"hook": script[0], "body": script[1], "cta": script[2]},
            "hooks": list(hooks),
            "locations": list(locations),
            "needs": needs,
            "available": available,
            "premium": premium,
            "scene": scene,
            "seed": SEED_VERSION,
            **(
                {
                    "shots": [
                        {"use": shot[0], "text": shot[1], **(shot[2] if len(shot) > 2 else {})}
                        for shot in shots
                    ]
                }
                if shots
                else {}
            ),
            # S17-33: how the shots are joined, and what the script writer
            # must know about this format's words.
            **({"transition": transition} if transition else {}),
            **({"script_hint": script_hint} if script_hint else {}),
        },
    }


_TALK = "Talking formats need a voice and lip sync, which arrive with the next Studio update."

TEMPLATES: list[dict[str, Any]] = [
    # --- unboxing -------------------------------------------------------------
    _template(
        "slow-unbox",
        "Slow unbox",
        "unbox",
        ("physical", "beauty"),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("Box on the table", "Lid lifts", "Product in light", "End card"),
        frame="Close, eye-level shot of a closed gift box on a table, the lid just lifting.",
        motion="Slow push-in as the lid lifts and light falls on the product inside.",
        script=("Wait for it.", "{product}, unboxed the way it deserves.", "Order yours today."),
        hooks=("Wait for it.", "Turn the sound on.", "Guess what's inside."),
        locations=("studio", "kitchen", "rooftop", "entryway"),
        scene="vs7",
        sort=10,
    ),
    _template(
        "gift-reveal",
        "Gift reveal",
        "unbox",
        ("physical", "beauty"),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("Ribbon pull", "Reveal", "Close-up", "End card"),
        frame="A wrapped present with a ribbon, a hand about to pull it.",
        motion=(
            "The ribbon slides away and the wrapping opens to reveal the product, then a "
            "gentle push-in."
        ),
        script=(
            "The gift they'll actually use.",
            "{product}, wrapped and ready.",
            "Gift it today.",
        ),
        hooks=(
            "The gift they'll actually use.",
            "Stop overthinking the gift.",
            "Wrapped in ten seconds.",
        ),
        locations=("studio", "rooftop", "terrace", "shop-counter"),
        sort=11,
    ),
    _template(
        "first-look",
        "First look",
        "unbox",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Hello to camera", "Open it", "Reaction", "Call to action"),
        frame="Medium shot of a person at a table holding the unopened box toward the camera.",
        motion="Handheld feel: the person opens the box, lifts the product and smiles at it.",
        script=(
            "First look at {product}.",
            "Opening {product} for the first time: every detail, up close.",
            "Link to shop.",
        ),
        hooks=("First look at {product}.", "Fresh out of the box.", "Unboxing it live."),
        locations=("terrace", "kitchen", "studio", "entryway"),
        scene="vs7",
        sort=13,
    ),
    # --- in hand --------------------------------------------------------------
    _template(
        "pour-shot",
        "Pour proof",
        "hand",
        ("physical",),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("Hand enters", "The pour", "Close-up", "End card"),
        frame=(
            "A hand tilting the product over a clear glass or bowl, the first drop about to "
            "fall, the label turned to the camera."
        ),
        motion=(
            "The hand tilts the product and it pours in one steady stream, the texture and "
            "colour catching the light; the camera creeps in on the label as the pour settles."
        ),
        script=("This is the pour.", "{product}, poured fresh.", "Taste it yourself."),
        hooks=("This is the pour.", "Watch the colour.", "Slow it down."),
        locations=("kitchen", "cafe-table", "terrace", "picnic", "beach"),
        scene="re1-1",
        sort=20,
    ),
    _template(
        "market-stroll",
        "Market stroll",
        "hand",
        ("physical",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Walk-in", "Pick it up", "Show the label", "Call to action"),
        frame="A person walking through a market aisle, reaching for the product on a stall.",
        motion=(
            "Gentle tracking shot: the person picks the product up and turns the label to camera."
        ),
        script=(
            "Watch this.",
            "Our {product}, the way it should be: simple and real.",
            "Find it near you.",
        ),
        hooks=("Watch this.", "Found at the market.", "Straight to this stall."),
        locations=("market", "terrace", "rooftop"),
        scene="vs5",
        sort=21,
    ),
    _template(
        "hand-turn",
        "Hand turn 360",
        "hand",
        ("physical", "beauty"),
        "optional",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Front", "Turn", "Back label", "End card"),
        frame=(
            "A hand holding the product at eye level against a clean background, label to camera."
        ),
        motion="The hand turns the product a full half-turn and back, showing every side.",
        script=("Every side of it.", "{product}, from every angle.", "See it in person."),
        hooks=("Every side of it.", "Turn it around.", "Look closer."),
        locations=("studio", "brand-colour", "kitchen"),
        capture="studio_hero",
        scene="re2-2",
        sort=23,
    ),
    _template(
        "morning-ritual",
        "Morning ritual",
        "hand",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Wake-up light", "The ritual", "Product close", "Call to action"),
        frame="Soft morning light, a person starting their routine with the product in hand.",
        motion="Slow handheld drift following the person's hands through the routine.",
        script=("My morning, ten seconds.", "It starts with {product}.", "Start yours."),
        hooks=("My morning, ten seconds.", "Before coffee, this.", "Routine check."),
        locations=("kitchen", "bathroom-shelf", "bedside", "terrace"),
        sort=24,
    ),
    _template(
        "bag-check",
        "Bag check",
        "hand",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("What's in my bag", "Pull it out", "Why", "Call to action"),
        frame="A person with an open bag, looking at the camera, about to reach in.",
        motion="Quick reach into the bag, the product comes out and is held up to camera.",
        script=("What's in my bag?", "{product}, always.", "Get yours."),
        hooks=("What's in my bag?", "Always in the bag.", "Bag essentials."),
        locations=("street", "cafe-table", "rooftop", "terrace", "market"),
        sort=25,
    ),
    # --- try-on ---------------------------------------------------------------
    _template(
        "mirror-try-on",
        "Mirror try-on",
        "tryon",
        ("beauty",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Mirror wide", "Apply", "Result", "Call to action"),
        frame="A person in front of a mirror holding the product, the reflection visible.",
        motion="Slow push-in as the person applies or wears the product and checks the result.",
        script=("Mirror check.", "Trying {product} for the first time.", "Try it yourself."),
        hooks=("Mirror check.", "Let's try it.", "Before I go out."),
        locations=("bathroom-shelf", "salon", "studio"),
        scene="vs3",
        sort=30,
    ),
    _template(
        "before-after",
        "Old way, new way",
        "tryon",
        ("beauty",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("The usual way", "The new way", "Side by side", "End card"),
        frame=(
            "A split frame: on the left the usual way of doing the task, on the right the same "
            "task done with the product, the same hands and light in both halves."
        ),
        motion=(
            "Both halves play side by side: the left slow and fiddly, the right quick and clean "
            "with the product; a gentle push-in on the right half."
        ),
        script=("The usual way.", "The {product} way.", "Try it yourself."),
        hooks=("The usual way.", "Same task, two ways.", "Step by step."),
        locations=("bathroom-shelf", "salon", "studio"),
        scene="re1-5",
        sort=31,
    ),
    _template(
        "get-ready",
        "Get ready with me",
        "tryon",
        ("beauty",),
        "required",
        "people",
        lengths=("clip-15",),
        beats=("Hello", "Step 1", "Step 2", "Final look"),
        frame="A person at a vanity, the product among a few others, smiling at the camera.",
        motion="Handheld front-camera feel, the person moves through two steps with the product.",
        script=("Get ready with me.", "Step one, always {product}.", "Shop the look."),
        hooks=("Get ready with me.", "Five-minute routine.", "Tonight's look."),
        locations=("bathroom-shelf", "salon", "studio", "terrace"),
        scene="re3-1",
        sort=32,
    ),
    # --- app demo -------------------------------------------------------------
    _template(
        "tap-through",
        "Tap-through",
        "app",
        ("app",),
        "none",
        "product",
        lengths=_CLIP,
        beats=("Home screen", "Tap", "Result", "Store badge"),
        frame="A hand holding a phone toward the camera, the app's screen clearly visible.",
        motion="A thumb taps the screen once, the phone tilts slightly toward the camera.",
        script=("Two taps.", "That's all {product} needs.", "Download it free."),
        hooks=("Two taps.", "Watch how fast.", "This app does it for you."),
        locations=("studio", "cafe-table", "terrace"),
        needs="app_screenshot",
        scene="vs11",
        sort=40,
    ),
    _template(
        "feature-in-15",
        "Feature in 15",
        "app",
        ("app",),
        "optional",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Problem", "Feature", "Proof", "Download"),
        frame="A phone on a desk showing the app, a hand about to pick it up.",
        motion=(
            "The hand lifts the phone into frame and scrolls once, a slow push-in on the screen."
        ),
        script=("One feature, fifteen seconds.", "{product} does this better.", "Try it today."),
        hooks=(
            "One feature, fifteen seconds.",
            "You're doing it the slow way.",
            "Here's the trick.",
        ),
        locations=("studio", "office", "rooftop"),
        needs="app_screenshot",
        scene="vs8",
        sort=41,
    ),
    _template(
        "problem-to-app",
        "Problem → app",
        "app",
        ("app",),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The problem", "Here's the app", "How it works", "Download"),
        frame="A person looking at the camera, holding a phone with the app open.",
        motion="The person talks to camera, then turns the phone screen toward the lens.",
        script=("Still doing it by hand?", "{product} fixes it in a minute.", "Download it free."),
        hooks=("Still doing it by hand?", "The slow way, or this.", "Stop doing it by hand."),
        locations=("rooftop", "studio"),
        available=False,
        description=_TALK,
        scene="re4-4",
        sort=42,
    ),
    # --- talking --------------------------------------------------------------
    *[
        _template(
            key,
            label,
            "talk",
            ("physical", "beauty", "app"),
            "required",
            "talking",
            lengths=("clip-15",),
            beats=beats,
            frame=(
                "A person speaking to the camera at eye level, the product in hand or beside them."
            ),
            motion="Static front camera, natural gestures, a punch-in for the last line.",
            script=script,
            hooks=(script[0],),
            locations=("studio", "terrace", "market"),
            available=False,
            description=_TALK,
            scene=scene,
            sort=sort,
        )
        for key, label, beats, script, scene, sort in (
            (
                "founder-story",
                "Founder story",
                ("Who I am", "Why we make it", "The product", "Call to action"),
                ("I started this for a reason.", "Why we make {product} the way we do.", "Try it."),
                "vs1",
                50,
            ),
            (
                "three-reasons",
                "3 reasons",
                ("Hook", "Reason 1", "Reason 2", "Reason 3"),
                ("Three reasons.", "Why people switch to {product}.", "See for yourself."),
                "re2-4",
                51,
            ),
            (
                "myth-vs-fact",
                "Myth vs fact",
                ("The myth", "The fact", "Proof", "Call to action"),
                ("Myth.", "Here's the fact about {product}.", "Learn more."),
                "vs10",
                52,
            ),
            (
                "quick-tip",
                "Quick tip",
                ("Tip", "Show", "Why it works", "Call to action"),
                ("Quick tip.", "How to get more from {product}.", "Save this."),
                "re1-1",
                53,
            ),
            (
                "customer-letter",
                "Customer letter",
                ("The message", "Reading it", "Our answer", "Call to action"),
                (
                    "The question we hear most.",
                    "What people ask about {product}, answered.",
                    "Ask us yours.",
                ),
                "re2-4",
                54,
            ),
        )
    ],
    # --- interview ------------------------------------------------------------
    _template(
        "street-question",
        "Street question",
        "interview",
        ("physical", "beauty", "app"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The question", "Answer", "Reveal", "Call to action"),
        frame="Street interview: a handheld mic toward a passer-by holding the product.",
        motion="Handheld street camera, a quick question and answer, a punch-in on the product.",
        script=("Quick question.", "Have you tried {product}?", "Find it near you."),
        hooks=("Quick question.", "Can I ask you something?", "Street test."),
        locations=("market", "rooftop"),
        available=False,
        description=_TALK,
        scene="vs4",
        sort=60,
    ),
    _template(
        "two-chairs",
        "Two chairs",
        "interview",
        ("physical", "beauty", "app"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("Question", "Answer", "Follow-up", "Close"),
        frame=(
            "Two people in chairs facing each other, studio microphones, the product on a "
            "side table."
        ),
        motion="Cut between two medium shots, then a close-up on the product.",
        script=("Let's talk about it.", "Why {product} works.", "Listen to the full story."),
        hooks=("Let's talk about it.", "Honest conversation.", "The real review."),
        locations=("studio",),
        available=False,
        description=_TALK,
        scene="re1-3",
        sort=61,
    ),
    # --- multi-shot (T3): a reel of clips ---------------------------------------
    _template(
        "app-walkthrough",
        "App walkthrough",
        "app",
        ("app",),
        "optional",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Phone in hand", "The screen", "The result", "Download"),
        frame="A hand holding a phone toward the camera in natural light.",
        motion="The hand brings the phone closer, a slow push-in, the thumb ready to tap.",
        script=("Watch this.", "{product}, in three taps.", "Download it free."),
        hooks=("Watch this.", "Three taps, done.", "You'll want this app."),
        locations=("studio", "terrace"),
        needs="app_screenshot",
        sort=42,
        shots=(("frame", "{hook}"), ("screen", "{product}"), ("screen", "Done in seconds")),
    ),
    _template(
        "space-tour",
        "Space tour",
        "tour",
        ("space", "service"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Arrive", "Walk through", "Details", "Book"),
        frame="A wide view of the space, doors open, light coming in.",
        motion=(
            "A slow, steady gimbal glide forward through the space with gentle parallax; "
            "nothing in the room moves or changes, no people appear."
        ),
        script=(
            "Step inside {product}.",
            "Light, space and every detail ready for you.",
            "Book your visit.",
        ),
        hooks=("Step inside {product}.", "Take the tour.", "This is where it happens."),
        locations=("studio",),
        sort=45,
        shots=(("photo", "{hook}"), ("photo", ""), ("photo", "")),
    ),
    # --- effects --------------------------------------------------------------
    _template(
        "liquid-splash",
        "Liquid splash",
        "fx",
        ("physical",),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Drop", "Splash", "Hero", "End card"),
        frame="The product standing alone, hero-lit, droplets suspended around it.",
        motion=(
            "A splash of liquid rises around the product in slow motion and settles, ending "
            "exactly as in this frame with the droplets hanging still around it."
        ),
        land=True,
        script=("Fresh.", "{product}, at its best.", "Taste it now."),
        hooks=("Fresh.", "Splash.", "Pure."),
        locations=("studio", "brand-colour", "beach"),
        sort=70,
    ),
    _template(
        "levitate",
        "Levitate",
        "fx",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Rest", "Lift", "Float", "End card"),
        frame="The product resting on a plinth, soft rim light.",
        motion=(
            "The product drifts down through the air, turning slowly, and comes to rest on the "
            "plinth exactly as in this frame."
        ),
        land=True,
        script=("Light.", "{product}, weightless.", "Discover it."),
        hooks=("Light.", "Gravity optional.", "Float."),
        locations=("studio", "brand-colour"),
        sort=71,
    ),
    # --- added 2026-10-27: closer to the reference scenes (`ad-templates/`) ----
    _template(
        "stair-unbox",
        "Loft stair unbox",
        "unbox",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Sits on the stairs", "Opens the box", "Holds it up", "Call to action"),
        frame=(
            "Phone-camera shot of a person sitting on an open-tread loft staircase, a shipping "
            "box on their knees, the lid half open."
        ),
        motion=(
            "Handheld, slightly shaky phone feel: the person lifts the product out of the box, "
            "turns it once in the window light and holds it toward the lens."
        ),
        script=("It came.", "{product}, straight out of the box.", "Get yours."),
        hooks=("It came.", "Finally here.", "Open it with me."),
        locations=("loft", "living-room"),
        scene="vs7",
        sort=14,
        premium=True,
    ),
    _template(
        "doorstep-delivery",
        "Doorstep delivery",
        "unbox",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=_CLIP,
        beats=("Parcel at the door", "Tape cut", "Product out", "End card"),
        frame=(
            "Top-down phone shot of a parcel on a doormat, hands with a key about to cut the tape."
        ),
        motion=(
            "Hands slit the tape, fold back the flaps and lift the product out into daylight; "
            "the phone camera follows a little late, like a real home video."
        ),
        script=("Delivery day.", "{product} made it.", "Order yours."),
        hooks=("Delivery day.", "Best parcel this week.", "Guess what came."),
        locations=("entryway", "living-room", "loft"),
        sort=15,
    ),
    _template(
        "close-detail",
        "Close to the lens",
        "hand",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Too close", "Pull back", "Texture", "End card"),
        frame=(
            "A hand holding the product very close to a phone camera at home, window light, "
            "the texture and label sharp."
        ),
        motion=(
            "The product starts almost touching the lens, pulls back and turns slowly so the "
            "light rakes across its texture; natural autofocus hunting."
        ),
        script=("Look closer.", "This is {product} up close.", "See it in person."),
        hooks=("Look closer.", "The detail on this.", "Zoom in with me."),
        locations=("living-room", "studio", "brand-colour"),
        capture="macro_tabletop",
        scene="re5-1",
        sort=26,
    ),
    _template(
        "trail-test",
        "Trail test",
        "hand",
        ("physical",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("On the trail", "Holds it up", "Puts it to work", "Call to action"),
        frame=(
            "Selfie-angle phone shot of a person on a rocky hillside trail, holding the product "
            "up beside their face, wind in their hair."
        ),
        motion=(
            "Walking selfie camera with natural bounce: the person shows the product, then "
            "swings the camera round to the view and back."
        ),
        script=("Tested it out here.", "{product} kept up all day.", "Take it further."),
        hooks=("Tested it out here.", "Real conditions only.", "No studio, just the trail."),
        locations=("trail", "beach"),
        scene="re4-2",
        sort=27,
        premium=True,
    ),
    _template(
        "street-carry",
        "Street carry",
        "hand",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Walking", "The product in view", "Stops and shows it", "Call to action"),
        frame=(
            "Phone shot walking backwards in front of a person strolling down a city street, "
            "the product carried or worn in plain view."
        ),
        motion=(
            "Backwards tracking shot as the person walks, glances at the camera, stops and lifts "
            "the product toward the lens; passers-by soft in the background."
        ),
        script=("Out with {product}.", "Out the door, every day.", "Find yours."),
        hooks=("Out with {product}.", "Errands, but make it nice.", "Spotted on the street."),
        locations=("street", "market"),
        scene="vs2",
        sort=28,
        premium=True,
    ),
    _template(
        "flat-lay",
        "Flat lay",
        "hand",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Empty surface", "Things placed", "The product last", "End card"),
        frame=(
            "Top-down shot of a wooden table with a few everyday things arranged around an "
            "empty space in the middle."
        ),
        motion=(
            "Hands place the last items one by one and set the product in the centre; the "
            "camera stays overhead and settles."
        ),
        script=("The set-up.", "{product} in the middle of it all.", "Shop the set-up."),
        hooks=("The set-up.", "Everything I carry.", "One more thing."),
        locations=("kitchen", "cafe-table", "office", "studio"),
        sort=29,
    ),
    _template(
        "seaside-wear",
        "Seaside wear",
        "tryon",
        ("beauty", "physical"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("The view", "Wearing it", "Touches it", "Call to action"),
        frame=(
            "Golden-hour phone shot of a person on a terrace above the sea, wearing the product, "
            "looking out at the view."
        ),
        motion=(
            "Slow drift around the person as they turn to the camera, touch the product and "
            "smile; warm flare from the low sun."
        ),
        script=("Golden hour.", "Wearing {product} all summer.", "Make it yours."),
        hooks=("Golden hour.", "Holiday mode.", "This light, this piece."),
        locations=("seaside", "terrace", "rooftop"),
        scene="vs3",
        sort=33,
        premium=True,
    ),
    _template(
        "beach-day",
        "Beach day",
        "tryon",
        ("beauty", "physical"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Lying on the towel", "The product", "Turns to camera", "Call to action"),
        frame=(
            "Overhead phone shot of a person lying on a beach towel, the product worn or held, "
            "blue sky and white clouds behind."
        ),
        motion=(
            "Handheld overhead camera: the person lifts the product into the sun, then turns to "
            "the lens with a laugh."
        ),
        script=("Beach essentials.", "{product} came along.", "Pack yours."),
        hooks=("Beach essentials.", "What I brought.", "Summer, sorted."),
        locations=("beach",),
        scene="re3-1",
        sort=34,
    ),
    _template(
        "floor-read",
        "Floor break",
        "use",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-5", "clip-10"),
        beats=("Lying on the rug", "Using it", "Looks up", "Tilts it to camera"),
        frame=(
            "Phone camera on the floor, low angle, a person lying on a patterned rug using the "
            "product, daylight from high windows."
        ),
        motion=(
            "Static floor-level camera: the person looks up from the product and tilts it toward "
            "the lens."
        ),
        script=("My Sunday.", "{product} and nowhere to be.", "Try it."),
        hooks=("My Sunday.", "Do not disturb.", "Me time."),
        locations=("loft", "living-room", "bedside"),
        scene="re3-3",
        sort=80,
    ),
    _template(
        "desk-break",
        "Desk break",
        "use",
        ("physical", "app"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("At the desk", "The break", "Using it", "Call to action"),
        frame=(
            "Phone propped on a desk, a person behind papers and a laptop, the product in hand."
        ),
        motion=(
            "Static desk camera: the person pushes the work aside, uses the product, and holds "
            "it up to the lens with a relieved smile."
        ),
        script=("3 p.m. slump?", "{product}, three o'clock.", "Try it today."),
        hooks=("3 p.m. slump?", "Study break.", "The afternoon reset."),
        locations=("office", "cafe-table", "kitchen"),
        scene="re1-1",
        sort=81,
    ),
    _template(
        "made-by-hand",
        "Made by hand",
        "maker",
        ("physical", "beauty"),
        "optional",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The workshop", "Hands at work", "Finishing touch", "The finished piece"),
        frame=(
            "Close shot of hands finishing the product on a workbench in a small workshop, tools "
            "and materials around, window light."
        ),
        motion=(
            "Slow handheld push-in on the hands giving the product its finishing touch, then a "
            "rack focus to the finished piece on the bench."
        ),
        script=("Made by hand.", "Every {product} is finished in our workshop.", "Own one."),
        hooks=("Made by hand.", "How it's made.", "From our workshop."),
        locations=("workshop", "studio", "shop-counter"),
        scene="vs13",
        sort=90,
        premium=True,
    ),
    _template(
        "packing-order",
        "Packing your order",
        "maker",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=_CLIP,
        beats=("Order comes in", "Wrapped", "Boxed", "Sealed"),
        frame=(
            "Overhead shot of a packing table: tissue paper, a kraft box, a handwritten note and "
            "the product."
        ),
        motion=(
            "Hands wrap the product in tissue, place it in the box with the note and close the "
            "lid; quick, satisfying, slightly sped up."
        ),
        script=("Packing your order.", "Every {product} is wrapped by hand.", "Order yours."),
        hooks=("Packing your order.", "Small business, big care.", "Pack an order with me."),
        locations=("workshop", "shop-counter", "kitchen"),
        sort=91,
    ),
    _template(
        "room-reveal",
        "Room reveal",
        "tour",
        ("physical", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The room", "Slow pan", "The product in place", "End card"),
        frame=(
            "Wide phone shot of a styled room in soft daylight, the product in place as part of "
            "the room."
        ),
        motion=(
            "Slow, smooth pan across the room that ends on the product in place; nothing in the "
            "room moves, no people appear."
        ),
        script=("The finishing touch.", "{product}, at home.", "Bring it home."),
        hooks=("The finishing touch.", "Room glow-up.", "What changed in here?"),
        locations=("living-room", "loft"),
        scene="vs9",
        sort=46,
        premium=True,
    ),
    _template(
        "gate-app",
        "On the go app",
        "app",
        ("app",),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("On the move", "Phone out", "The screen", "Download"),
        frame=(
            "A hand holding a phone up while on the move, the street soft behind, the app's "
            "screen clearly visible."
        ),
        motion=(
            "Walking handheld feel, the phone held steady while the street drifts past behind "
            "it; one thumb tap."
        ),
        script=("Sorted before boarding.", "{product} does it in a tap.", "Download it free."),
        hooks=("Sorted before boarding.", "Travel hack.", "Done before the gate."),
        locations=("street", "cafe-table"),
        needs="app_screenshot",
        scene="re4-4",
        sort=43,
    ),
    _template(
        "gym-tablet",
        "Gym tablet",
        "app",
        ("app",),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("At the gym", "Tablet up", "The screen", "Download"),
        frame=(
            "A tablet held up in a bright gym, machines behind, the app's screen clearly visible."
        ),
        motion="The tablet tilts toward the camera and a finger scrolls once; slow push-in.",
        script=("Today's plan.", "{product} has it ready.", "Start free."),
        hooks=("Today's plan.", "No more guessing.", "Train smarter."),
        locations=("gym",),
        needs="app_screenshot",
        scene="vs8",
        sort=44,
    ),
    # --- added 2026-10-27, second pass: formats the ad references rank high ---
    # (coreyhaines31/marketingskills `ad-creative`, gooseworks-ai/goose-skills
    # `skills/video/render-*`), kept to what renders without speech today.
    _template(
        "reaction-reveal",
        "Reaction + reveal",
        "hand",
        ("physical", "beauty"),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("The reaction", "Hard cut: the product", "Up close", "Call to action"),
        frame=(
            "Phone-camera close-up of a person glancing up from what they were doing, eyebrows "
            "raised, eyes on something just out of frame."
        ),
        motion=(
            "Subtle handheld drift and a slow push-in; small natural movements only, nothing "
            "changes shape."
        ),
        script=("Wait, what?", "{product}. That's what.", "See it for yourself."),
        hooks=("Wait, what?", "Look closer.", "Okay, look at this."),
        locations=("living-room", "kitchen", "street"),
        sort=29,
        premium=True,
        shots=(("frame", "{hook}"), ("photo", "{product}"), ("photo", "{cta}")),
    ),
    _template(
        "reaction-demo",
        "Reaction + demo",
        "app",
        ("app",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("The reaction", "Hard cut: the app", "The result", "Download"),
        frame=(
            "Phone-camera close-up of a person looking at their phone, eyebrows up, a half "
            "laugh of disbelief."
        ),
        motion=(
            "Subtle handheld drift and a slow push-in; small natural movements only, nothing "
            "changes shape."
        ),
        script=("Done in two taps.", "{product} does it for you.", "Download it free."),
        hooks=("Done in two taps.", "Watch this.", "The fast way."),
        locations=("living-room", "office", "street"),
        needs="app_screenshot",
        sort=44,
        shots=(("frame", "{hook}"), ("screen", "{body}"), ("screen", "{cta}")),
    ),
    _template(
        "value-stack",
        "Three reasons, no talking",
        "promo",
        ("physical", "beauty", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The hook", "Reason", "Reason", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion=(
            "A slow, steady push-in with a slight parallax; the product stays exactly as it is."
        ),
        script=("Why people switch.", "Here's what {product} does.", "Try it."),
        hooks=("Why people switch.", "Three reasons.", "Sound off? Read this."),
        locations=("studio", "brand-colour"),
        sort=100,
        shots=(("photo", "{hook}"), ("photo", "{body}"), ("photo", "{cta}")),
    ),
    _template(
        "offer-drop",
        "Offer drop",
        "promo",
        ("physical", "beauty", "app", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Headline", "Product drop", "The offer", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion=(
            "A quick push-in that settles, as if the product just dropped into place; the "
            "product stays exactly as it is."
        ),
        script=("Now on offer.", "{product}, on offer.", "Shop now."),
        hooks=("Now on offer.", "{product}, now on offer.", "Worth a look."),
        locations=("studio", "brand-colour"),
        sort=101,
        premium=True,
        shots=(("photo", "{hook}"), ("photo", "{product}"), ("photo", "{cta}")),
    ),
    _template(
        "lineup",
        "The lineup",
        "promo",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The hook", "One", "Another", "Pick yours"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow push-in, then a gentle drift; the product stays exactly as it is.",
        script=("Which one is you?", "Every {product}, side by side.", "Pick yours."),
        hooks=("Which one is you?", "The full range.", "Choose your favourite."),
        locations=("studio", "brand-colour"),
        sort=102,
        shots=(("photo", "{hook}"), ("photo", ""), ("photo", ""), ("photo", "{cta}")),
    ),
    _template(
        "hero-orbit",
        "Hero orbit",
        "fx",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Hero", "Orbit", "Macro", "End card"),
        frame=(
            "The product alone on a round plinth against a seamless colour backdrop, soft rim "
            "light, crisp reflections."
        ),
        motion=(
            "The camera orbits slowly halfway round the product and settles on it exactly as in "
            "this frame; perfectly smooth, the product never moves."
        ),
        land=True,
        script=("Look again.", "{product}, every angle.", "Discover it."),
        hooks=("Look again.", "Built to be seen.", "The detail."),
        locations=("studio", "brand-colour"),
        sort=74,
        premium=True,
    ),
    _template(
        "macro-sizzle",
        "Macro sizzle",
        "use",
        ("physical",),
        "optional",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Flat lay", "Hands break it", "The bite", "Hero"),
        frame=(
            "Macro tabletop shot of the product on a rustic board, crumbs and texture sharp, "
            "warm side light."
        ),
        motion=(
            "Hands break or open the product in slow macro detail, crumbs and texture catch the "
            "light; the camera creeps in."
        ),
        script=("Hear that?", "{product}, fresh.", "Taste it."),
        hooks=("Hear that?", "Crunch.", "Fresh out."),
        locations=("kitchen", "cafe-table", "picnic", "studio"),
        capture="macro_tabletop",
        sort=82,
    ),
    _template(
        "swatch-test",
        "Swatch test",
        "tryon",
        ("beauty",),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Clean arm", "Swatch", "Blend", "Hero"),
        frame="Close shot of a forearm on a white surface, the product held just above it.",
        motion=(
            "Stop-motion feel: the hand swipes the product across the skin, a swatch appears and "
            "is blended with a fingertip; the frame stays locked."
        ),
        script=("Swatch check.", "{product}, on real skin.", "Find your shade."),
        hooks=("Swatch check.", "Up close.", "On real skin."),
        locations=("studio", "bathroom-shelf"),
        capture="macro_tabletop",
        sort=35,
    ),
    # Talking formats the references rank highest (S/A tier); shown as coming
    # until speech ships (S17-12).
    _template(
        "expert-take",
        "Expert take",
        "talk",
        ("physical", "beauty", "app", "service"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("Who I am", "The problem", "What works", "Call to action"),
        frame=(
            "An expert in work clothes speaking to the camera in their workplace, the product "
            "in hand."
        ),
        motion="Static front camera, natural gestures, a punch-in for the last line.",
        script=("Here's how it's done.", "Why {product} works.", "Learn more."),
        hooks=("Here's how it's done.", "The right way to use it.", "Quick tip."),
        locations=("workshop", "office", "studio"),
        available=False,
        description=_TALK,
        scene="re3-5",
        sort=55,
    ),
    _template(
        "investigation",
        "I looked into it",
        "talk",
        ("physical", "beauty", "app", "service", "space"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The question", "Checking it out", "The finding", "Call to action"),
        frame="A person on the move holding the phone selfie-style, curious, the product in hand.",
        motion="Walking selfie camera, quick cuts of looking around, a punch-in on the product.",
        script=("Is it actually worth it?", "What's really in {product}.", "See for yourself."),
        hooks=("Is it actually worth it?", "Let's look closer.", "Let's investigate."),
        locations=("street", "market", "living-room"),
        available=False,
        description=_TALK,
        sort=56,
    ),
    _template(
        "underdog",
        "Underdog story",
        "talk",
        ("physical", "beauty", "app", "service", "space"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The big problem", "Why we started", "What we do differently", "Join us"),
        frame="A founder in their small workspace speaking to the camera, the product beside them.",
        motion="Static front camera, natural gestures, a punch-in for the last line.",
        script=("The big brands do it one way.", "We made {product} another way.", "Join us."),
        hooks=("The big brands do it one way.", "We're the small team.", "Here's what changed."),
        locations=("workshop", "office", "living-room"),
        available=False,
        description=_TALK,
        sort=57,
    ),
    _template(
        "green-screen",
        "Green-screen take",
        "talk",
        ("physical", "beauty", "app", "service", "space"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The point", "Shown behind", "The proof", "Call to action"),
        frame=(
            "A person cut out in the lower half of the frame, the product's photo filling the "
            "background."
        ),
        motion=(
            "The person talks and points up at the picture behind them; the picture stays still."
        ),
        script=("Look at this.", "Here's what {product} does.", "Try it."),
        hooks=("Look at this.", "Let me explain.", "Nobody talks about this."),
        locations=("studio",),
        available=False,
        description=_TALK,
        sort=58,
    ),
    _template(
        "comment-reply",
        "Reply to a comment",
        "talk",
        ("physical", "beauty", "app", "service", "space"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The question", "The answer", "Show it", "Call to action"),
        frame="A person speaking to the phone camera at home, the product in hand.",
        motion="Static front camera, natural gestures, the product lifted to the lens.",
        script=("You asked, so here it is.", "The honest answer about {product}.", "Ask me more."),
        hooks=("You asked, so here it is.", "Replying to your question.", "Let's settle this."),
        locations=("living-room", "kitchen"),
        available=False,
        description=_TALK,
        sort=59,
    ),
    _template(
        "car-chat",
        "Car conversation",
        "interview",
        ("physical", "beauty", "app", "service"),
        "required",
        "talking",
        lengths=("clip-15",),
        beats=("The question", "The answer", "The product", "Close"),
        frame=(
            "Two friends in a parked car's front seats, wide dashboard camera, the product "
            "between them."
        ),
        motion="Static dashboard camera, natural back-and-forth, a glance at the product.",
        script=("Okay, be honest.", "What do you think of {product}?", "Try it."),
        hooks=("Okay, be honest.", "Car talk.", "Settle this for us."),
        locations=("car-interior", "street"),
        available=False,
        description=_TALK,
        scene="re5-5",
        sort=62,
    ),
    # --- graphic formats (S17-33): drawn in code, around the real product ------
    _template(
        "offer-ad",
        "Offer ad",
        "promo",
        ("physical", "beauty", "service"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The offer", "The product", "Why now", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion=(
            "A quick push-in that settles, as if the product just landed; it stays exactly "
            "as it is."
        ),
        script=("This one is on offer.", "{product}, at its offer price.", "Shop the offer."),
        hooks=("This one is on offer.", "{product}, on offer.", "Worth a look."),
        locations=("studio", "brand-colour"),
        needs="offer",
        shots=(
            ("card", "{offer}", {"kicker": "{ends}"}),
            ("photo", "{product}"),
            ("card", "{body}"),
            ("card", "{cta}"),
        ),
        sort=90,
    ),
    _template(
        "value-prop",
        "Value prop",
        "promo",
        ("physical", "beauty", "app", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The promise", "Benefit 1", "Benefit 2", "Benefit 3"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow, steady push-in; the product stays exactly as it is.",
        script=(
            "Three reasons to pick it up.",
            "Ready when you are · Made to be used · Easy to keep",
            "See it for yourself.",
        ),
        hooks=("Three reasons to pick it up.", "What it does, in three lines.", "Worth a look."),
        locations=("studio", "brand-colour"),
        shots=(
            ("card", "{hook}"),
            ("cutout", "{claim1}"),
            ("cutout", "{claim2}"),
            ("cutout", "{claim3}"),
        ),
        script_hint=(
            "BODY is three benefits of at most four words each, separated by ' · ', each one "
            "true of the product from the brief."
        ),
        sort=91,
    ),
    _template(
        "variant-swipe",
        "Variant swipe",
        "promo",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Variant 1", "Variant 2", "Variant 3", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow, steady push-in; the product stays exactly as it is.",
        script=("Pick your favourite.", "Every {product}, one swipe each.", "Find yours."),
        hooks=("Pick your favourite.", "Which one is you?", "Swipe through."),
        locations=("studio", "brand-colour"),
        needs="variants",
        shots=(("cutout", "{product}"), ("cutout", ""), ("cutout", ""), ("card", "{cta}")),
        transition="slide",
        sort=92,
    ),
    _template(
        "mosaic-reveal",
        "Mosaic reveal",
        "promo",
        ("physical", "beauty", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("All at once", "Up close", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow push-in on the product; it stays exactly as it is.",
        script=("All of it, in one look.", "{product}, every angle.", "Shop the range."),
        hooks=("All of it, in one look.", "The whole range.", "Every angle."),
        locations=("studio", "brand-colour"),
        needs="photos",
        shots=(("grid", "{hook}"), ("photo", "{body}"), ("card", "{cta}")),
        sort=93,
    ),
    _template(
        "myth-fact",
        "Myth vs fact",
        "text",
        ("physical", "beauty", "app", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The myth", "The fact", "The product", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow push-in on the product; it stays exactly as it is.",
        script=("It has to be complicated.", "With {product}, it isn't.", "See how it works."),
        hooks=("It has to be complicated.", "Everyone says it's hard.", "You've heard this one."),
        locations=("studio", "brand-colour"),
        shots=(
            ("card", "{hook}", {"kicker": "Myth", "strike": True}),
            ("card", "{body}", {"kicker": "Fact"}),
            ("photo", "{product}"),
            ("card", "{cta}"),
        ),
        script_hint=(
            "HOOK is a belief people hold about this kind of product, never about a named rival; "
            "BODY answers it with one fact from the brief."
        ),
        sort=94,
    ),
    _template(
        "chat-reveal",
        "Chat reveal",
        "text",
        ("physical", "beauty", "app", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("The question", "The answer", "The product", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow push-in on the product; it stays exactly as it is.",
        script=("Does it actually work?", "Here's {product} doing it.", "Try it."),
        hooks=("Does it actually work?", "Okay, which one?", "Need a hand with this."),
        locations=("studio", "brand-colour"),
        shots=(("chat", ""), ("photo", "{product}"), ("card", "{cta}")),
        script_hint=(
            "HOOK is a friend's short question in a chat; BODY is the reply, which names the "
            "product. Both read like a real message, never like an ad."
        ),
        sort=95,
    ),
    _template(
        "shop-tour",
        "Your shop tour",
        "tour",
        ("physical", "beauty", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Come in", "Look around", "The product", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A gentle walk-through push-in, steady and slow; nothing in the place changes.",
        script=("Come and see us.", "This is where {product} lives.", "Visit us."),
        hooks=("Come and see us.", "Step inside.", "Our place."),
        locations=("studio",),
        needs="places",
        shots=(("place", "{hook}"), ("place", ""), ("photo", "{body}"), ("card", "{cta}")),
        sort=96,
    ),
    _template(
        "proof-card",
        "Customer quote",
        "promo",
        ("physical", "beauty", "app", "service", "space"),
        "none",
        "product",
        lengths=("clip-10", "clip-15"),
        beats=("Their words", "The product", "Call to action"),
        frame="The product on a plain surface, soft daylight.",
        motion="A slow push-in on the product; it stays exactly as it is.",
        script=("In our customers' words.", "{product}, as they tell it.", "See for yourself."),
        hooks=("In our customers' words.", "What people say.", "Straight from a customer."),
        locations=("studio", "brand-colour"),
        needs="reviews",
        shots=(
            ("card", '"{review}"', {"kicker": "{review_author}"}),
            ("photo", "{product}"),
            ("card", "{cta}"),
        ),
        sort=102,
    ),
    *[
        _template(
            key,
            label,
            "promo",
            ("physical", "beauty", "service"),
            "none",
            "product",
            lengths=("clip-10", "clip-15"),
            beats=("The offer", "The product", "Why now", "Call to action"),
            frame="The product on a plain surface, soft daylight.",
            motion=(
                "A quick push-in that settles, as if the product just landed; it stays exactly "
                "as it is."
            ),
            script=script,
            hooks=(script[0],),
            locations=("studio", "brand-colour"),
            needs="offer",
            shots=(
                ("card", "{offer}", {"kicker": "{ends}"}),
                ("photo", "{product}"),
                ("card", "{body}"),
                ("card", "{cta}"),
            ),
            sort=sort,
        )
        for key, label, script, sort in (
            (
                "ramadan-offer",
                "Ramadan offer",
                (
                    "Ramadan nights, a little sweeter.",
                    "{product}, on offer for Ramadan.",
                    "Shop the Ramadan offer.",
                ),
                97,
            ),
            (
                "eid-offer",
                "Eid offer",
                ("Ready for Eid.", "{product}, on offer for Eid.", "Shop the Eid offer."),
                98,
            ),
            (
                "back-to-school",
                "Back to school",
                (
                    "Back to school, sorted.",
                    "{product}, on offer for the new term.",
                    "Shop the offer.",
                ),
                99,
            ),
            (
                "summer-sale",
                "Summer offer",
                ("Summer, on offer.", "{product}, at its summer price.", "Shop the summer offer."),
                100,
            ),
            (
                "black-friday",
                "Black Friday",
                ("Black Friday.", "{product}, at its Black Friday price.", "Shop the offer."),
                101,
            ),
        )
    ],
]

#: The rail, in order: key, label.
CATEGORIES: list[tuple[str, str]] = [
    ("unbox", "Unboxing"),
    ("hand", "In hand"),
    ("tryon", "Try-on"),
    ("use", "In use"),
    ("app", "App demo"),
    ("tour", "Space tour"),
    ("maker", "Behind the scenes"),
    ("talk", "Talking"),
    ("interview", "Interview"),
    ("promo", "Offers & claims"),
    ("text", "Text & chat"),
    ("fx", "Effects"),
]


def _trait(
    group: str, key: str, label: str, fragment: str, sort: int, **extra: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "description": "",
        "icon_paths": [],
        "colors": [],
        "prompt_fragment": fragment,
        "sort_order": sort,
        "metadata": {"group": group, **extra, "seed": SEED_VERSION},
    }


_GROUPS: dict[str, list[tuple[str, str, str]]] = {
    # S17-30: who the person is. Never set by an archetype, never defaulted —
    # an unspecified face comes out as a composite that drifts between shots.
    "gender": [
        ("gender-woman", "woman", "a woman"),
        ("gender-man", "man", "a man"),
    ],
    "heritage": [
        ("heritage-north-african", "North African", "of North African heritage"),
        ("heritage-middle-eastern", "Middle Eastern", "of Middle Eastern heritage"),
        ("heritage-southern-european", "Southern European", "of Southern European heritage"),
        ("heritage-northern-european", "Northern European", "of Northern European heritage"),
        ("heritage-west-african", "West African", "of West African heritage"),
        ("heritage-east-african", "East African", "of East African heritage"),
        ("heritage-south-asian", "South Asian", "of South Asian heritage"),
        ("heritage-east-asian", "East Asian", "of East Asian heritage"),
        ("heritage-southeast-asian", "Southeast Asian", "of Southeast Asian heritage"),
        ("heritage-latin-american", "Latin American", "of Latin American heritage"),
        ("heritage-mixed", "mixed", "of mixed heritage"),
    ],
    "hair": [
        ("hair-short-dark", "short dark", "short dark hair, neatly cut"),
        ("hair-long-dark-wavy", "long dark wavy", "long dark wavy hair"),
        ("hair-bun", "dark bun", "dark hair in a low bun"),
        ("hair-curly", "curly", "shoulder-length curly hair"),
        ("hair-braids", "braids", "long braids tied back"),
        ("hair-blonde", "long blonde", "long straight blonde hair"),
        ("hair-grey", "short grey", "short grey hair"),
        ("hair-shaved", "shaved", "a closely shaved head"),
        ("hair-headscarf", "headscarf", "hair covered by a plain headscarf"),
    ],
    "age": [
        ("age-20s", "20s", "in their twenties"),
        ("age-30s", "30s", "in their thirties"),
        ("age-40s", "40s", "in their forties"),
        ("age-50s", "50s+", "in their fifties or older"),
    ],
    "look": [
        ("look-fresh", "fresh", "fresh-faced, natural skin"),
        ("look-polished", "polished", "polished, well-groomed"),
        ("look-athletic", "athletic", "athletic build"),
        ("look-rugged", "rugged", "rugged, outdoorsy"),
    ],
    "energy": [
        ("energy-calm", "calm", "a calm, warm expression"),
        ("energy-friendly", "friendly", "a friendly, open smile"),
        ("energy-upbeat", "upbeat", "a bright, lively expression, the face still relaxed"),
    ],
}

_ARCHETYPES: list[tuple[str, str, str, dict[str, str]]] = [
    (
        "arch-coach",
        "Gym coach, 30s",
        "a fitness coach",
        {
            "age": "age-30s",
            "look": "look-athletic",
            "style": "style-sporty",
            "energy": "energy-upbeat",
        },
    ),
    (
        "arch-host",
        "Studio host",
        "a studio presenter",
        {
            "age": "age-30s",
            "look": "look-polished",
            "style": "style-smart",
            "energy": "energy-friendly",
        },
    ),
    (
        "arch-chef",
        "Home chef, 40s",
        "a home cook",
        {
            "age": "age-40s",
            "look": "look-fresh",
            "style": "style-casual",
            "energy": "energy-friendly",
        },
    ),
    (
        "arch-student",
        "Student, 20s",
        "a university student",
        {
            "age": "age-20s",
            "look": "look-fresh",
            "style": "style-casual",
            "energy": "energy-upbeat",
        },
    ),
    (
        "arch-artisan",
        "Artisan, 50s",
        "a craftsperson",
        {
            "age": "age-50s",
            "look": "look-rugged",
            "style": "style-traditional",
            "energy": "energy-calm",
        },
    ),
    (
        "arch-pharmacist",
        "Pharmacist, 30s",
        "a pharmacist",
        {
            "age": "age-30s",
            "look": "look-polished",
            "style": "style-smart",
            "energy": "energy-calm",
        },
    ),
]

#: Whole outfits (S17-30): a top, a bottom and what goes with them. Naming only
#: the top lets the model invent the rest, differently in every shot.
_STYLES: list[tuple[str, str, str, str, str]] = [
    (
        "style-casual",
        "casual",
        "a plain white crew-neck t-shirt",
        "light blue straight jeans",
        "no jewellery",
    ),
    (
        "style-smart",
        "smart",
        "a navy blazer over a white shirt",
        "dark tailored trousers",
        "a simple steel watch",
    ),
    (
        "style-sporty",
        "sporty",
        "a grey zip hoodie",
        "black joggers and white trainers",
        "nothing else",
    ),
    (
        "style-traditional",
        "traditional",
        "a linen tunic in a warm neutral colour",
        "wide matching trousers",
        "a thin leather bracelet",
    ),
]

AVATAR_TRAITS: list[dict[str, Any]] = [
    *[
        _trait("archetype", key, label, fragment, index, values=values)
        for index, (key, label, fragment, values) in enumerate(_ARCHETYPES)
    ],
    *[
        _trait(group, key, label, fragment, index)
        for group, rows in _GROUPS.items()
        for index, (key, label, fragment) in enumerate(rows)
    ],
    *[
        _trait(
            "style",
            key,
            label,
            f"wearing {top}, {bottom}; {accessories}",
            index,
            top=top,
            bottom=bottom,
            accessories=accessories,
        )
        for index, (key, label, top, bottom, accessories) in enumerate(_STYLES)
    ],
]

LENGTH_UNITS = {"clip-5": 1, "clip-10": 1, "clip-15": 2, "reel-15": 2, "reel-30": 3}

#: Words a second a script is read at, per content language (goose-skills'
#: measured reference rates, `avatar_upgrade/analysis.md` §7).
LANGUAGE_WPS = {"en": 2.6, "fr": 2.4, "it": 2.5}


# -----------------------------------------------------------------------------
# Angles and hook styles (S17-34, `studio_content_audit.md` §4.8)
# -----------------------------------------------------------------------------
def _brief_row(
    key: str, label: str, description: str, guidance: str, **meta: Any
) -> dict[str, Any]:
    """A row the script writer reads: `prompt_fragment` is its instruction."""
    return {
        "key": key,
        "label": label,
        "description": description,
        "icon_paths": [],
        "colors": [],
        "prompt_fragment": guidance,
        "metadata": {**meta, "seed": SEED_VERSION},
    }


#: How an ad's first second works (goose-skills `hook-families.md`). Styles
#: that need an invented history or a stranger's reaction are left out.
HOOK_FAMILIES: list[dict[str, Any]] = [
    _brief_row(
        "relatable-moment",
        "Relatable moment",
        "The viewer's own habit, mid-action",
        "Open on the viewer's own habit, mid-action, named in the buyer's own words.",
    ),
    _brief_row(
        "call-out",
        "Call-out",
        "Who it is for, by situation",
        "Name exactly who it is for by a concrete situation, never by a trait the viewer has.",
    ),
    _brief_row(
        "number",
        "Number or stake",
        "One number the brief gives",
        "Lead with one number or stake the brief gives; never invent one.",
    ),
    _brief_row(
        "cold-open",
        "Cold open",
        "Mid-action, no setup",
        "Start mid-action with no setup; say only where or when.",
    ),
    _brief_row(
        "result-first",
        "Result first",
        "The after, then how",
        "Show the after state first, then how it got there; only a result the brief supports.",
    ),
    _brief_row(
        "demo-first",
        "Demo first",
        "The product doing it at once",
        "The product doing the thing in the first second; the words point at it.",
    ),
    _brief_row(
        "visual-only",
        "Visual only",
        "A picture that needs no words",
        "A pattern break that needs no words; at most four words on screen.",
    ),
    _brief_row(
        "category-complaint",
        "The usual complaint",
        "The category's shared complaint",
        "Name the one complaint everyone has about this kind of product; never a rival brand.",
    ),
    _brief_row(
        "contrarian",
        "Contrarian",
        "The opposite of the usual advice",
        "Say the opposite of the usual advice, with the real reason behind it.",
    ),
    _brief_row(
        "mistake",
        "The common mistake",
        "A mistake, said kindly",
        "Show the common mistake in a protective tone; no fear, no shame.",
    ),
    _brief_row(
        "old-vs-new",
        "Old way, new way",
        "The usual way beside this one",
        "Set the usual way beside this one, fairly; never name a rival.",
    ),
    _brief_row(
        "question",
        "Specific question",
        "A question only this viewer asks",
        "Ask a question only this viewer asks and answer it within five seconds; never "
        "'Have you ever'.",
    ),
    _brief_row(
        "reveal",
        "Reveal",
        "Promise the end, then show it",
        "Promise what the end will show, and show it.",
    ),
    _brief_row(
        "list",
        "Short list",
        "A count with item one in view",
        "A count or a time-box, with the first item already in view.",
    ),
]

#: What the ad argues, by what the viewer already knows (goose-skills
#: `hook-families.md`, sales `storytelling`); each with the styles that suit it.
ANGLES: list[dict[str, Any]] = [
    _brief_row(
        "story",
        "Story",
        "A moment, a snag, a way through",
        "Tell one short story: the situation, what got in the way, how the product resolved it; "
        "the buyer is the hero.",
        stage="unaware",
        families=["relatable-moment", "cold-open", "reveal"],
    ),
    _brief_row(
        "desire",
        "Desire",
        "The pleasure of using it",
        "Sell the sensory moment of using it; no problem is needed.",
        stage="unaware",
        families=["visual-only", "demo-first", "relatable-moment"],
    ),
    _brief_row(
        "problem-fix",
        "Problem, then fix",
        "The problem in the buyer's words",
        "Name the problem as the buyer says it, then show the product fixing it.",
        stage="problem",
        families=["relatable-moment", "call-out", "mistake"],
    ),
    _brief_row(
        "demo",
        "Demo",
        "Watch it do the job",
        "Show the product doing its job; the words only point at what to watch.",
        stage="solution",
        families=["demo-first", "result-first", "list"],
    ),
    _brief_row(
        "how-it-works",
        "How it works",
        "One step at a time",
        "Explain how it works, one step at a time, each step visible.",
        stage="solution",
        families=["list", "question", "demo-first"],
    ),
    _brief_row(
        "old-vs-new",
        "Old way, new way",
        "The usual way beside this one",
        "Compare the usual way of doing it with this product's way, fairly; never a rival brand.",
        stage="solution",
        families=["old-vs-new", "category-complaint", "contrarian"],
    ),
    _brief_row(
        "proof",
        "Proof",
        "One true proof",
        "Make one claim and prove it with a fact or a proven claim from the brief.",
        stage="product",
        families=["number", "result-first", "demo-first"],
    ),
    _brief_row(
        "objection",
        "Answer an objection",
        "What makes them hesitate, answered",
        "Name the top objection the brief gives, then answer it with one fact.",
        stage="product",
        families=["question", "contrarian", "call-out"],
    ),
    _brief_row(
        "offer",
        "Offer",
        "The real offer and when it ends",
        "Lead with the real offer and its end date, only as the brief gives them.",
        stage="product",
        families=["number", "cold-open", "call-out"],
    ),
    _brief_row(
        "news",
        "News",
        "What is new",
        "Say what is new about the product, as the brief gives it.",
        stage="any",
        families=["cold-open", "reveal", "number"],
    ),
]


AD_TEMPLATE_CATALOG: dict[str, list[dict[str, Any]]] = {
    "ad_template": TEMPLATES,
    "avatar_trait": AVATAR_TRAITS,
    "place": PLACES,
    "hook_family": HOOK_FAMILIES,
    "angle": ANGLES,
}
