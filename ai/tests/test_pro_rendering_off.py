"""S17-43 (amends S19-16): with Pro rendering off, a template render is still
compiled from its timed direction for the package's route — the same words the
S19 goldens hold for that model — never the label beats and appended look of
before. Pro still gates its own scenes, drafts and routes; it no longer gates
whether the model is told a coherent scene (beta render #7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai.models import VideoRender
from ai.providers.fake import _fake_video_provider
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    avatar_for,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db

GOLDENS = Path(__file__).parent / "goldens" / "s19"


@pytest.mark.parametrize("key", ["pour-shot", "desk-break"])
def test_with_the_flag_off_a_template_sends_its_compiled_direction(
    key: str,
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    _fake_video_provider.clear()
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": key,
        "product": product.pk,
        "language": "en",
    }
    if key == "desk-break":
        body["avatar"] = avatar_for(growth, make_png_upload).pk
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    sent = VideoRender.objects.get(pk=render["id"]).prompt_sent
    # Growth's route without Pro is Grok Imagine 1.5 Lite: its golden, word for word.
    assert sent == (GOLDENS / f"{key}-grok.txt").read_text()
    assert "Timing:" not in sent and "Call to action" not in sent


def test_with_the_flag_off_a_reels_shots_get_their_own_prompts(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    _fake_video_provider.clear()
    product = make_product(growth, make_png_upload, photos=2)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "reaction-reveal",
        "product": product.pk,
        "language": "en",
        "avatar": avatar_for(growth, make_png_upload).pk,
    }
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    moving = [call for call in _fake_video_provider.calls if call]
    assert len(moving) >= 1
    assert all("0-" in call for call in moving)
