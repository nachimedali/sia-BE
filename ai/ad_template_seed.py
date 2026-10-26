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
* `needs` — `app_screenshot` when the frame needs one from the product
* `available` — false while the format needs speech (T4): shown, never rendered

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

#: Where a template can be set. Fragments say what the camera sees, never what
#: the product looks like.
LOCATIONS: dict[str, str] = {
    "studio": "a clean studio set with a soft seamless backdrop",
    "kitchen": "a bright home kitchen counter in morning light",
    "rooftop": "a city rooftop terrace at golden hour",
    "terrace": "a sunlit café terrace with whitewashed walls",
    "market": "a lively covered market aisle with warm lamps",
    "beach": "a quiet beach with soft afternoon light",
    "dunes": "rolling sand dunes under a clear sky",
}

_NEGATIVE = (
    "no resemblance to any real or famous person, no brand logos except the product's own, "
    "no readable text except the product's own, no extra fingers, no warped hands, no product "
    "redesign, no watermark"
)

_CLIP = ("clip-5", "clip-10", "clip-15")
_UNITS = {"clip-5": 1, "clip-10": 1, "clip-15": 2}


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
) -> dict[str, Any]:
    person_line = {
        "required": " {person} is in the frame, natural and unposed.",
        "optional": "{person_optional}",
        "none": " No person in frame; hands only where the action needs them.",
    }[person]
    keyframe = (
        f"Vertical 9:16 photo for a short social ad. {frame} Setting: {{location}}."
        f"{person_line} The {{product}} is clearly visible and exactly as in the reference "
        "photos: same shape, label and colours. Authentic, unpolished social-video realism."
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
            "units": {length: _UNITS[length] for length in lengths},
            "beats": list(beats),
            "keyframe_prompt": keyframe,
            "motion_prompt": f"{motion} The product never changes shape, label or colour.",
            "negative": _NEGATIVE,
            "script": {"hook": script[0], "body": script[1], "cta": script[2]},
            "hooks": list(hooks),
            "locations": list(locations),
            "needs": needs,
            "available": available,
            "scene": scene,
            "seed": SEED_VERSION,
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
        hooks=("Wait for it.", "The slowest unboxing you'll watch today.", "Guess what's inside."),
        locations=("studio", "kitchen", "rooftop"),
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
        locations=("studio", "rooftop", "terrace"),
        sort=11,
    ),
    _template(
        "lid-asmr",
        "Lid lift ASMR",
        "unbox",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=_CLIP,
        beats=("Closed lid", "Slow lift", "Detail", "End card"),
        frame="Macro shot of the product's closed lid or packaging, shallow depth of field.",
        motion="Very slow lid lift, macro detail, a soft rack focus onto the product.",
        script=("Turn the sound on.", "Every detail of {product}.", "Discover it now."),
        hooks=("Turn the sound on.", "Oddly satisfying.", "Just watch the lid."),
        locations=("studio",),
        sort=12,
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
            "Opening it for the first time, and it's even better in hand.",
            "Link to shop.",
        ),
        hooks=("First look at {product}.", "I wasn't expecting this.", "Unboxing it live."),
        locations=("terrace", "kitchen", "studio"),
        scene="vs7",
        sort=13,
    ),
    # --- in hand --------------------------------------------------------------
    _template(
        "pour-shot",
        "Pour shot",
        "hand",
        ("physical",),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("Hand enters", "The pour", "Close-up", "End card"),
        frame="A hand holding the product above a glass or plate, ready to pour.",
        motion="Slow-motion pour, then a slow push-in on the product label.",
        script=("This is the pour.", "{product}, poured fresh.", "Taste it yourself."),
        hooks=("This is the pour.", "Watch the colour.", "Slow it down."),
        locations=("kitchen", "terrace", "beach"),
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
        hooks=("Watch this.", "My market find of the week.", "This is what I came for."),
        locations=("market", "terrace", "rooftop"),
        scene="vs5",
        sort=21,
    ),
    _template(
        "kitchen-counter",
        "Kitchen counter",
        "hand",
        ("physical", "beauty"),
        "optional",
        "product",
        lengths=_CLIP,
        beats=("Counter wide", "Hand reaches", "In use", "End card"),
        frame="Wide shot of a tidy counter with the product among everyday things.",
        motion="Slow push-in as a hand reaches in and picks up the product.",
        script=(
            "Every morning, same spot.",
            "{product} has a place on my counter.",
            "Make it yours.",
        ),
        hooks=("Every morning, same spot.", "The one thing on my counter.", "Kitchen staple."),
        locations=("kitchen",),
        sort=22,
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
        locations=("studio", "kitchen"),
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
        locations=("kitchen", "terrace"),
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
        hooks=("What's in my bag?", "Never leaving home without it.", "Bag essentials."),
        locations=("rooftop", "terrace", "market"),
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
        locations=("studio",),
        scene="vs3",
        sort=30,
    ),
    _template(
        "before-after",
        "Before / after",
        "tryon",
        ("beauty",),
        "required",
        "people",
        lengths=("clip-10", "clip-15"),
        beats=("Before", "Apply", "After", "End card"),
        frame="Close-up of a person's face or hands, the product held beside them.",
        motion="A gentle wipe transition from before to after, then a hold on the result.",
        script=("Before.", "After {product}.", "See the difference."),
        hooks=("Before.", "Watch the change.", "Same light, no filter."),
        locations=("studio",),
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
        locations=("studio", "terrace"),
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
        locations=("studio", "terrace"),
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
        locations=("studio", "rooftop"),
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
        script=("Tired of this?", "{product} fixes it in a minute.", "Download it free."),
        hooks=("Tired of this?", "I used to hate this.", "Stop doing it by hand."),
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
                ("A customer wrote to us.", "What they said about {product}.", "Join them."),
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
        motion="A splash of liquid rises around the product in slow motion, then settles.",
        script=("Fresh.", "{product}, at its best.", "Taste it now."),
        hooks=("Fresh.", "Splash.", "Pure."),
        locations=("studio", "beach"),
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
        motion="The product lifts off the plinth and floats, turning slowly in the air.",
        script=("Light.", "{product}, weightless.", "Discover it."),
        hooks=("Light.", "Gravity optional.", "Float."),
        locations=("studio", "dunes"),
        sort=71,
    ),
    _template(
        "colour-burst",
        "Colour burst",
        "fx",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Still", "Burst", "Hero", "End card"),
        frame="The product centred on a plain backdrop in the brand's colours.",
        motion="A burst of coloured powder blooms behind the product, then a slow push-in.",
        script=("Colour.", "{product}, bold.", "Shop now."),
        hooks=("Colour.", "Bold move.", "Burst."),
        locations=("studio", "rooftop"),
        sort=72,
    ),
    _template(
        "freeze-frame",
        "Freeze frame",
        "fx",
        ("physical", "beauty"),
        "none",
        "product",
        lengths=("clip-5", "clip-10"),
        beats=("Motion", "Freeze", "Callouts", "End card"),
        frame="The product mid-motion, caught at its best angle.",
        motion="Fast motion that snaps to a freeze, a slight zoom on the product.",
        script=("Stop.", "Look at {product}.", "Get yours."),
        hooks=("Stop.", "Freeze.", "Wait, look."),
        locations=("studio",),
        sort=73,
    ),
]

