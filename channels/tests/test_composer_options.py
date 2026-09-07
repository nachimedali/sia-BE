"""Composer metadata reaching the provider (P1-11).

The last leg of the path. `content/tests/test_composer_metadata.py` proves an
option reaches the rendered payload; this proves the payload reaches Zernio,
because an option that stops at our own database is decoration with extra
steps.

**The mapping is data, and it lives in the adapter, not in `rules.py`.**
`rules.py` says what an option *is* — a platform fact, true whoever publishes
it. What the vendor calls it is a vendor fact, and putting it next to the
platform fact is how a vendor swap turns into an excavation (D3).

Like the rest of the Zernio surface, the field names here are the documented
contract at OpenAPI v1.0.4 and not observed behaviour (U-5). That is what makes
a data mapping the right shape: a wrong guess is one row to change.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from django.test import override_settings

from channels.adapters.fake import FakePlatformAdapter
from channels.adapters.zernio import PROVIDER_OPTION_FIELDS, ZernioAdapter
from content.models import Platform
from tests.httpx_transport import patch_httpx_transport


@pytest.fixture
def captured(monkeypatch: Any) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"post": {"_id": "post-1", "platforms": []}})

    patch_httpx_transport(monkeypatch, handler)
    return requests


@override_settings(ZERNIO_API_KEY="k", ZERNIO_BASE_URL="https://zernio.test/api")
def test_options_are_sent_alongside_the_content(captured: list[httpx.Request]) -> None:
    ZernioAdapter().publish(
        platform=Platform.INSTAGRAM,
        provider_account_id="acct-1",
        payload={
            "body": "Hello",
            "media": [],
            "options": {"first_comment": "Swipe →", "location_id": "12345"},
        },
        idempotency_key="key-1",
    )

    body = json.loads(captured[-1].content)
    entry = body["platforms"][0]
    assert entry["firstComment"] == "Swipe →"
    assert entry["locationId"] == "12345"


@override_settings(ZERNIO_API_KEY="k", ZERNIO_BASE_URL="https://zernio.test/api")
def test_alt_text_travels_with_the_media(captured: list[httpx.Request]) -> None:
    ZernioAdapter().publish(
        platform=Platform.INSTAGRAM,
        provider_account_id="acct-1",
        payload={
            "body": "Hello",
            "media": [{"id": 1, "kind": "IMAGE", "url": "/m/1.png", "alt": "A blue ceramic mug"}],
            "options": {},
        },
        idempotency_key="key-2",
    )

    body = json.loads(captured[-1].content)
    assert body["mediaItems"][0]["altText"] == "A blue ceramic mug"


@override_settings(ZERNIO_API_KEY="k", ZERNIO_BASE_URL="https://zernio.test/api")
def test_an_unmapped_option_is_dropped_rather_than_sent_raw(
    captured: list[httpx.Request],
) -> None:
    """A key the vendor does not know is a rejected post. Dropping it loses a
    setting; sending it loses the whole publish."""
    ZernioAdapter().publish(
        platform=Platform.INSTAGRAM,
        provider_account_id="acct-1",
        payload={"body": "Hello", "media": [], "options": {"share_to_feed": True}},
        idempotency_key="key-3",
    )

    entry = json.loads(captured[-1].content)["platforms"][0]
    assert "share_to_feed" not in entry


@override_settings(ZERNIO_API_KEY="k", ZERNIO_BASE_URL="https://zernio.test/api")
def test_a_payload_without_options_still_publishes(captured: list[httpx.Request]) -> None:
    """Every stored payload written before this task exists has no `options`
    key at all."""
    ZernioAdapter().publish(
        platform=Platform.THREADS,
        provider_account_id="acct-1",
        payload={"body": "Hello", "media": []},
        idempotency_key="key-4",
    )
    assert json.loads(captured[-1].content)["platforms"][0]["platform"] == Platform.THREADS


def test_the_fake_records_what_the_real_adapter_would_send() -> None:
    """A fake that drops options would let every options test pass while the
    real path sent nothing (A8)."""
    fake = FakePlatformAdapter()
    fake.publish(
        platform=Platform.INSTAGRAM,
        provider_account_id="acct-1",
        payload={"body": "Hello", "media": [], "options": {"first_comment": "Swipe →"}},
        idempotency_key="key-5",
    )
    assert fake.published[-1]["payload"]["options"]["first_comment"] == "Swipe →"


def test_every_mapped_key_is_a_declared_option() -> None:
    """The mapping and the declaration drift apart silently otherwise — a
    renamed option leaves a mapping row pointing at nothing, and nothing
    fails."""
    from content.services.rules import options_for

    for platform, mapping in PROVIDER_OPTION_FIELDS.items():
        declared = options_for(platform)
        unknown = sorted(set(mapping) - set(declared))
        assert unknown == [], f"{platform} maps options that are not declared: {unknown}"


@override_settings(ZERNIO_API_KEY="k", ZERNIO_BASE_URL="https://zernio.test/api")
def test_a_custom_thumbnail_is_sent_as_a_url(captured: list[httpx.Request]) -> None:
    """C-11's rule applied to a composer field: a setting that is stored and
    never sent implies coverage that does not exist."""
    ZernioAdapter().publish(
        platform=Platform.YOUTUBE,
        provider_account_id="acct-1",
        payload={
            "body": "Hello",
            "media": [],
            "options": {"title": "A video", "thumbnail_media_id": "/m/thumb.png"},
        },
        idempotency_key="key-6",
    )

    entry = json.loads(captured[-1].content)["platforms"][0]
    assert entry["thumbnailUrl"].endswith("/m/thumb.png")
