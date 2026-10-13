"""SocialAPI's capability rows (`social-provider-plan.md` §5.2).

`get_or_create` only: an operator's edit in admin is never overwritten, and a
new default for an existing database is a new migration, never an edit here.

The rows are frozen here rather than imported from
`social_providers.socialapi.capabilities`: a migration that read the live table
would seed whatever that file says on the day a fresh database migrates, not
what it said when this migration was written.
"""

import datetime as dt

from django.db import migrations

from common.seeding import seeding_enabled

#: (platform, metric, available, latency hours, note) — as of 2026-10-13.
ROWS = [
    ("instagram", "likes", True, 0, ""),
    ("instagram", "comments", True, 0, ""),
    ("instagram", "shares", True, 48, ""),
    ("instagram", "saves", True, 48, ""),
    ("instagram", "reach", True, 48, ""),
    ("instagram", "views", True, 48, ""),
    ("instagram", "impressions", False, 0, "Instagram reports views, not impressions"),
    ("instagram", "clicks", False, 0, "Instagram reports profile website taps, not link clicks"),
    ("facebook", "likes", True, 0, ""),
    ("facebook", "comments", True, 0, ""),
    ("facebook", "shares", True, 0, "Not reported for Reels"),
    ("facebook", "views", True, 0, "Reels only; other posts report none"),
    ("facebook", "saves", False, 0, "Not reported by Facebook"),
    ("facebook", "reach", False, 0, "Facebook Page insights are still in beta at the provider"),
    (
        "facebook",
        "impressions",
        False,
        0,
        "Facebook Page insights are still in beta at the provider",
    ),
    ("facebook", "clicks", False, 0, "Not reported by Facebook through this provider"),
    (
        "threads",
        "impressions",
        False,
        0,
        "Needs the Threads insights permission, pending Meta's review",
    ),
    ("threads", "likes", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    (
        "threads",
        "comments",
        False,
        0,
        "Needs the Threads insights permission, pending Meta's review",
    ),
    ("threads", "shares", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    ("threads", "clicks", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    ("threads", "saves", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    ("threads", "reach", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    ("threads", "views", False, 0, "Needs the Threads insights permission, pending Meta's review"),
    ("youtube", "likes", True, 0, ""),
    ("youtube", "comments", True, 0, ""),
    ("youtube", "views", True, 0, ""),
    ("youtube", "shares", False, 0, "YouTube does not report shares through its API"),
    ("youtube", "saves", False, 0, "YouTube does not report saves through its API"),
    ("youtube", "reach", False, 0, "YouTube reports views, not reach"),
    ("youtube", "impressions", False, 0, "YouTube reports views, not impressions"),
    ("youtube", "clicks", False, 0, "Not reported by YouTube"),
    ("tiktok", "impressions", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "likes", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "comments", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "shares", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "clicks", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "saves", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "reach", False, 0, "Waiting for the provider's TikTok video-list access"),
    ("tiktok", "views", False, 0, "Waiting for the provider's TikTok video-list access"),
    (
        "linkedin",
        "impressions",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "likes",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "comments",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "shares",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "clicks",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "saves",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "reach",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    (
        "linkedin",
        "views",
        False,
        0,
        "LinkedIn shares post analytics for company pages only, in beta",
    ),
    ("pinterest", "impressions", False, 0, "Not reported through this provider yet"),
    ("pinterest", "likes", False, 0, "Not reported through this provider yet"),
    ("pinterest", "comments", False, 0, "Not reported through this provider yet"),
    ("pinterest", "shares", False, 0, "Not reported through this provider yet"),
    ("pinterest", "clicks", False, 0, "Not reported through this provider yet"),
    ("pinterest", "saves", False, 0, "Not reported through this provider yet"),
    ("pinterest", "reach", False, 0, "Not reported through this provider yet"),
    ("pinterest", "views", False, 0, "Not reported through this provider yet"),
    (
        "google_business",
        "impressions",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "likes",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "comments",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "shares",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "clicks",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "saves",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "reach",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    (
        "google_business",
        "views",
        False,
        0,
        "Google reports no per-post numbers; profile views and actions come from its performance "
        "report",
    ),
    ("x", "impressions", False, 0, "X needs your own developer app on this provider"),
    ("x", "likes", False, 0, "X needs your own developer app on this provider"),
    ("x", "comments", False, 0, "X needs your own developer app on this provider"),
    ("x", "shares", False, 0, "X needs your own developer app on this provider"),
    ("x", "clicks", False, 0, "X needs your own developer app on this provider"),
    ("x", "saves", False, 0, "X needs your own developer app on this provider"),
    ("x", "reach", False, 0, "X needs your own developer app on this provider"),
    ("x", "views", False, 0, "X needs your own developer app on this provider"),
]


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    capability = apps.get_model("analytics", "MetricCapability")
    for platform, metric_key, available, latency_hours, note in ROWS:
        capability.objects.get_or_create(
            provider_key="socialapi",
            platform=platform,
            metric_key=metric_key,
            defaults={
                "available": available,
                "latency_hint": dt.timedelta(hours=latency_hours) if latency_hours else None,
                "note": note,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("analytics", "0010_postmetric_reach_postmetric_views")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
