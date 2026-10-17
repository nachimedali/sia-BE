"""Reading a public post's picture for the image model (the Playbook).

A port with a fake (rule 6). The real adapter is a server-side fetch of a URL
the Playbook's collector recorded, so it is guarded the way any server-side
fetch must be:

* **Allow-listed hosts, HTTPS only.** `REFERENCE_IMAGE_HOSTS` names the
  providers' own image CDNs. A URL anywhere else is refused before a socket
  opens, so nothing this app stores can be turned into a request to an
  internal address. Redirects are not followed — a redirect is a new host.
* **Bounded.** A byte ceiling read while streaming, a timeout, and an image
  content type; the bytes must then decode as an image.
* **Never stored.** The picture lives in memory for one generation and is
  re-encoded to PNG at a bounded size. It is a composition reference, not an
  asset — nothing here writes to storage or to the library.

A picture that cannot be read is skipped, not fatal: the reference still
contributes its notes and its structure, and the generation records what the
model actually saw.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from PIL import Image

from common.exceptions import ProviderError

logger = logging.getLogger(__name__)

#: The long edge a reference is reduced to. Enough for composition and light;
#: small enough that six of them do not dominate the request.
MAX_EDGE = 768


class ReferenceImageError(ProviderError):
    default_code = "reference_image_unavailable"
    default_detail = "A reference picture could not be read."


class ReferenceImageFetcher(Protocol):
    def fetch(self, url: str) -> bytes:
        """PNG bytes of the picture at `url`, or `ReferenceImageError`."""
        ...


def allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    return any(
        host == entry or (entry.startswith(".") and host.endswith(entry))
        for entry in settings.REFERENCE_IMAGE_HOSTS
    )


def _png(content: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(content)) as img:
            picture = img.convert("RGB")
    except Exception as exc:  # any decoder failure is "not an image"
        raise ReferenceImageError("The reference is not a readable picture.") from exc
    picture.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


class HttpReferenceImageFetcher:
    def fetch(self, url: str) -> bytes:
        if not allowed(url):
            raise ReferenceImageError("That picture is not on a provider's image host.")
        ceiling = int(settings.REFERENCE_IMAGE_MAX_BYTES)
        try:
            with (
                httpx.Client(
                    timeout=settings.REFERENCE_IMAGE_TIMEOUT_SECONDS, follow_redirects=False
                ) as http,
                http.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise ReferenceImageError(f"The image host answered {response.status_code}.")
                kind = response.headers.get("content-type", "")
                if not kind.startswith("image/"):
                    raise ReferenceImageError("The image host did not send a picture.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > ceiling:
                        raise ReferenceImageError("The picture is too large.")
        except httpx.HTTPError as exc:
            raise ReferenceImageError("The image host could not be reached.") from exc
        return _png(bytes(body))


class FakeReferenceImageFetcher:
    """A deterministic picture per URL, so tests and the e2e run compose with
    real pixels and no network. Records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.fail or not url:
            raise ReferenceImageError("The fake was told to fail.")
        # Structured, not flat: a perceptual hash of a flat picture is all
        # zeros and would match every other flat picture, so the fake would
        # trip the reference ceiling for a reason no real photo shares.
        digest = hashlib.sha256(url.encode()).digest()
        img = Image.new("RGB", (64, 64))
        for x in range(64):
            for y in range(64):
                band = digest[(x // 8 + (y // 8) * 8) % 32]
                img.putpixel((x, y), (band, (band * 3 + x * 4) % 256, (255 - band + y * 4) % 256))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


_fake = FakeReferenceImageFetcher()
_override: ReferenceImageFetcher | None = None


def set_override(fetcher: ReferenceImageFetcher | None) -> None:
    global _override
    _override = fetcher


def get_reference_image_fetcher() -> ReferenceImageFetcher:
    if _override is not None:
        return _override
    if settings.USE_FAKE_REFERENCE_IMAGES:
        return _fake
    return HttpReferenceImageFetcher()
