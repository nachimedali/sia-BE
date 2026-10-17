"""Each AI port has its own fake switch.

Text (NVIDIA), image and video are separate vendors, so a key for one must
switch that one on and leave the others on their fakes. `USE_FAKE_AI_PROVIDERS`
stays as the master switch that forces every port onto its fake — the test
suite and the Playwright run use it so no test ever reaches a vendor.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.providers.embeddings import OpenAIEmbeddingProvider, get_embedding_provider
from ai.providers.fake import (
    FakeImageProvider,
    FakeTextProvider,
    _fake_embedding_provider,
    _fake_video_composer,
    _fake_video_provider,
)
from ai.providers.llm_text import LLMTextProvider, get_text_provider
from ai.providers.nanobanana_image import NanoBananaImageProvider, get_image_provider
from ai.providers.video import get_video_composer, get_video_provider
from brand.inference import FakeBrandInference, LLMBrandInference, get_brand_inference


@pytest.fixture
def ports(settings: Any) -> Any:
    """No master switch; every port real until a test says otherwise."""
    settings.USE_FAKE_AI_PROVIDERS = False
    settings.USE_FAKE_TEXT_PROVIDER = False
    settings.USE_FAKE_IMAGE_PROVIDER = False
    settings.USE_FAKE_VIDEO_PROVIDER = False
    settings.VIDEO_PROVIDER_API_KEY = ""
    settings.VIDEO_COMPOSER_API_KEY = ""
    return settings


def test_text_can_be_real_while_image_and_video_stay_fake(ports: Any) -> None:
    ports.USE_FAKE_IMAGE_PROVIDER = True
    ports.USE_FAKE_VIDEO_PROVIDER = True

    assert isinstance(get_text_provider(), LLMTextProvider)
    assert isinstance(get_image_provider(), FakeImageProvider)
    assert get_video_provider() is _fake_video_provider
    assert get_video_composer() is _fake_video_composer


def test_image_can_be_real_while_text_stays_fake(ports: Any) -> None:
    ports.USE_FAKE_TEXT_PROVIDER = True

    assert isinstance(get_text_provider(), FakeTextProvider)
    assert isinstance(get_image_provider(), NanoBananaImageProvider)


def test_embeddings_and_page_scanning_follow_the_text_switch(ports: Any) -> None:
    """They share the text gateway, so they share its switch."""
    assert isinstance(get_embedding_provider(), OpenAIEmbeddingProvider)
    assert isinstance(get_brand_inference(), LLMBrandInference)

    ports.USE_FAKE_TEXT_PROVIDER = True

    assert get_embedding_provider() is _fake_embedding_provider
    assert isinstance(get_brand_inference(), FakeBrandInference)


def test_a_real_video_switch_with_no_vendor_is_still_none(ports: Any) -> None:
    """Turning the video fake off never invents a vendor: no key, no provider."""
    assert get_video_provider() is None
    assert get_video_composer() is None


def test_the_master_switch_forces_every_port_onto_its_fake(ports: Any) -> None:
    ports.USE_FAKE_AI_PROVIDERS = True

    assert isinstance(get_text_provider(), FakeTextProvider)
    assert isinstance(get_image_provider(), FakeImageProvider)
    assert get_video_provider() is _fake_video_provider
    assert get_embedding_provider() is _fake_embedding_provider
    assert isinstance(get_brand_inference(), FakeBrandInference)
