"""Composing from Playbook references: what the model may see and must not copy.

Three guarantees, each tested where it is enforced:

* the server only ever fetches a picture from a provider's own image host,
  over HTTPS, bounded in size and type — never an address a user chose;
* the rules about what a reference may lend are added verbatim, after any
  rewrite, and name every attached picture;
* a render too close to a reference is refused by the quality gate, however
  good it is.
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from ai.providers.fake import _fake_text_provider
from ai.providers.reference_images import (
    FakeReferenceImageFetcher,
    HttpReferenceImageFetcher,
    ReferenceImageError,
    allowed,
)
from ai.services import quality, references, rephrase


def _png(seed: int, size: tuple[int, int] = (96, 96)) -> bytes:
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), ((x * seed) % 256, (y * 7 + seed) % 256, (x * y) % 256))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# --- the fetch is guarded --------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://i.ytimg.com/vi/abc/hqdefault.jpg", True),
        ("https://yt3.ggpht.com/a.jpg", True),
        ("http://i.ytimg.com/vi/abc/hqdefault.jpg", False),  # not HTTPS
        ("https://evil.example/i.ytimg.com.jpg", False),
        ("https://i.ytimg.com.evil.example/a.jpg", False),
        ("https://user:pw@i.ytimg.com/a.jpg", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("https://localhost/a.png", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_only_provider_image_hosts_are_fetched(url: str, ok: bool) -> None:
    assert allowed(url) is ok


def test_a_refused_host_never_opens_a_socket() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://169.254.169.254/latest/meta-data")
        with pytest.raises(ReferenceImageError):
            HttpReferenceImageFetcher().fetch("https://169.254.169.254/latest/meta-data")
        assert not route.called


@respx.mock
def test_a_picture_is_read_and_normalised() -> None:
    big = io.BytesIO()
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(big, format="JPEG")
    respx.get("https://i.ytimg.com/vi/a/hq.jpg").mock(
        return_value=httpx.Response(
            200, content=big.getvalue(), headers={"content-type": "image/jpeg"}
        )
    )

    content = HttpReferenceImageFetcher().fetch("https://i.ytimg.com/vi/a/hq.jpg")

    with Image.open(io.BytesIO(content)) as img:
        assert img.format == "PNG"
        assert max(img.size) <= 768


@respx.mock
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "302"),
        (httpx.Response(404), "404"),
        (
            httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
            "did not send",
        ),
        (
            httpx.Response(200, content=b"not a picture", headers={"content-type": "image/png"}),
            "readable",
        ),
    ],
)
def test_anything_but_a_picture_is_refused(response: httpx.Response, message: str) -> None:
    respx.get("https://i.ytimg.com/x.jpg").mock(return_value=response)

    with pytest.raises(ReferenceImageError, match=message):
        HttpReferenceImageFetcher().fetch("https://i.ytimg.com/x.jpg")


@respx.mock
def test_a_picture_over_the_ceiling_is_refused(settings: Any) -> None:
    settings.REFERENCE_IMAGE_MAX_BYTES = 1000
    respx.get("https://i.ytimg.com/x.jpg").mock(
        return_value=httpx.Response(
            200, content=b"x" * 5000, headers={"content-type": "image/jpeg"}
        )
    )

    with pytest.raises(ReferenceImageError, match="too large"):
        HttpReferenceImageFetcher().fetch("https://i.ytimg.com/x.jpg")


# --- the gate refuses a near-copy ------------------------------------------------------
def _gate(content: bytes, inspiration: list[bytes]) -> quality.QualityResult:
    return quality.run_image_quality_gate(
        content=content,
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=_fake_text_provider,
        identity_similarity_threshold=0.6,
        inspiration_images=inspiration,
    )


def test_a_render_that_copies_a_reference_is_refused() -> None:
    reference = _png(3)

    result = _gate(reference, [_png(11), reference])

    assert not result.passed
    check = result.checks["reference_distance"]
    assert check["closest"] == 1
    assert check["score"] > quality.REFERENCE_MAX_SIMILARITY
    assert "too close to a reference" in result.rejected_reason


def test_a_new_picture_passes_and_is_scored() -> None:
    result = _gate(_png(5), [_png(91)])

    assert result.passed
    assert result.checks["reference_distance"]["score"] <= quality.REFERENCE_MAX_SIMILARITY


def test_the_reference_ceiling_is_the_repurpose_judgement() -> None:
    assert quality.REFERENCE_MAX_SIMILARITY == quality.REPURPOSE_MAX_SIMILARITY


# --- what the models are told ----------------------------------------------------------
REFS: list[dict[str, Any]] = [
    {
        "post": 1,
        "provider": "youtube",
        "format": "SHORT",
        "title": "Pouring the first press",
        "creator": "dar.zitouna",
        "image": True,
        "thumbnail_url": "https://i.ytimg.com/1.jpg",
        "note": "The slow pour",
        "take": ["light", "colours"],
        "influence": "strong",
    },
    {
        "post": 2,
        "provider": "google_trends",
        "format": "SEARCH",
        "title": "Olive harvest",
        "image": False,
    },
    {
        "post": 3,
        "provider": "youtube",
        "format": "VIDEO",
        "title": "Kiln reveal",
        "image": True,
        "thumbnail_url": "https://i.ytimg.com/3.jpg",
        "take": [],
        "influence": "light",
    },
]


def test_brief_lines_name_what_each_reference_lends() -> None:
    lines = references.brief_lines(REFS)

    assert lines[0] == (
        "R1 (YouTube short video “Pouring the first press” by dar.zitouna), follow it closely: "
        "take the light: its direction, softness and time of day; the colour palette. "
        'The user likes: "The slow pour".'
    )
    assert lines[1].startswith("R2 is a Google search rising today")
    assert "only if it fits the product naturally" in lines[1]
    assert "a light nod: take its overall composition and light" in lines[2]


def test_rule_lines_number_every_attached_picture() -> None:
    lines = references.rule_lines(REFS, product_images=2, seen=["R1"])

    assert lines[0] == (
        "Attached, in order: images 1 to 2 are photographs of the product: reproduce the "
        "product exactly as shown; image 3 is a composition reference (R1)."
    )
    assert "Never reproduce their subject, people, faces" in lines[1]
    # R3's picture could not be read; R2 is a search and never had one.
    assert "No picture is attached for R3: follow the description only." in lines
    assert "Do not depict real people, news events or other brands from any reference." in lines


def test_only_pictures_the_playbook_allowed_are_read() -> None:
    fetcher = FakeReferenceImageFetcher()
    from ai.providers import reference_images

    reference_images.set_override(fetcher)
    try:
        seen = references.images(REFS)
    finally:
        reference_images.set_override(None)

    assert [image.label for image in seen] == ["R1", "R3"]
    assert fetcher.calls == ["https://i.ytimg.com/1.jpg", "https://i.ytimg.com/3.jpg"]


def test_a_rewrite_that_adds_a_number_falls_back_to_the_lines(monkeypatch: Any) -> None:
    from ai.providers.base import TextGenerationResult, TextVariant

    class Inventive:
        def generate(self, **_: Any) -> TextGenerationResult:
            return TextGenerationResult(
                variants=[TextVariant(body="A bottle with 40% more flavour, glowing at dusk.")],
                provider="x",
                model="m",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
            )

    monkeypatch.setattr(rephrase, "get_text_provider", lambda *_a, **_k: Inventive())

    result = rephrase.reference_brief(
        idea="", product="Djerba olive oil", references=references.brief_lines(REFS[:1]), choices=[]
    )

    assert result.by == rephrase.TEMPLATE
    assert "R1 (YouTube short video" in result.text