#: The rail, in order: key, label.
CATEGORIES: list[tuple[str, str]] = [
    ("unbox", "Unboxing"),
    ("hand", "In hand"),
    ("tryon", "Try-on"),
    ("app", "App demo"),
    ("talk", "Talking"),
    ("interview", "Interview"),
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
    "style": [
        ("style-casual", "casual", "casual everyday clothes"),
        ("style-smart", "smart", "smart, tailored clothes"),
        ("style-sporty", "sporty", "sportswear"),
        ("style-traditional", "traditional", "classic, traditional clothes"),
    ],
    "energy": [
        ("energy-calm", "calm", "a calm, warm expression"),
        ("energy-friendly", "friendly", "a friendly, open smile"),
        ("energy-upbeat", "upbeat", "an upbeat, energetic expression"),
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
]

LENGTH_UNITS = {"clip-5": 1, "clip-10": 1, "clip-15": 2, "reel-15": 2, "reel-30": 3}

#: Words a second a script is read at, per content language (goose-skills'
#: measured reference rates, `avatar_upgrade/analysis.md` §7).
LANGUAGE_WPS = {"en": 2.6, "fr": 2.4, "it": 2.5}

AD_TEMPLATE_CATALOG: dict[str, list[dict[str, Any]]] = {
    "ad_template": TEMPLATES,
    "avatar_trait": AVATAR_TRAITS,
}
