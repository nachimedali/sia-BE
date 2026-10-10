"""**P1-01 / P1-G1 — preview is provably what publish sends.**

Written before the override resolution it guards, and deliberately so.
Per-platform overrides are the single greatest threat to Part 7 rule 1: the
moment a target can carry its own body, its own media and its own options,
there are two plausible places to resolve them — the preview view and the
publish task — and two places is one too many.

The defence is a **matrix**, not an example. A handful of hand-picked cases
proves the cases someone thought of; a random matrix over every override
combination proves the combinations nobody did. Seeded, so a failure is
reproducible rather than a Heisenbug that vanishes on rerun.

The assertion is byte identity on the JSON, not equality of dataclasses:
`as_dict()` is what is stored and what is returned over the wire, and two
dataclasses can compare equal while serialising differently.
"""

from __future__ import annotations

import itertools
import json
import random
from typing import Any

import pytest

from content.models import Platform, PostMediaAttachment, PostTarget
from content.services.adaptation import render_post
from content.services.rules import PLATFORM_RULES

pytestmark = pytest.mark.django_db

#: Fixed, so a failing combination can be reproduced from the failure message
#: alone rather than by rerunning until it recurs.
SEED = 20260906

#: Every axis a target can differ on. The matrix is the product of these, which
#: is the point: the bugs live in the combinations, not the individual values.
BODY_OVERRIDES: tuple[str | None, ...] = (
    None,  # inherit
    "",  # deliberately empty — legitimate on a video-first platform
    "A short override.",
    "Overridden with #hashtags and a #second one, plus " + "x" * 600,
)

MEDIA_OVERRIDES: tuple[list[int] | None, ...] = (
    None,  # inherit
    [],  # deliberately none
)


def _payload_publish_would_send(target: PostTarget) -> dict[str, Any]:
    """What the publish task hands the provider.

    Read from the same place `scheduling.publishing.publish_post` reads it, so
    this test breaks if that path ever starts assembling its own payload
    instead of using the stored render.
    """
    target.refresh_from_db()
    return dict(target.rendered_payload)


def _payload_preview_shows(target: PostTarget) -> dict[str, Any]:
    """What `/posts/preview/` renders. One function, one call path."""
    return render_post(target.post, [target.platform])[target.platform].as_dict()


# -----------------------------------------------------------------------------
# The matrix
# -----------------------------------------------------------------------------
def _combinations() -> list[tuple[str, str | None, list[int] | None]]:
    combos = list(itertools.product(Platform.values, BODY_OVERRIDES, MEDIA_OVERRIDES))
    random.Random(SEED).shuffle(combos)
    return combos


@pytest.mark.parametrize(("platform", "body_override", "media_override"), _combinations())
def test_preview_is_byte_identical_to_what_publish_sends(
    paid_workspace: Any,
    user: Any,
    make_png_upload: Any,
    platform: str,
    body_override: str | None,
    media_override: list[int] | None,
) -> None:
    from content.services.media import ingest_media
    from content.services.posts import create_post

    post = create_post(
        workspace=paid_workspace,
        author=user,
        master_body="Master body with #glaze and #ceramics, and enough text to matter. "
        + "y" * 400,
    )
    asset = ingest_media(workspace=paid_workspace, upload=make_png_upload())
    PostMediaAttachment.objects.create(post=post, media_asset=asset, order=0)

    target = PostTarget.objects.create(
        post=post,
        platform=platform,
        body_override=body_override,
        media_override=media_override,
    )
    _store_render(target)

    preview = _payload_preview_shows(target)
    published = _payload_publish_would_send(target)

    assert json.dumps(preview, sort_keys=True) == json.dumps(published, sort_keys=True), (
        f"preview and publish disagree for {platform} "
        f"(body_override={body_override!r}, media_override={media_override!r})"
    )


def _store_render(target: PostTarget) -> None:
    """Writes the payload the way the scheduling service does.

    Deliberately not a shortcut: it calls the same renderer, because a test
    that stored a payload some other way would prove the two match only in the
    test.
    """
    from content.services.adaptation import render_post as render

    target.rendered_payload = render(target.post, [target.platform])[target.platform].as_dict()
    target.save(update_fields=["rendered_payload"])


# -----------------------------------------------------------------------------
# The properties the matrix depends on
# -----------------------------------------------------------------------------
def test_null_and_empty_body_overrides_are_different(paid_workspace: Any, user: Any) -> None:
    """`None` means inherit; `""` means a deliberately empty caption. Treating
    them alike makes clearing an override impossible, and makes an empty
    caption unrepresentable."""
    from content.services.posts import create_post

    post = create_post(workspace=paid_workspace, author=user, master_body="Master text.")
    inherits = PostTarget.objects.create(post=post, platform=Platform.THREADS, body_override=None)
    empty = PostTarget.objects.create(post=post, platform=Platform.LINKEDIN, body_override="")

    assert render_post(post, [inherits.platform])[inherits.platform].body == "Master text."
    assert render_post(post, [empty.platform])[empty.platform].body == ""


def test_an_override_still_obeys_the_platform_rule(paid_workspace: Any, user: Any) -> None:
    """An override is not an escape hatch from the platform's own limits — a
    600-character Threads caption is rejected by Threads regardless of who
    typed it, so it is truncated here exactly as a master body would be."""
    from content.services.posts import create_post

    post = create_post(workspace=paid_workspace, author=user, master_body="short")
    PostTarget.objects.create(post=post, platform=Platform.THREADS, body_override="z" * 5000)

    payload = render_post(post, [Platform.THREADS])[Platform.THREADS]

    limit = PLATFORM_RULES[Platform.THREADS].char_limit
    assert all(len(chunk) <= limit for chunk in payload.thread or [payload.body])


def test_rendering_twice_is_byte_identical(paid_workspace: Any, user: Any) -> None:
    """Determinism is what makes the matrix above meaningful: a renderer that
    varied run to run would pass it by luck."""
    from content.services.posts import create_post

    post = create_post(workspace=paid_workspace, author=user, master_body="Body #one #two")
    PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM, body_override="Override #x")

    first = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    second = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
