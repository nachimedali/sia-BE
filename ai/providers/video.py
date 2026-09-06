"""Resolving the video provider (C-11 / P0-04).

Separate module from the image provider because they are separate vendors —
nobody's image endpoint is their video endpoint — and because this one has the
distinguishing property that **it may legitimately resolve to nothing**.

That is the whole discharge of C-11's video item. The entitlement gate was
correct and stays; what was missing was a port behind it. Now a fresh checkout
generates video against the fake (Part 7 rule 6), and a production deployment
with no vendor configured says so plainly rather than pretending.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def get_video_provider() -> Any | None:
    """The configured provider, or `None` when there is not one.

    `None` rather than an exception: "no video vendor is configured" is a
    deployment fact the pipeline turns into a clear 422 for the user, not an
    error condition callers should have to catch.
    """
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        from ai.providers.fake import _fake_video_provider

        return _fake_video_provider
    if not getattr(settings, "VIDEO_PROVIDER_API_KEY", ""):
        return None
    raise NotImplementedError(
        "VIDEO_PROVIDER_API_KEY is set but no real video adapter is implemented yet. "
        "Add one in ai/providers/ and resolve it here."
    )
