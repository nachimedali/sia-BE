"""Resolving the two edit ports (P1-12).

Same `USE_FAKE_*` shape as every other port in the project. The asymmetry
between them is the point:

* **Image** always resolves to something. Pillow is already a dependency, so
  there is no deployment in which cropping is unavailable.
* **Video may resolve to `None`.** Trimming needs a codec, and Part 7 rule 6
  says a fresh checkout runs with zero third-party accounts — so until a vendor
  is chosen, `trim_video` says "not configured" plainly rather than a fake
  quietly producing a clip nobody rendered. The same discharge C-11 gave
  `VideoProvider`.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def get_image_editor() -> Any:
    if getattr(settings, "USE_FAKE_MEDIA_EDITOR", False):
        from content.editing.fake import _fake_editor

        return _fake_editor

    from content.editing.pillow import PillowImageEditor

    return PillowImageEditor()


def get_video_editor() -> Any | None:
    if getattr(settings, "USE_FAKE_MEDIA_EDITOR", False):
        from content.editing.fake import _fake_editor

        return _fake_editor
    if not getattr(settings, "VIDEO_EDITOR_BACKEND", ""):
        return None
    raise NotImplementedError(
        "VIDEO_EDITOR_BACKEND is set but no real video editor is implemented yet. "
        "Add one in content/editing/ and resolve it here."
    )
