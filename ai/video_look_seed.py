"""What places, lights, moods and cast vibes add to a video prompt (S19-07).

`upgrade_rendering.md` §8. Every value lands in the row's `metadata` under a
new key, so the words the image model reads (`prompt_fragment`) never change:
with Pro rendering off, nothing here is read. `ai/0052` adds a key only where
the row has none, so an operator's own value is never overwritten.

* place  — `ambient`: the room tone a model with sound gives the clip;
           `depth`: what sits in front of and behind the product, for SCENE.
* light  — `keyword`: the words a video model weighs first;
           `conflicts`: the places whose own light it would fight (a 400).
* mood   — `visible`: the mood as what the camera sees; `motion`: the camera
           a scene with none of its own takes.
* vibe   — `performance`: an attitude for the person, not a gesture.
"""

from __future__ import annotations

from collections.abc import Mapping

_OUTDOOR = ["beach", "picnic", "trail", "seaside", "terrace", "rooftop"]
_NO_SUN = ["studio", "brand-colour"]

PLACES: dict[str, dict[str, str]] = {
    "studio": {
        "ambient": "quiet studio room tone",
        "depth": "nothing in front, the plinth in the middle, the seamless backdrop behind",
    },
    "brand-colour": {
        "ambient": "quiet studio room tone",
        "depth": "nothing in front, the plinth in the middle, the coloured backdrop behind",
    },
    "kitchen": {
        "ambient": "a soft kitchen hum and a distant kettle",
        "depth": "the counter edge in front, the product mid-counter, the window soft behind",
    },
    "cafe-table": {
        "ambient": "café murmur and cups on saucers",
        "depth": "the cup in front, the product mid-table, the café soft behind",
    },
    "terrace": {
        "ambient": "a light breeze and distant street sounds",
        "depth": "the table edge in front, the product mid-table, white walls behind",
    },
    "rooftop": {
        "ambient": "wind and a distant city",
        "depth": "the parapet in front, the product on it, the rooftops behind",
    },
    "seaside": {
        "ambient": "waves below and a light wind",
        "depth": "the stone wall in front, the product on it, the sea and hills behind",
    },
    "beach": {
        "ambient": "gentle waves and wind",
        "depth": "sand in front, the product on the mat, the sea behind",
    },
    "picnic": {
        "ambient": "birdsong and leaves in the wind",
        "depth": "the blanket in front, the product beside the basket, trees behind",
    },
    "market": {
        "ambient": "market murmur and a distant radio",
        "depth": "a crate edge in front, the product among baskets, the aisle soft behind",
    },
    "shop-counter": {
        "ambient": "a shop's quiet and a door chime",
        "depth": "the counter edge in front, the product on it, shelves soft behind",
    },
    "living-room": {
        "ambient": "a quiet room and a distant street",
        "depth": "the rug in front, the product on the table, the sofa behind",
    },
    "loft": {
        "ambient": "a quiet loft and a distant city",
        "depth": "the rug in front, the product in the middle, the steel windows behind",
    },
    "entryway": {
        "ambient": "a door and footsteps outside",
        "depth": "the doormat in front, the product on the floor, the door ajar behind",
    },
    "bedside": {
        "ambient": "a quiet night room",
        "depth": "the table edge in front, the product by the lamp, the dim room behind",
    },
    "bathroom-shelf": {
        "ambient": "a quiet bathroom and a dripping tap",
        "depth": "the basin edge in front, the product on the shelf, towels behind",
    },
    "salon": {
        "ambient": "a hairdryer in the distance and low music-free chatter",
        "depth": "the trolley in front, the product at the station, the mirror behind",
    },
    "office": {
        "ambient": "quiet room tone and keyboard taps",
        "depth": "papers in front, the product on the desk, the window behind",
    },
    "workshop": {
        "ambient": "a workshop's quiet and a tool set down",
        "depth": "the bench edge in front, the product on it, the pegboard behind",
    },
    "gym": {
        "ambient": "gym echo and a distant weight clank",
        "depth": "the bench in front, the product on it, the dumbbell rack behind",
    },
    "street": {
        "ambient": "street traffic and footsteps",
        "depth": "the pavement in front, the product held up, the shop window behind",
    },
    "trail": {
        "ambient": "wind and footsteps on gravel",
        "depth": "dry grass in front, the product on the trail, the hillside behind",
    },
    "car-interior": {
        "ambient": "a parked car's quiet and a distant road",
        "depth": "the seat edge in front, the product on the passenger seat, the windscreen behind",
    },
}

LIGHTS: dict[str, dict[str, object]] = {
    "soft-daylight": {"keyword": "soft window light", "conflicts": []},
    "golden-hour": {"keyword": "golden hour cinematography", "conflicts": _NO_SUN},
    "high-key": {"keyword": "soft box three-point lighting", "conflicts": _OUTDOOR},
    "low-key": {"keyword": "chiaroscuro lighting", "conflicts": _OUTDOOR},
    "hard-sun": {
        "keyword": "hard midday sun",
        "conflicts": [*_NO_SUN, "bedside", "bathroom-shelf", "salon", "market"],
    },
    "overcast": {"keyword": "diffused cloudy daylight", "conflicts": [*_NO_SUN, "bedside"]},
    "blue-hour": {"keyword": "blue hour with warm practical lights", "conflicts": _NO_SUN},
    "neon-gel": {
        "keyword": "neon gel lighting, magenta and teal",
        "conflicts": ["beach", "picnic", "trail", "seaside"],
    },
}

MOODS: dict[str, dict[str, str]] = {
    "editorial": {
        "visible": "the product on the left third, the top third left plain",
        "motion": "locked off",
    },
    "clean": {
        "visible": "a seamless backdrop, at most two props, even soft light",
        "motion": "locked off",
    },
    "warm": {
        "visible": "3200K practical light, amber highlights, wood and linen",
        "motion": "slow dolly in",
    },
    "airy": {
        "visible": "a stop brighter, lifted shadows, a pale pastel backdrop, sheer-curtain light",
        "motion": "a gentle drift",
    },
    "vibrant": {
        "visible": "saturated primary colours, hard light, strong contrast",
        "motion": "a quicker dolly in",
    },
    "cinematic": {
        "visible": "35 mm anamorphic look, shallow depth, a teal-and-amber grade",
        "motion": "a slow arc",
    },
    "moody": {
        "visible": "deep shadow, one hard side light, a dark backdrop",
        "motion": "static",
    },
    "film": {
        "visible": "35 mm grain, rolled-off highlights",
        "motion": "handheld with a light micro-shake",
    },
    "natural": {
        "visible": "raw wood, stone and linen, unretouched surfaces, daylight",
        "motion": "handheld, unhurried",
    },
    "playful": {
        "visible": "one prop at an unexpected scale, bold colour blocks, the frame tilted 10°",
        "motion": "a quick pan",
    },
}

VIBES: dict[str, str] = {
    "everyday": "relaxed and unhurried, as if no one is filming",
    "elevated-minimal": "slow, deliberate hand movements",
    "luxe": "savours the moment; small, precise gestures",
    "active": "energetic, breath visible, quick movements",
    "family": "people turn to each other and laugh, hands reach across",
    "street": "walks with purpose, chin up, a direct look at the lens",
}

#: kind → key → the metadata keys that kind gains.
BY_KIND: dict[str, Mapping[str, Mapping[str, object]]] = {
    "place": PLACES,
    "light": LIGHTS,
    "mood": MOODS,
    "vibe": {key: {"performance": line} for key, line in VIBES.items()},
}
