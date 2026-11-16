"""S19-16, flag off: a template render sends today's prompt, byte for byte.

Kept apart from `test_pro_rendering.py` and importing nothing S19 added, so
the golden can be (and was) written by the code as it stood before S19:
`UPDATE_GOLDENS=1` on a checkout without the step.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ai.models import VideoRender
from ai.providers.fake import _fake_video_provider
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db

GOLDENS = Path(__file__).parent / "goldens" / "s19"


def golden(name: str, text: str) -> None:
    path = GOLDENS / name
    if os.environ.get("UPDATE_GOLDENS"):
        path.write_text(text)
    assert text == path.read_text(), name


@pytest.mark.parametrize("key", ["pour-shot", "desk-break", "reaction-reveal"])
def test_with_the_flag_off_a_template_sends_todays_prompt(
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
    if key in ("desk-break", "reaction-reveal"):
        from ai.tests.test_ad_templates import avatar_for

        body["avatar"] = avatar_for(growth, make_png_upload).pk
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    row = VideoRender.objects.get(pk=render["id"])
    golden(f"{key}-today.txt", row.prompt_sent)
    golden(f"{key}-today-calls.txt", "\n---\n".join(_fake_video_provider.calls))
