"""The real `ImageEditPort`, on Pillow.

Pillow is already a dependency — `content.services.media` probes every upload
with it — so this adapter adds no vendor, no key and no account. It still sits
behind the port: what makes the port worth having is not that Pillow is remote
but that the *edit* has to be substitutable, and the day a smart-crop service
is worth paying for, this is the module that gets replaced rather than the
service that calls it.

There is deliberately no real `VideoEditPort` here. Trimming needs a codec, a
codec is a system dependency, and Part 7 rule 6 says a fresh checkout runs with
none — so video resolves to `None` until a vendor is chosen, exactly as
`VideoProvider` does.
"""

from __future__ import annotations

import io

from PIL import Image

from content.editing.base import CropBox, EditedMedia

#: Preserves transparency and is lossless, so a crop of a crop does not
#: degrade. Size is not the constraint here — a composer crop is one image.
_OUTPUT_FORMAT = "PNG"
_OUTPUT_MIME = "image/png"


class PillowImageEditor:
    def crop(self, *, content: bytes, box: CropBox) -> EditedMedia:
        with Image.open(io.BytesIO(content)) as image:
            cropped = image.crop((box.left, box.top, box.left + box.width, box.top + box.height))
            buffer = io.BytesIO()
            cropped.convert("RGB").save(buffer, format=_OUTPUT_FORMAT)
            width, height = cropped.size

        return EditedMedia(content=buffer.getvalue(), mime=_OUTPUT_MIME, width=width, height=height)
