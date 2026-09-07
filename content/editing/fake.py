"""Deterministic editors for tests and a fresh checkout (A8).

Records what it was asked to do, so a test can assert the *request* rather than
inspecting pixels — the interesting property of an edit path is which box was
sent, not whether Pillow can crop.

The crop is real, not stubbed: it produces an image of the requested size, so a
caller that stores width and height off the result gets truthful numbers and
the fake cannot make a broken pipeline look green.
"""

from __future__ import annotations

import io

from PIL import Image

from content.editing.base import CropBox, EditedMedia


class FakeMediaEditor:
    def __init__(self) -> None:
        self.crops: list[tuple[int, int, int, int]] = []
        self.trims: list[tuple[int, int]] = []

    def clear(self) -> None:
        self.crops.clear()
        self.trims.clear()

    def crop(self, *, content: bytes, box: CropBox) -> EditedMedia:
        self.crops.append((box.left, box.top, box.width, box.height))
        buffer = io.BytesIO()
        Image.new("RGB", (box.width, box.height), color=(120, 130, 200)).save(buffer, format="PNG")
        return EditedMedia(
            content=buffer.getvalue(), mime="image/png", width=box.width, height=box.height
        )

    def trim(self, *, content: bytes, start_ms: int, end_ms: int) -> EditedMedia:
        self.trims.append((start_ms, end_ms))
        # The bytes are passed through: a fake that re-encoded video would need
        # a codec, which is the dependency this port exists to keep optional.
        return EditedMedia(content=content, mime="video/mp4", duration_ms=max(0, end_ms - start_ms))


#: Module-level, like the other fakes, so a test can inspect its calls after a
#: view has run. Reset between tests by an autouse fixture in `conftest.py`.
_fake_editor = FakeMediaEditor()
