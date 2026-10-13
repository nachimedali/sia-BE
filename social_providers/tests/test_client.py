"""The SocialAPI client (SA-10): every call takes the same six steps.

Driven through `httpx.MockTransport`, so each test states exactly what the
vendor answered and asserts what we did with it: refused before the wire,
retried or not, which error, what the budget learned, what the log kept — and
that the key never lands anywhere.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from social_providers.models import CallKind, ProviderCall
from social_providers.socialapi.budget import Priority, TeamBudget
from social_providers.socialapi.client import SocialAPIClient, normalise_path
from social_providers.socialapi.errors import SocialAPIError

pytestmark = pytest.mark.django_db

KEY = "sapi_key_" + "a" * 62


def envelope(code: str, message: str = "nope", request_id: str = "req-1") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}, "request_id": request_id}


def answers(
    *responses: httpx.Response | Exception,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), seen


def client(
    transport: httpx.MockTransport,
    budget: TeamBudget,
    *,
    sample: Callable[[], float] = lambda: 0.0,
    key: str = KEY,
) -> tuple[SocialAPIClient, list[float]]:
    slept: list[float] = []
    return (
        SocialAPIClient(
            api_key=key,
            base_url="https://api.social-api.ai/v1",
            transport=transport,
            budget=budget,
            sleep=slept.append,
            sample=sample,
        ),
        slept,
    )


RATE = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59", "X-RateLimit-Reset": "40"}


# --- happy path --------------------------------------------------------------------------
def test_a_read_sends_the_key_and_returns_the_body(budget: TeamBudget) -> None:
    transport, seen = answers(
        httpx.Response(200, json={"posts_used": 0}, headers={**RATE, "X-Request-ID": "r-42"})
    )
    api, _ = client(transport, budget)

    result = api.get("/usage", params={"x": "1"})

    assert result.data == {"posts_used": 0}
    assert result.request_id == "r-42"
    assert (result.rate.limit, result.rate.remaining) == (60, 59)
    request = seen[0]
    assert request.url == "https://api.social-api.ai/v1/usage?x=1"
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_a_204_is_success_with_no_body(budget: TeamBudget) -> None:
    transport, _ = answers(httpx.Response(204))
    api, _ = client(transport, budget)
    assert api.delete("/accounts/acc_1").data is None


# --- refused before the wire -------------------------------------------------------------
def test_no_key_never_reaches_the_vendor(budget: TeamBudget) -> None:
    transport, seen = answers(httpx.Response(200, json={}))
    api, _ = client(transport, budget, key="")

    with pytest.raises(SocialAPIError) as caught:
        api.get("/usage")

    assert caught.value.kind == CallKind.MISCONFIGURED and caught.value.local
    assert seen == []
    assert ProviderCall.objects.get().local is True


def test_a_spent_budget_never_reaches_the_vendor(budget: TeamBudget, settings: Any) -> None:
    settings.SOCIALAPI_REQUESTS_PER_MINUTE = 2
    settings.SOCIALAPI_PUBLISH_RESERVE_RATIO = 0.5
    transport, seen = answers(httpx.Response(200, json={}))
    api, _ = client(transport, budget)

    api.get("/usage")
    with pytest.raises(SocialAPIError) as caught:
        api.get("/usage")  # background: the second token is the publish reserve

    assert caught.value.kind == CallKind.RATE_LIMITED
    assert caught.value.local and caught.value.retry_after
    assert len(seen) == 1
    api.post("/posts", json={}, priority=Priority.PUBLISH)  # publishing may spend it
    assert len(seen) == 2


# --- errors -------------------------------------------------------------------------------
def test_an_error_carries_its_kind_code_and_request_id(budget: TeamBudget) -> None:
    transport, _ = answers(httpx.Response(403, json=envelope("account.reconnection_required")))
    api, _ = client(transport, budget)

    with pytest.raises(SocialAPIError) as caught:
        api.get("/accounts/acc_1")

    error = caught.value
    assert (error.kind, error.vendor_code, error.status) == (
        CallKind.REAUTH,
        "account.reconnection_required",
        403,
    )
    assert error.request_id == "req-1"  # from the body when the header is absent
    row = ProviderCall.objects.get()
    assert (row.kind, row.error_code, row.request_id) == (
        CallKind.REAUTH,
        "account.reconnection_required",
        "req-1",
    )


def test_the_two_429s_are_told_apart(budget: TeamBudget) -> None:
    limited, _ = answers(
        httpx.Response(429, json=envelope("ratelimit.exceeded"), headers={"Retry-After": "34"})
    )
    api, _ = client(limited, budget)
    with pytest.raises(SocialAPIError) as slow:
        api.post("/posts", json={}, priority=Priority.PUBLISH)
    assert slow.value.retryable and slow.value.retry_after == 34

    budget.reset()
    exhausted, _ = answers(httpx.Response(429, json=envelope("validation.post_limit_exhausted")))
    api, _ = client(exhausted, budget)
    with pytest.raises(SocialAPIError) as month:
        api.post("/posts", json={}, priority=Priority.PUBLISH)
    assert month.value.kind == CallKind.QUOTA_EXHAUSTED and not month.value.retryable


def test_a_429_holds_the_whole_team(budget: TeamBudget) -> None:
    transport, seen = answers(
        httpx.Response(429, json=envelope("ratelimit.exceeded"), headers={"Retry-After": "30"})
    )
    api, _ = client(transport, budget)
    with pytest.raises(SocialAPIError):
        api.get("/usage")
    with pytest.raises(SocialAPIError) as held:
        api.post("/posts", json={}, priority=Priority.PUBLISH)
    assert held.value.local
    assert len(seen) == 1  # the second call never left


def test_a_2xx_that_is_not_json_is_not_success(budget: TeamBudget) -> None:
    transport, _ = answers(httpx.Response(200, text="<html>maintenance</html>"))
    api, _ = client(transport, budget)
    with pytest.raises(SocialAPIError) as caught:
        api.get("/usage")
    assert caught.value.kind == CallKind.BAD_RESPONSE


# --- retries ------------------------------------------------------------------------------
def test_a_read_is_retried_twice_on_an_outage_then_succeeds(budget: TeamBudget) -> None:
    transport, seen = answers(
        httpx.Response(503, json=envelope("system.internal")),
        httpx.ConnectError("refused"),
        httpx.Response(200, json={"ok": True}),
    )
    api, slept = client(transport, budget)

    assert api.get("/usage").data == {"ok": True}
    assert len(seen) == 3
    assert slept == [0.5, 1.5]
    assert ProviderCall.objects.filter(kind=CallKind.OK).get().attempts == 3


def test_a_read_gives_up_after_its_retries(budget: TeamBudget) -> None:
    transport, seen = answers(httpx.Response(502))
    api, _ = client(transport, budget)
    with pytest.raises(SocialAPIError) as caught:
        api.get("/usage")
    assert caught.value.kind == CallKind.UNAVAILABLE
    assert len(seen) == 3


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
def test_a_write_is_never_retried(budget: TeamBudget, method: str) -> None:
    transport, seen = answers(httpx.Response(503, json=envelope("system.internal")))
    api, slept = client(transport, budget)
    call = getattr(api, method)
    with pytest.raises(SocialAPIError):
        call("/posts/p_1") if method == "delete" else call("/posts", json={"text": "x"})
    assert len(seen) == 1 and slept == []


def test_a_timeout_on_a_write_is_unavailable_and_not_retried(budget: TeamBudget) -> None:
    transport, seen = answers(httpx.ReadTimeout("slow"))
    api, _ = client(transport, budget)
    with pytest.raises(SocialAPIError) as caught:
        api.post("/posts/p_1/publish", priority=Priority.PUBLISH)
    assert caught.value.kind == CallKind.UNAVAILABLE and caught.value.status is None
    assert len(seen) == 1


# --- the budget learns from every answer ------------------------------------------------------
def test_the_budget_is_calibrated_from_headers(budget: TeamBudget) -> None:
    transport, _ = answers(
        httpx.Response(
            200, json={}, headers={"X-RateLimit-Limit": "600", "X-RateLimit-Remaining": "599"}
        )
    )
    api, _ = client(transport, budget)
    api.get("/usage")
    assert budget.limit() == 600


# --- the log ------------------------------------------------------------------------------
def test_writes_and_failures_are_always_logged_reads_are_sampled(budget: TeamBudget) -> None:
    transport, _ = answers(httpx.Response(200, json={}, headers=RATE))
    sampled_out, _ = client(transport, budget, sample=lambda: 0.99)
    sampled_out.get("/usage")
    assert ProviderCall.objects.count() == 0

    sampled_out.post("/posts", json={}, priority=Priority.PUBLISH)
    row = ProviderCall.objects.get()
    assert (row.method, row.path, row.priority, row.call_class) == (
        "POST",
        "/posts",
        "publish",
        "write",
    )
    assert (row.rate_limit, row.rate_remaining) == (60, 59)


def test_the_log_never_holds_an_id_a_body_or_the_key(budget: TeamBudget) -> None:
    transport, _ = answers(httpx.Response(400, json=envelope("validation.field_invalid")))
    api, _ = client(transport, budget)
    with pytest.raises(SocialAPIError):
        api.post(
            "/posts/7c9e6679-7425-40de-944b-e07fc1f90ae7/publish", json={"text": "secret caption"}
        )

    row = ProviderCall.objects.get()
    assert row.path == "/posts/{id}/publish"
    dumped = json.dumps({f.name: str(getattr(row, f.name)) for f in ProviderCall._meta.fields})
    assert KEY not in dumped and "secret caption" not in dumped


def test_a_failing_log_never_fails_the_call(budget: TeamBudget, monkeypatch: Any) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(ProviderCall.objects, "create", boom)
    transport, _ = answers(httpx.Response(201, json={"id": "p_1"}))
    api, _ = client(transport, budget)
    assert api.post("/posts", json={}, priority=Priority.PUBLISH).data == {"id": "p_1"}


@pytest.mark.parametrize(
    ("path", "shape"),
    [
        ("/posts/7c9e6679-7425-40de-944b-e07fc1f90ae7/metrics", "/posts/{id}/metrics"),
        ("/accounts/acc_01HZ9X3Q4R5M6N7P8V2K0W1J/analytics", "/accounts/{id}/analytics"),
        ("/inbox/comments/17895695668004550?account_id=acc_1", "/inbox/comments/{id}"),
        ("/accounts/pending/pc_4a8f/select", "/accounts/pending/{id}/select"),
        (
            "/platforms/instagram/accounts/acc_1/insights",
            "/platforms/instagram/accounts/{id}/insights",
        ),
        ("/usage", "/usage"),
    ],
)
def test_paths_are_logged_as_shapes(path: str, shape: str) -> None:
    assert normalise_path(path) == shape
