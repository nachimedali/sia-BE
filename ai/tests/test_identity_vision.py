"""PR-24 — a template's first frame is held to its product by a vision verdict.

dHash compares a 9x8 light/dark layout of the whole frame, so a product placed
in a scene scores low against its packshot whichever vendor drew it (three FLUX
frames scored 0.45-0.55 on beta, 2026-11-07). For a template's first frame the
gate asks the vision model on the constraint-check route instead; every other
image keeps dHash. Never only a lower threshold.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image

from ai.models import Generation, GenerationKind, GenerationMode, GenerationStatus
from ai.providers.fake import FORCE_LOW_SIMILARITY_SENTINEL, FakeTextProvider
from ai.providers.llm_text import LLMTextProvider
from ai.services import quality
from ai.services.pipeline import create_generation, run_generation
from tests.httpx_transport import patch_httpx_transport


def _ramp(rising: bool) -> bytes:
    """A left-to-right ramp; the falling one is its dHash opposite (score 0)."""
    image = Image.new("L", (90, 160))
    image.putdata(
        [(x * 255 // 89 if rising else 255 - x * 255 // 89) for _y in range(160) for x in range(90)]
    )
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _gate(fake: FakeTextProvider, *, vision: bool, references: int = 1) -> quality.QualityResult:
    return quality.run_image_quality_gate(
        content=_ramp(True),
        requested_aspect="9:16",
        reference_images=[_ramp(False)] * references,
        restrictions=[],
        text_provider=fake,
        identity_similarity_threshold=0.6,
        identity_by_vision=vision,
    )


def test_a_template_frame_passes_on_the_vision_verdict_where_dhash_fails() -> None:
    fake = FakeTextProvider()

    by_layout = _gate(fake, vision=False)
    assert not by_layout.passed and "product_identity" in by_layout.rejected_reason
    assert fake.identity_checks == []

    by_vision = _gate(fake, vision=True)
    assert by_vision.passed
    assert by_vision.checks["product_identity"] == {"passed": True, "method": "vision"}
    assert by_vision.identity_score is None
    assert len(fake.identity_checks) == 1


def test_the_vision_verdict_can_refuse() -> None:
    fake = FakeTextProvider()
    fake.same_product_answer = False

    result = _gate(fake, vision=True)

    assert not result.passed
    assert result.checks["product_identity"] == {"passed": False, "method": "vision"}
    assert result.rejected_reason == "product_identity: the vision check did not find the product"


def test_at_most_three_product_photos_travel_to_the_vision_check() -> None:
    fake = FakeTextProvider()
    _gate(fake, vision=True, references=5)
    assert fake.identity_checks[0]["references"] == 3


@pytest.mark.parametrize(
    ("reply", "same"),
    [("YES", True), ("yes.", True), ("No, the label differs.", False), ("", False)],
)
def test_the_real_adapter_sends_the_photos_then_the_frame_and_reads_yes_or_no(
    monkeypatch: Any, reply: str, same: bool
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    patch_httpx_transport(monkeypatch, handler)

    answer = LLMTextProvider().same_product(image_bytes=b"frame", reference_images=[b"one", b"two"])

    assert answer is same
    parts = sent[0]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "YES or NO" in parts[0]["text"]
    urls = [part["image_url"]["url"] for part in parts[1:]]
    assert [base64.b64decode(url.split(",", 1)[1]) for url in urls] == [b"one", b"two", b"frame"]


@pytest.mark.django_db
def test_a_plain_product_image_keeps_dhash_and_never_asks_the_vision_model(
    workspace: Any,
    user: Any,
    product_with_reference_image: Any,
    generation_costs: Any,
    text_provider: Any,
) -> None:
    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt=f"the mug on a table {FORCE_LOW_SIMILARITY_SENTINEL}",
        product=product_with_reference_image,
    )
    run_generation(generation, n=1)

    generation = Generation.objects.get(pk=generation.pk)
    assert generation.status == GenerationStatus.FAILED
    assert text_provider.identity_checks == []
