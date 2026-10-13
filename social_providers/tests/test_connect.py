"""SocialAPI's connect port (SA-20, SA-21), driven through `httpx.MockTransport`.

Each test states what the vendor answered and asserts what we sent and kept:
the brand found before any create (a create is not idempotent and costs a
unit), the `state` and `brand_id` on the connect call, the three return shapes,
Facebook's single-use selection, and disconnect treating "already gone" as done.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from channels.adapters.base import PlatformError
from social_providers.registry import ProviderNotReadyError
from social_providers.socialapi.budget import TeamBudget
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.connect import SocialAPIConnectAdapter, brand_name

pytestmark = pytest.mark.django_db

KEY = "sapi_key_" + "b" * 62
Route = Callable[[httpx.Request], httpx.Response]


def vendor(
    routes: dict[tuple[str, str], Any],
) -> tuple[SocialAPIConnectAdapter, list[httpx.Request]]:
    """An adapter whose vendor answers `routes[(METHOD, path)]` — a JSON body
    (200), an `httpx.Response`, or a callable taking the request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/v1")
        answer = routes.get((request.method, path))
        if answer is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "no route"}})
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    client = SocialAPIClient(
        api_key=KEY,
        base_url="https://api.social-api.ai/v1",
        transport=httpx.MockTransport(handler),
        budget=TeamBudget(),
        sleep=lambda _s: None,
        sample=lambda: 1.0,
    )
    return SocialAPIConnectAdapter(client), seen


def body(request: httpx.Request) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(request.content or b"{}")
    return parsed


def not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})


@pytest.fixture(autouse=True)
def _clean_budget(budget: TeamBudget) -> None:
    """Every test starts from an empty team bucket (conftest `budget`)."""


# -----------------------------------------------------------------------------
# The brand — one per workspace, never created twice
# -----------------------------------------------------------------------------
def test_an_existing_brand_id_still_at_the_vendor_is_kept() -> None:
    adapter, seen = vendor({("GET", "/brands"): {"data": [{"id": "b-1", "name": "cv:acme"}]}})

    assert adapter.ensure_profile(name="acme", existing_profile_id="b-1") == "b-1"
    assert [r.method for r in seen] == ["GET"], "nothing may be created or renamed"


def test_a_brand_left_by_a_crashed_create_is_found_by_name() -> None:
    adapter, seen = vendor(
        {("GET", "/brands"): {"data": [{"id": "b-9", "name": brand_name("acme")}]}}
    )

    assert adapter.ensure_profile(name="acme") == "b-9"
    assert all(r.method == "GET" for r in seen)


def test_a_brand_deleted_in_the_dashboard_is_noticed_and_replaced() -> None:
    adapter, seen = vendor(
        {
            ("GET", "/brands"): {
                "data": [{"id": "b-other", "name": "cv:else", "accounts_count": 1}]
            },
            ("POST", "/brands"): {"id": "b-new"},
        }
    )

    assert adapter.ensure_profile(name="acme", existing_profile_id="b-gone") == "b-new"
    assert body(seen[-1]) == {"name": "cv:acme"}


def test_the_empty_default_brand_is_adopted_instead_of_spending_a_second_unit() -> None:
    adapter, seen = vendor(
        {
            ("GET", "/brands"): {"data": [{"id": "b-def", "name": "Default", "accounts_count": 0}]},
            ("PATCH", "/brands/b-def"): {"id": "b-def", "name": "cv:acme"},
        }
    )

    assert adapter.ensure_profile(name="acme") == "b-def"
    assert [(r.method, r.url.path) for r in seen][-1] == ("PATCH", "/v1/brands/b-def")
    assert not any(r.method == "POST" for r in seen)


def test_a_default_brand_already_holding_accounts_is_left_alone() -> None:
    adapter, seen = vendor(
        {
            ("GET", "/brands"): {"data": [{"id": "b-def", "name": "Default", "accounts_count": 2}]},
            ("POST", "/brands"): {"id": "b-new"},
        }
    )

    assert adapter.ensure_profile(name="acme") == "b-new"
    assert not any(r.method == "PATCH" for r in seen)


def test_a_create_answering_no_id_is_an_error_not_an_empty_brand() -> None:
    adapter, _ = vendor({("GET", "/brands"): {"data": []}, ("POST", "/brands"): {}})

    with pytest.raises(PlatformError):
        adapter.ensure_profile(name="acme")


def test_a_brand_limit_from_the_vendor_surfaces_as_a_platform_error() -> None:
    adapter, _ = vendor(
        {
            ("GET", "/brands"): {"data": []},
            ("POST", "/brands"): httpx.Response(
                403, json={"error": {"code": "plan.brand_limit", "message": "limit"}}
            ),
        }
    )

    with pytest.raises(PlatformError):
        adapter.ensure_profile(name="acme")


