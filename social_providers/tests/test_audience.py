"""SA-60/61/62 at the provider: reading, answering and moderating comments.

Recorded vendor answers (`docs.social-api.ai`, inbox guide): newest-first pages
with a cursor and no `since` (G11), per-comment capability flags, no reaction
breakdown (G5), platform post ids in the path.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from analytics.providers.base import MetricsError, MetricsUnsupportedError
from social_providers.socialapi.audience import SocialAPIAudienceProvider
from social_providers.tests.stub import ReadVendor, error

pytestmark = pytest.mark.django_db

POST = "17895695668004550"
ACCOUNT = "acc_1"
ROUTE = f"GET /inbox/comments/{POST}"


def comment(n: int, *, hours_ago: float, **flags: bool) -> dict[str, Any]:
    at = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC) - dt.timedelta(hours=hours_ago)
    return {
        "id": f"sapi_cmt_{n}",
        "platform": "instagram",
        "text": f"comment {n}",
        "author_name": f"Person {n}",
        "created_at": at.isoformat(),
        "capabilities": {
            "can_reply": True,
            "can_hide": True,
            "can_like": False,
            "can_delete": True,
            "can_private_reply": True,
            **flags,
        },
    }


def page(*items: dict[str, Any], cursor: str = "") -> dict[str, Any]:
    return {"data": list(items), "pagination": {"has_more": bool(cursor), "next_cursor": cursor}}


@pytest.fixture
def vendor() -> ReadVendor:
    return ReadVendor()


def provider(vendor: ReadVendor) -> SocialAPIAudienceProvider:
    return SocialAPIAudienceProvider(vendor.client())


def read(vendor: ReadVendor, *, since: dt.datetime | None = None) -> list[Any]:
    return provider(vendor).fetch_comments(
        platform="instagram",
        provider_post_id="post-uuid",
        since=since,
        platform_post_id=POST,
        provider_account_id=ACCOUNT,
    )


# -----------------------------------------------------------------------------
# Reading (SA-60, SA-61)
# -----------------------------------------------------------------------------
def test_comments_carry_their_flags_and_no_reaction_breakdown(vendor: ReadVendor) -> None:
    vendor.routes[ROUTE] = page(comment(1, hours_ago=1, can_hide=False))

    [only] = read(vendor)

    assert (only.external_id, only.author, only.body) == ("sapi_cmt_1", "Person 1", "comment 1")
    assert only.capabilities == {
        "can_reply": True,
        "can_hide": False,
        "can_like": False,
        "can_delete": True,
        "can_private_reply": True,
    }
    assert only.reactions is None, "not shared — never {} and never zeros"
    assert vendor.calls[0][2] == {"account_id": ACCOUNT, "limit": "50"}


def test_paging_stops_at_the_first_page_already_held(vendor: ReadVendor) -> None:
    pages = iter(
        [
            page(comment(3, hours_ago=1), comment(2, hours_ago=2), cursor="c2"),
            page(comment(1, hours_ago=30), cursor="c3"),
            page(comment(0, hours_ago=40)),
        ]
    )
    vendor.routes[ROUTE] = lambda _request: next(pages)
    held_up_to = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC) - dt.timedelta(hours=10)

    found = read(vendor, since=held_up_to)

    assert [c.external_id for c in found] == ["sapi_cmt_3", "sapi_cmt_2"]
    assert len(vendor.calls) == 2, "the third page is never asked for"
    assert vendor.calls[1][2]["cursor"] == "c2"


def test_a_busy_backlog_is_read_a_few_pages_at_a_time(vendor: ReadVendor) -> None:
    counter = iter(range(1000))
    vendor.routes[ROUTE] = lambda _request: page(comment(next(counter), hours_ago=1), cursor="more")

    read(vendor)

    assert len(vendor.calls) == 5


def test_a_platform_without_comments_is_unsupported_not_empty(vendor: ReadVendor) -> None:
    with pytest.raises(MetricsUnsupportedError):
        provider(vendor).fetch_comments(platform="tiktok", provider_post_id="p")
    assert vendor.calls == []


def test_a_post_without_its_platform_id_cannot_be_read_yet(vendor: ReadVendor) -> None:
    with pytest.raises(MetricsError):
        provider(vendor).fetch_comments(platform="instagram", provider_post_id="p")
    assert vendor.calls == []


def test_the_vendor_shares_no_reaction_breakdown() -> None:
    assert (
        SocialAPIAudienceProvider().fetch_reactions(platform="facebook", provider_post_id="p")
        is None
    )


# -----------------------------------------------------------------------------
# Acting (SA-62)
# -----------------------------------------------------------------------------
def test_a_reply_is_threaded_under_the_comment(vendor: ReadVendor) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}"] = {"comment_id": "sapi_cmt_99", "success": True}

    reply_id = provider(vendor).reply(
        platform="instagram",
        comment_external_id="sapi_cmt_1",
        body="Thank you!",
        platform_post_id=POST,
        provider_account_id=ACCOUNT,
    )

    assert reply_id == "sapi_cmt_99"
    assert vendor.bodies == [
        {"account_id": ACCOUNT, "comment_id": "sapi_cmt_1", "text": "Thank you!"}
    ]


@pytest.mark.parametrize(
    ("action", "method", "suffix"),
    [
        ("hide", "POST", "/hide"),
        ("unhide", "DELETE", "/hide"),
        ("like", "POST", "/like"),
        ("unlike", "DELETE", "/like"),
        ("delete", "DELETE", ""),
    ],
)
def test_each_moderation_action_reaches_its_route(
    vendor: ReadVendor, action: str, method: str, suffix: str
) -> None:
    route = f"{method} /inbox/comments/{POST}/sapi_cmt_1{suffix}"
    vendor.routes[route] = {"success": True}

    provider(vendor).moderate(
        action=action,
        comment_external_id="sapi_cmt_1",
        platform_post_id=POST,
        provider_account_id=ACCOUNT,
    )

    method_called, path, params = vendor.calls[0]
    assert f"{method_called} {path}" == route
    account = vendor.bodies[0]["account_id"] if method == "POST" else params["account_id"]
    assert account == ACCOUNT


def test_deleting_a_comment_already_gone_is_done(vendor: ReadVendor) -> None:
    vendor.routes[f"DELETE /inbox/comments/{POST}/sapi_cmt_1"] = error(404, "resource.not_found")

    provider(vendor).moderate(
        action="delete",
        comment_external_id="sapi_cmt_1",
        platform_post_id=POST,
        provider_account_id=ACCOUNT,
    )


def test_an_action_the_platform_refuses_is_reported(vendor: ReadVendor) -> None:
    vendor.routes[f"POST /inbox/comments/{POST}/sapi_cmt_1/like"] = error(
        501, "resource.not_supported", "Likes are not supported here."
    )

    with pytest.raises(MetricsUnsupportedError):
        provider(vendor).moderate(
            action="like",
            comment_external_id="sapi_cmt_1",
            platform_post_id=POST,
            provider_account_id=ACCOUNT,
        )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"interactions_used": 12, "interactions_limit": 50}, (12, 50)),
        ({"interactions_used": 12, "interactions_limit": -1}, None),
        ({}, None),
    ],
)
def test_the_vendors_interaction_allowance(
    vendor: ReadVendor, usage: dict[str, Any], expected: Any
) -> None:
    vendor.routes["GET /usage"] = usage

    assert provider(vendor).interaction_allowance() == expected
