"""TN-10: the Konnect adapter against the documented request and response shapes.

Recorded from the vendor's documentation (`docs.konnect.network`, 2026-11-01),
not from a live call — TN-90 is what observes it. Only the transport is
stubbed, so the adapter's own URL, header and body assembly run for real.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from billing.gateways.base import BillingGatewayError
from billing.gateways.konnect import KonnectGateway
from tests.httpx_transport import patch_httpx_transport

PAYMENT = {
    "payment": {
        "id": "87a4c597cb3c15311bdbd547",
        "status": "completed",
        "amountDue": 0,
        "reachedAmount": 175930,
        "amount": 175930,
        "token": "TND",
        "orderId": "42",
        "type": "immediate",
        "receiverWallet": {"_id": "wallet-1"},
        "transactions": [{"status": "success"}],
    }
}


@pytest.fixture(autouse=True)
def _keys(settings: Any) -> None:
    settings.KONNECT_API_KEY = "key-1"
    settings.KONNECT_WALLET_ID = "wallet-1"
    settings.KONNECT_BASE_URL = "https://api.sandbox.konnect.network/api/v2"


def test_init_payment_sends_millimes_in_tnd_with_the_fee_absorbed(monkeypatch: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"payUrl": "https://pay.konnect/x", "paymentRef": "ref-1"})

    patch_httpx_transport(monkeypatch, handler)

    hosted = KonnectGateway().create_payment(
        order_id="42",
        amount_minor=175930,
        currency="TND",
        description="ComposeVision Essential, 1 mois",
        email="a@b.tn",
        webhook_url="https://api.test/api/v1/billing/webhook/konnect/",
    )

    assert (hosted.ref, hosted.url) == ("ref-1", "https://pay.konnect/x")
    request = seen[0]
    assert str(request.url) == "https://api.sandbox.konnect.network/api/v2/payments/init-payment"
    assert request.headers["x-api-key"] == "key-1"
    body = json.loads(request.content)
    assert body["receiverWalletId"] == "wallet-1"
    assert (body["token"], body["amount"], body["orderId"]) == ("TND", 175930, "42")
    assert body["addPaymentFeesToAmount"] is False
    assert body["webhook"].endswith("/billing/webhook/konnect/")


def test_only_tnd_is_ever_sent() -> None:
    with pytest.raises(BillingGatewayError):
        KonnectGateway().create_payment(
            order_id="1", amount_minor=1, currency="USD", description="", email="", webhook_url=""
        )


def test_payment_details_are_read_back(monkeypatch: Any) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=PAYMENT)

    patch_httpx_transport(monkeypatch, handler)

    status = KonnectGateway().fetch_payment("ref/1")

    assert seen == ["https://api.sandbox.konnect.network/api/v2/payments/ref%2F1"]
    assert (status.state, status.amount_minor, status.currency) == ("completed", 175930, "TND")
    assert (status.order_id, status.wallet_id) == ("42", "wallet-1")


@pytest.mark.parametrize(("raw", "state"), [("pending", "pending"), ("expired", "failed")])
def test_anything_but_completed_is_not_paid(monkeypatch: Any, raw: str, state: str) -> None:
    body = {"payment": {**PAYMENT["payment"], "status": raw}}
    patch_httpx_transport(monkeypatch, lambda request: httpx.Response(200, json=body))

    assert KonnectGateway().fetch_payment("ref-1").state == state


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"errors": [{"code": "unauthorized"}]}),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"nothing": True}),
    ],
)
def test_a_refusal_or_an_unreadable_answer_is_a_gateway_error(
    monkeypatch: Any, response: Any
) -> None:
    patch_httpx_transport(monkeypatch, lambda request: response)

    with pytest.raises(BillingGatewayError):
        KonnectGateway().fetch_payment("ref-1")


def test_no_key_is_refused_before_the_wire(settings: Any) -> None:
    settings.KONNECT_API_KEY = ""

    with pytest.raises(BillingGatewayError):
        KonnectGateway().fetch_payment("ref-1")