# -----------------------------------------------------------------------------
# The link
# -----------------------------------------------------------------------------
def test_the_connect_call_carries_slug_redirect_state_and_brand() -> None:
    adapter, seen = vendor(
        {("POST", "/accounts/connect"): {"auth_url": "https://www.facebook.com/dialog/oauth?x=1"}}
    )

    url = adapter.connect_url(
        platform="google_business",
        profile_id="b-1",
        redirect_url="https://app.test/app/settings/connections",
        state="signed-token",
    )

    assert url.startswith("https://www.facebook.com/")
    assert body(seen[0]) == {
        "platform": "google",
        "redirect_uri": "https://app.test/app/settings/connections",
        "state": "signed-token",
        "brand_id": "b-1",
    }


def test_x_is_refused_with_a_sentence_before_any_call() -> None:
    adapter, seen = vendor({})

    with pytest.raises(PlatformError) as caught:
        adapter.connect_url(platform="x", profile_id="b-1", redirect_url="https://a.test/")

    assert caught.value.code == "platform_not_connectable"
    assert "developer app" in str(caught.value.detail)
    assert seen == []


def test_a_non_https_auth_url_is_never_handed_to_the_browser() -> None:
    adapter, _ = vendor({("POST", "/accounts/connect"): {"auth_url": "javascript:alert(1)"}})

    with pytest.raises(PlatformError):
        adapter.connect_url(platform="instagram", profile_id="b-1", redirect_url="https://a.test/")


# -----------------------------------------------------------------------------
# The return
# -----------------------------------------------------------------------------
ACCOUNTS = {
    "data": [
        {
            "id": "acc-1",
            "platform": "instagram",
            "username": "acme.studio",
            "name": "Acme Studio",
            "metadata": {"follower_count": 812},
        }
    ]
}


def test_success_reads_the_account_from_the_brands_list() -> None:
    adapter, seen = vendor({("GET", "/accounts"): ACCOUNTS})

    resolution = adapter.resolve_callback(
        platform="instagram", profile_id="b-1", params={"status": "success", "account_id": "acc-1"}
    )

    assert resolution.account is not None
    assert resolution.account.provider_account_id == "acc-1"
    assert resolution.account.handle == "@acme.studio"
    assert resolution.account.display_name == "Acme Studio"
    assert resolution.account.followers == 812
    assert seen[0].url.params["brand_id"] == "b-1"


def test_a_cancelled_sign_in_says_so() -> None:
    adapter, _ = vendor({})

    with pytest.raises(PlatformError) as caught:
        adapter.resolve_callback(
            platform="instagram",
            profile_id="b-1",
            params={"status": "error", "error": "access_denied"},
        )
    assert caught.value.code == "connect_denied"


def test_a_platform_refusal_carries_its_reason() -> None:
    adapter, _ = vendor({})

    with pytest.raises(PlatformError) as caught:
        adapter.resolve_callback(
            platform="instagram",
            profile_id="b-1",
            params={"status": "error", "error": "server_error", "error_description": "busy"},
        )
    assert caught.value.code == "connect_failed"
    assert "busy" in str(caught.value.detail)


def test_an_unknown_status_is_a_failure_not_a_success() -> None:
    adapter, _ = vendor({})

    with pytest.raises(PlatformError) as caught:
        adapter.resolve_callback(platform="instagram", profile_id="b-1", params={})
    assert caught.value.code == "connect_failed"


PENDING = {
    "pages": [
        {"platform_page_id": "p-1", "name": "Acme Paris", "assignable": True},
        {
            "platform_page_id": "p-2",
            "name": "Acme Lyon",
            "assignable": False,
            "assigned_brand_name": "cv:other",
        },
    ]
}


def test_selection_required_offers_the_pages_and_marks_the_taken_one() -> None:
    adapter, _ = vendor({("GET", "/accounts/pending/c-1"): PENDING})

    resolution = adapter.resolve_callback(
        platform="facebook",
        profile_id="b-1",
        params={"status": "selection_required", "connection_id": "c-1", "state": "s"},
    )

    assert resolution.needs_selection
    assert [t.id for t in resolution.targets] == ["p-1", "p-2"]
    assert "already used by cv:other" in resolution.targets[1].name
    assert resolution.params["connection_id"] == "c-1"


def test_an_expired_selection_says_start_again() -> None:
    adapter, _ = vendor({("GET", "/accounts/pending/c-1"): not_found()})

    with pytest.raises(PlatformError) as caught:
        adapter.resolve_callback(
            platform="facebook",
            profile_id="b-1",
            params={"status": "selection_required", "connection_id": "c-1"},
        )
    assert caught.value.code == "connect_expired"


def test_a_login_managing_nothing_connectable_says_so() -> None:
    adapter, _ = vendor({("GET", "/accounts/pending/c-1"): {"pages": [], "profiles": []}})

    with pytest.raises(PlatformError) as caught:
        adapter.resolve_callback(
            platform="facebook",
            profile_id="b-1",
            params={"status": "selection_required", "connection_id": "c-1"},
        )
    assert caught.value.code == "connect_no_targets"


def _offered(adapter: SocialAPIConnectAdapter, platform: str = "facebook") -> dict[str, Any]:
    resolution = adapter.resolve_callback(
        platform=platform,
        profile_id="b-1",
        params={"status": "selection_required", "connection_id": "c-1"},
    )
    return dict(resolution.params)


