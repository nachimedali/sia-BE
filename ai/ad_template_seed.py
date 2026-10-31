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

_NEGATIVE = (
    "no resemblance to any real or famous person, no brand logos except the product's own, "
    "no readable text except the product's own, no extra fingers, no warped hands, no product "
    "redesign, no watermark"
)

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
    shots: tuple[tuple[str, str], ...] = (),
    premium: bool = False,
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
            "units": {length: _units(length, tier, category) for length in lengths},
            "beats": list(beats),
            "keyframe_prompt": keyframe,
            "motion_prompt": f"{motion} The product never changes shape, label or colour.",
            "negative": _NEGATIVE,
            "script": {"hook": script[0], "body": script[1], "cta": script[2]},
            "hooks": list(hooks),
            "locations": list(locations),
            "needs": needs,
            "available": available,
            "premium": premium,
            "scene": scene,
            "seed": SEED_VERSION,
            **({"shots": [{"use": use, "text": text} for use, text in shots]} if shots else {}),
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
        locations=("living-room", "loft"),
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
        locations=("living-room", "studio"),
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
        locations=("trail", "dunes", "beach"),
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
        script=("Out with {product}.", "It goes everywhere I go.", "Find yours."),
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
        locations=("kitchen", "office", "studio"),
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
        locations=("loft", "living-room"),
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
        script=("3 p.m. slump?", "{product} gets me through.", "Try it today."),
        hooks=("3 p.m. slump?", "Study break.", "How I get through the afternoon."),
        locations=("office", "kitchen"),
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
        locations=("workshop", "studio"),
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
        locations=("workshop", "kitchen"),
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
            "A hand holding a phone up in an airport terminal, departure boards soft behind, the "
            "app's screen clearly visible."
        ),
        motion=(
            "Walking handheld feel, the phone held steady while the terminal drifts past behind "
            "it; one thumb tap."
        ),
        script=("Sorted before boarding.", "{product} does it in a tap.", "Download it free."),
        hooks=("Sorted before boarding.", "Travel hack.", "Done before the gate."),
        locations=("airport", "street"),
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
            "Phone-camera close-up of a person reacting with genuine surprise, hand half over "
            "their mouth, eyes on something just out of frame."
        ),
        motion=(
            "Subtle handheld drift and a slow push-in; small natural movements only, nothing "
            "changes shape."
        ),
        script=("Wait, what?", "{product}. That's what.", "See it for yourself."),
        hooks=("Wait, what?", "I did not expect this.", "Okay, this is good."),
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
        script=("I was about to give up.", "Then {product} did it for me.", "Download it free."),
        hooks=("I was about to give up.", "This app just saved me.", "Why did nobody tell me?"),
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
        script=("Why people switch.", "Made better, built to last, easy to love.", "Try it."),
        hooks=("Why people switch.", "Three reasons.", "Sound off? Read this."),
        locations=("studio",),
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
        script=("This week only.", "{product}, on offer.", "Shop now."),
        hooks=("This week only.", "Last chance.", "Don't scroll past this."),
        locations=("studio",),
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
        locations=("studio",),
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
            "The camera orbits slowly halfway round the product, then pushes in to a macro "
            "detail; perfectly smooth, the product never moves or changes."
        ),
        script=("Look again.", "{product}, every angle.", "Discover it."),
        hooks=("Look again.", "Built to be seen.", "The detail."),
        locations=("studio",),
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
        locations=("kitchen", "studio"),
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
        hooks=("Swatch check.", "No filter.", "On real skin."),
        locations=("studio",),
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
        script=("As a professional, here's what I use.", "Why {product} works.", "Learn more."),
        hooks=("As a professional, here's what I use.", "What I'd tell a client.", "Expert tip."),
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
        script=("Is it actually worth it?", "I looked into {product}.", "See what I found."),
        hooks=("Is it actually worth it?", "I had to find out.", "Let's investigate."),
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
        locations=("street",),
        available=False,
        description=_TALK,
        scene="re5-5",
        sort=62,
    ),
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
