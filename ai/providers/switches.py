"""Which AI ports run on their fakes.

Text, image and video are separate vendors, so each has its own switch and a
key for one switches only that one on: with only the NVIDIA key set, text is
real while image and video stay on their fakes.

`USE_FAKE_AI_PROVIDERS` is the master switch over all of them. The test suite
and the Playwright run set it, so no test ever reaches a vendor whatever keys
the local `.env` holds.
"""

from __future__ import annotations

from typing import Literal

from django.conf import settings

Port = Literal["TEXT", "IMAGE", "VIDEO", "CHECKS"]


def uses_fake(port: Port) -> bool:
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        return True
    return bool(getattr(settings, f"USE_FAKE_{port}_PROVIDER", False))