def test_choosing_a_page_sends_page_ids_and_attaches_the_account() -> None:
    adapter, seen = vendor(
        {
            ("GET", "/accounts/pending/c-1"): PENDING,
            ("POST", "/accounts/pending/c-1/select"): {"account_id": "acc-fb"},
            ("GET", "/accounts"): {"data": [{"id": "acc-fb", "page_name": "Acme Paris"}]},
        }
    )

    account = adapter.select_target(
        platform="facebook", profile_id="b-1", params=_offered(adapter), target_id="p-1"
    )

    select = next(r for r in seen if r.url.path.endswith("/select"))
    assert body(select) == {"page_ids": ["p-1"]}
    assert account.provider_account_id == "acc-fb"
    assert account.display_name == "Acme Paris"


def test_a_google_profile_is_chosen_by_platform_account_id() -> None:
    adapter, seen = vendor(
        {
            ("GET", "/accounts/pending/c-1"): {
                "profiles": [{"platform_account_id": "loc-7", "display_name": "Acme Shop"}]
            },
            ("POST", "/accounts/pending/c-1/select"): {"account_id": "acc-g"},
            ("GET", "/accounts"): {"data": []},
        }
    )

    adapter.select_target(
        platform="google_business",
        profile_id="b-1",
        params=_offered(adapter, "google_business"),
        target_id="loc-7",
    )

    select = next(r for r in seen if r.url.path.endswith("/select"))
    assert body(select) == {"platform_account_id": "loc-7"}


def test_a_page_taken_by_another_brand_is_refused_before_any_call() -> None:
    adapter, seen = vendor({("GET", "/accounts/pending/c-1"): PENDING})
    params = _offered(adapter)
    calls = len(seen)

    with pytest.raises(PlatformError) as caught:
        adapter.select_target(platform="facebook", profile_id="b-1", params=params, target_id="p-2")

    assert caught.value.code == "target_unavailable"
    assert len(seen) == calls


def test_a_target_that_was_never_offered_is_refused() -> None:
    adapter, _ = vendor({("GET", "/accounts/pending/c-1"): PENDING})

    with pytest.raises(PlatformError):
        adapter.select_target(
            platform="facebook", profile_id="b-1", params=_offered(adapter), target_id="p-forged"
        )


def test_selecting_after_the_window_closed_says_start_again() -> None:
    adapter, _ = vendor(
        {
            ("GET", "/accounts/pending/c-1"): PENDING,
            ("POST", "/accounts/pending/c-1/select"): not_found(),
        }
    )

    with pytest.raises(PlatformError) as caught:
        adapter.select_target(
            platform="facebook", profile_id="b-1", params=_offered(adapter), target_id="p-1"
        )
    assert caught.value.code == "connect_expired"


def test_the_vendors_page_cap_is_named() -> None:
    adapter, _ = vendor(
        {
            ("GET", "/accounts/pending/c-1"): PENDING,
            ("POST", "/accounts/pending/c-1/select"): httpx.Response(
                403, json={"error": {"code": "platform.facebook.page_cap", "message": "cap"}}
            ),
        }
    )

    with pytest.raises(PlatformError) as caught:
        adapter.select_target(
            platform="facebook", profile_id="b-1", params=_offered(adapter), target_id="p-1"
        )
    assert caught.value.code == "page_cap"


# -----------------------------------------------------------------------------
# After
# -----------------------------------------------------------------------------
def test_disconnect_deletes_at_the_vendor() -> None:
    adapter, seen = vendor({("DELETE", "/accounts/acc-1"): httpx.Response(204)})

    adapter.disconnect(provider_account_id="acc-1")

    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/v1/accounts/acc-1")]


def test_disconnecting_an_account_already_gone_counts_as_done() -> None:
    adapter, _ = vendor({("DELETE", "/accounts/acc-1"): not_found()})

    adapter.disconnect(provider_account_id="acc-1")


def test_pinterest_boards_are_listed_for_the_composer() -> None:
    adapter, _ = vendor(
        {("GET", "/accounts/acc-p/boards"): {"data": [{"board_id": "bd-1", "name": "Spring"}]}}
    )

    assert adapter.list_remote_options(
        platform="pinterest", provider_account_id="acc-p", source="pinterest_boards"
    ) == [{"id": "bd-1", "name": "Spring"}]
    assert (
        adapter.list_remote_options(
            platform="instagram", provider_account_id="acc-p", source="pinterest_boards"
        )
        == []
    )


def test_publishing_is_refused_until_its_port_is_built() -> None:
    adapter, seen = vendor({})

    with pytest.raises(ProviderNotReadyError):
        adapter.publish(
            platform="instagram", provider_account_id="a", payload={}, idempotency_key="k"
        )
    assert seen == []


def test_no_code_path_deletes_a_brand() -> None:
    """A brand delete cascades to every account in it at the vendor. Nothing
    in the app may issue one — not a cleanup, not a reset, not a test helper."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"\.delete\(\s*f?[\"']/brands")
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if ".venv" not in path.parts
        and "tests" not in path.parts
        and pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]

    assert offenders == []
