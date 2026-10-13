"""What SocialAPI can report, per platform — the plan's truth table (§5.2).

Seeded into `analytics.MetricCapability` (`provider_key="socialapi"`) by a data
migration — `get_or_create`, so an operator's edit in admin is never
overwritten, and SA-90 flips rows from live evidence, not from this file.

**`available=False` rows matter as much as the others.** Several platforms
answer `0` for a number SocialAPI cannot read (Facebook saves, Threads without
its insights permission, every Google Business post metric); a row marked
unavailable is what turns that `0` into a null in normalisation, and into
"Unavailable — <note>" on screen.

`latency_hours` is the platform's own reporting delay: inside it a figure is
*pending*, not missing.
"""

from __future__ import annotations

from typing import TypedDict


class Row(TypedDict):
    available: bool
    latency_hours: int
    note: str


def _row(available: bool, note: str = "", latency_hours: int = 0) -> Row:
    return {"available": available, "latency_hours": latency_hours, "note": note}


_ALL_OFF = ("impressions", "likes", "comments", "shares", "clicks", "saves", "reach", "views")


def _off(note: str) -> dict[str, Row]:
    return {metric: _row(False, note) for metric in _ALL_OFF}


#: platform → metric → row. Platform keys are ours (`content.models.Platform`).
METRICS: dict[str, dict[str, Row]] = {
    "instagram": {
        "likes": _row(True),
        "comments": _row(True),
        "shares": _row(True, latency_hours=48),
        "saves": _row(True, latency_hours=48),
        "reach": _row(True, latency_hours=48),
        "views": _row(True, latency_hours=48),
        "impressions": _row(False, "Instagram reports views, not impressions"),
        "clicks": _row(False, "Instagram reports profile website taps, not link clicks"),
    },
    "facebook": {
        "likes": _row(True),
        "comments": _row(True),
        "shares": _row(True, "Not reported for Reels"),
        "views": _row(True, "Reels only; other posts report none"),
        "saves": _row(False, "Not reported by Facebook"),
        "reach": _row(False, "Facebook Page insights are still in beta at the provider"),
        "impressions": _row(False, "Facebook Page insights are still in beta at the provider"),
        "clicks": _row(False, "Not reported by Facebook through this provider"),
    },
    "threads": _off("Needs the Threads insights permission, pending Meta's review"),
    "youtube": {
        "likes": _row(True),
        "comments": _row(True),
        "views": _row(True),
        "shares": _row(False, "YouTube does not report shares through its API"),
        "saves": _row(False, "YouTube does not report saves through its API"),
        "reach": _row(False, "YouTube reports views, not reach"),
        "impressions": _row(False, "YouTube reports views, not impressions"),
        "clicks": _row(False, "Not reported by YouTube"),
    },
    "tiktok": _off("Waiting for the provider's TikTok video-list access"),
    "linkedin": _off("LinkedIn shares post analytics for company pages only, in beta"),
    "pinterest": _off("Not reported through this provider yet"),
    "google_business": _off(
        "Google reports no per-post numbers; profile views and actions come from "
        "its performance report"
    ),
    "x": _off("X needs your own developer app on this provider"),
}

#: Whether comments can be read (and, on Advanced, answered) per platform.
COMMENTS: dict[str, bool] = {
    "instagram": True,
    "facebook": True,
    "threads": True,
    "youtube": True,
    "tiktok": False,
    "linkedin": False,
    "pinterest": False,
    "google_business": False,
    "x": False,
}

#: What the person needs on the platform's side before connecting — Meta's and
#: the platforms' own rules, the same for every vendor.
REQUIREMENTS: dict[str, str] = {
    "instagram": "An Instagram professional account (Creator or Business). A personal account "
    "can switch for free in the Instagram app, under Settings, Account type.",
    "facebook": "A Facebook Page you manage. Personal profiles cannot be published to by any app.",
    "threads": "A Threads profile.",
    "youtube": "A YouTube channel.",
    "tiktok": "A TikTok account.",
    "linkedin": "A LinkedIn profile. Company pages are in beta at the provider.",
    "pinterest": "A Pinterest account with at least one board.",
    "google_business": "A Google Business Profile you manage.",
    "x": "Not available through this provider yet.",
}
