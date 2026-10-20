"""The inbox API (S2-01, S2-03, S2-04, S2-05): listing, filters, triage, notes,
the Advanced reply, saved replies — and the three tenancy walls."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import (
    AudienceComment,
    AudienceCommentAction,
    Availability,
    CommentAction,
)
from analytics.tests.conftest import make_target
from billing.models import FeatureFlag, ReplyLedger
from billing.services.flags import INBOX_S2
from channels.models import SocialAccount
from conftest import PASSWORD
from inbox.models import CommentCoding, CommentNote, CommentTriage, SavedReply
from inbox.services import coding
from inbox.tests.conftest import make_comment
from workspaces.models import Membership, Role
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

LIST = "/api/v1/inbox/comments/"
SUMMARY = "/api/v1/inbox/summary/"
SAVED = "/api/v1/inbox/saved-replies/"


def detail(comment: AudienceComment) -> str:
    return f"{LIST}{comment.pk}/"


def ids(response: Any) -> list[int]:
    return [row["id"] for row in response.json()["results"]]


# --- listing and filters ------------------------------------------------------------------
def test_the_inbox_lists_every_captured_comment_newest_first(auth_client: Any, target: Any) -> None:
    older = make_comment(target, "first", external_id="c-1")
    older.posted_at = timezone.now() - dt.timedelta(hours=3)
    AudienceComment.objects.filter(pk=older.pk).update(posted_at=older.posted_at)
    newer = make_comment(target, "second", external_id="c-2")

    response = auth_client.get(LIST)

    assert response.status_code == 200
    assert ids(response) == [newer.pk, older.pk]
    row = response.json()["results"][0]
    assert row["platform"] == "instagram"
    assert row["status"] == "open"
    assert row["assignee"] is None
    assert row["intent"] is None
    assert row["post"]["id"] == target.post_id


def test_the_list_is_paginated(auth_client: Any, target: Any) -> None:
    for n in range(30):
        make_comment(target, f"comment {n}", external_id=f"c-{n}")

    response = auth_client.get(LIST)

    body = response.json()
    assert body["count"] == 30
    assert len(body["results"]) == 25
    assert body["next"]


def test_filters_narrow_by_platform_post_sentiment_intent_status_and_search(
    auth_client: Any, target: Any, paid_workspace: Any, user: Any
) -> None:
    other_account = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="facebook",
        handle="@acme",
        provider_account_id="acct-fb-1",
    )
    other_target = make_target(paid_workspace, user, other_account, age_days=1)
    ig = make_comment(target, "Where can I buy this?", sentiment="POS", external_id="a")
    fb = make_comment(other_target, "Too expensive", sentiment="NEG", external_id="b")
    coding.code_comments([ig, fb])

    assert ids(auth_client.get(LIST, {"platform": "facebook"})) == [fb.pk]
    assert ids(auth_client.get(LIST, {"post": target.post_id})) == [ig.pk]
    assert ids(auth_client.get(LIST, {"sentiment": "NEG"})) == [fb.pk]
    assert ids(auth_client.get(LIST, {"intent": "purchase_intent"})) == [ig.pk]
    assert ids(auth_client.get(LIST, {"q": "expensive"})) == [fb.pk]
    assert set(ids(auth_client.get(LIST, {"q": "MARIA"}))) == {fb.pk, ig.pk}


def test_the_uncoded_filter_means_looked_at_and_could_not_tell(
    auth_client: Any, target: Any
) -> None:
    unseen = make_comment(target, "🔥", external_id="a")
    tried = make_comment(target, "🙂", external_id="b")
    known = make_comment(target, "Where can I buy this?", external_id="c")
    coding.code_comments([tried, known])

    assert set(ids(auth_client.get(LIST, {"intent": "uncoded"}))) == {unseen.pk, tried.pk}


def test_status_and_assignee_filters(
    auth_client: Any, target: Any, user: Any, teammate: Any
) -> None:
    open_one = make_comment(target, "a", external_id="a")
    mine = make_comment(target, "b", external_id="b")
    done = make_comment(target, "c", external_id="c")
    CommentTriage.objects.create(comment=mine, assignee=user)
    CommentTriage.objects.create(comment=done, status="done", assignee=teammate)

    assert set(ids(auth_client.get(LIST, {"status": "open"}))) == {open_one.pk, mine.pk}
    assert ids(auth_client.get(LIST, {"status": "done"})) == [done.pk]
    assert ids(auth_client.get(LIST, {"assignee": "me"})) == [mine.pk]
    assert set(ids(auth_client.get(LIST, {"assignee": "none"}))) == {open_one.pk}
    assert ids(auth_client.get(LIST, {"assignee": str(teammate.pk)})) == [done.pk]


def test_replied_filter(auth_client: Any, target: Any, user: Any) -> None:
    answered = make_comment(target, "a", external_id="a")
    waiting = make_comment(target, "b", external_id="b")
    AudienceCommentAction.objects.create(
        comment=answered, action=CommentAction.REPLY, actor=user, body="thanks"
    )

    assert ids(auth_client.get(LIST, {"replied": "yes"})) == [answered.pk]
    assert ids(auth_client.get(LIST, {"replied": "no"})) == [waiting.pk]


def test_a_comment_deleted_on_the_platform_leaves_the_inbox(
    auth_client: Any, target: Any, user: Any
) -> None:
    gone = make_comment(target, "a", external_id="a")
    kept = make_comment(target, "b", external_id="b")
    AudienceCommentAction.objects.create(comment=gone, action=CommentAction.DELETE, actor=user)

    assert ids(auth_client.get(LIST)) == [kept.pk]


def test_a_comment_older_than_the_plan_keeps_is_not_listed(
    auth_client: Any, target: Any, plans: dict[str, Any], paid_workspace: Any
) -> None:
    """Free keeps 7 days (L-4a); Pro keeps 90. The inbox inherits the horizon
    from the one place that owns it."""
    old = make_comment(target, "old", external_id="old")
    AudienceComment.objects.filter(pk=old.pk).update(
        posted_at=timezone.now() - dt.timedelta(days=30)
    )
    assert ids(auth_client.get(LIST)) == [old.pk]

    paid_workspace.organization.plan = plans["free"]
    paid_workspace.organization.save(update_fields=["plan"])

    assert ids(auth_client.get(LIST)) == []


# --- summary and platform coverage -----------------------------------------------------------
def test_the_summary_counts_by_status_intent_and_assignee(
    auth_client: Any, target: Any, user: Any
) -> None:
    a = make_comment(target, "Where can I buy this?", external_id="a")
    b = make_comment(target, "🙂", external_id="b")
    coding.code_comments([a, b])
    CommentTriage.objects.create(comment=a, assignee=user, status="done")

    body = auth_client.get(SUMMARY).json()

    assert (body["total"], body["open"], body["done"]) == (2, 1, 1)
    assert (body["unassigned"], body["mine"]) == (1, 1)
    assert body["intents"]["purchase_intent"] == 1
    assert body["intents"]["uncoded"] == 1
    assert body["can_reply"] is False  # Pro


def test_tiktok_reads_unavailable_and_a_quiet_platform_reads_none_yet(
    auth_client: Any, target: Any, paid_workspace: Any, user: Any
) -> None:
    """The two must never share a rendering: one is the vendor's limit, the
    other is news about the audience (L-3)."""
    tiktok = SocialAccount.objects.create(
        workspace=paid_workspace, platform="tiktok", handle="@acme", provider_account_id="acct-tt"
    )
    tt_target = make_target(paid_workspace, user, tiktok, age_days=1)
    AudienceComment.objects.create(
        post_target=tt_target,
        external_id="__comments_unavailable__",
        body="",
        posted_at=timezone.now(),
        availability=Availability.UNAVAILABLE,
    )

    body = auth_client.get(SUMMARY).json()
    states = {row["platform"]: row["state"] for row in body["platforms"]}

    assert states == {"instagram": "none_yet", "tiktok": "unavailable"}
    assert ids(auth_client.get(LIST)) == [], "the marker row is not a comment"


def test_a_platform_with_comments_reads_available(auth_client: Any, target: Any) -> None:
    make_comment(target)

    [row] = auth_client.get(SUMMARY).json()["platforms"]

    assert (row["platform"], row["state"], row["comments"]) == ("instagram", "available", 1)


# --- one comment, triage, notes -----------------------------------------------------------
def test_one_comment_carries_its_notes_and_replies(
    auth_client: Any, target: Any, user: Any
) -> None:
    comment = make_comment(target)
    CommentNote.objects.create(comment=comment, author=user, body="ask Lea")
    AudienceCommentAction.objects.create(
        comment=comment, action=CommentAction.REPLY, actor=user, body="Thank you!"
    )

    body = auth_client.get(detail(comment)).json()

    assert [n["body"] for n in body["notes"]] == ["ask Lea"]
    assert [r["body"] for r in body["reply_history"]] == ["Thank you!"]
    assert body["replies"] == 1


def test_assign_close_and_reopen(auth_client: Any, target: Any, teammate: Any) -> None:
    comment = make_comment(target)

    body = auth_client.patch(
        detail(comment), {"assignee": teammate.pk, "status": "done"}, format="json"
    ).json()
    assert (body["status"], body["assignee"]["id"]) == ("done", teammate.pk)

    body = auth_client.patch(
        detail(comment), {"status": "open", "assignee": None}, format="json"
    ).json()
    assert (body["status"], body["assignee"]) == ("open", None)


def test_leaving_the_assignee_out_leaves_it_alone(
    auth_client: Any, target: Any, teammate: Any
) -> None:
    comment = make_comment(target)
    auth_client.patch(detail(comment), {"assignee": teammate.pk}, format="json")

    body = auth_client.patch(detail(comment), {"status": "done"}, format="json").json()

    assert body["assignee"]["id"] == teammate.pk


def test_a_person_from_another_workspace_cannot_be_assigned(auth_client: Any, target: Any) -> None:
    stranger = get_user_model().objects.create_user(email="x@example.com", password=PASSWORD)
    provision_workspace(stranger, name="Elsewhere")
    comment = make_comment(target)

    response = auth_client.patch(detail(comment), {"assignee": stranger.pk}, format="json")

    assert response.status_code == 400
    assert not CommentTriage.objects.filter(comment=comment, assignee=stranger).exists()


def test_an_unknown_status_or_intent_is_a_400(auth_client: Any, target: Any) -> None:
    comment = make_comment(target)

    assert (
        auth_client.patch(detail(comment), {"status": "snoozed"}, format="json").status_code == 400
    )
    assert auth_client.patch(detail(comment), {"intent": "spam"}, format="json").status_code == 400


def test_setting_an_intent_is_a_teammates_call(auth_client: Any, target: Any) -> None:
    comment = make_comment(target, "🙂")

    body = auth_client.patch(detail(comment), {"intent": "praise"}, format="json").json()

    assert (body["intent"], body["coding_source"]) == ("praise", "manual")


def test_an_internal_note_is_stored_and_never_sent(
    auth_client: Any, target: Any, metrics_provider: Any
) -> None:
    comment = make_comment(target)

    response = auth_client.post(
        f"{detail(comment)}notes/", {"body": "  call them  "}, format="json"
    )

    assert response.status_code == 201
    assert CommentNote.objects.get(comment=comment).body == "call them"
    assert metrics_provider.replies == []


def test_an_empty_note_is_a_400(auth_client: Any, target: Any) -> None:
    comment = make_comment(target)

    assert (
        auth_client.post(f"{detail(comment)}notes/", {"body": "   "}, format="json").status_code
        == 400
    )
    assert auth_client.post(f"{detail(comment)}notes/", {}, format="json").status_code == 400


def test_notes_do_not_touch_the_audience_comment(auth_client: Any, target: Any) -> None:
    comment = make_comment(target, "original words")

    auth_client.post(f"{detail(comment)}notes/", {"body": "n"}, format="json")
    auth_client.patch(detail(comment), {"status": "done"}, format="json")

    comment.refresh_from_db()
    assert comment.body == "original words"


# --- reply: Advanced only --------------------------------------------------------------------
@pytest.fixture
def advanced(workspace: Any, plans: dict[str, Any], user: Any, metrics_provider: Any) -> Any:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram", handle="@a", provider_account_id="acct-adv"
    )
    return make_target(workspace, user, account, age_days=1)


def test_a_reply_is_refused_below_advanced_with_an_upgrade(
    auth_client: Any, target: Any, metrics_provider: Any
) -> None:
    comment = make_comment(target)

    response = auth_client.post(f"{detail(comment)}reply/", {"body": "Thanks!"}, format="json")

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"]
    assert metrics_provider.replies == []
    assert not ReplyLedger.objects.exists()


def test_an_advanced_reply_goes_out_and_is_recorded(
    auth_client: Any, advanced: Any, metrics_provider: Any, user: Any
) -> None:
    comment = make_comment(advanced, "Where can I buy this?")

    response = auth_client.post(f"{detail(comment)}reply/", {"body": "In our shop!"}, format="json")

    assert response.status_code == 201
    assert metrics_provider.replies == [(comment.external_id, "In our shop!")]
    body = auth_client.get(detail(comment)).json()
    assert [r["body"] for r in body["reply_history"]] == ["In our shop!"]
    assert body["reply_history"][0]["actor"]["id"] == user.pk


def test_the_summary_says_whether_this_plan_can_reply(auth_client: Any, advanced: Any) -> None:
    assert auth_client.get(SUMMARY).json()["can_reply"] is True


def test_a_reply_over_the_length_limit_or_empty_is_a_400(auth_client: Any, advanced: Any) -> None:
    comment = make_comment(advanced)

    assert (
        auth_client.post(
            f"{detail(comment)}reply/", {"body": "x" * 1001}, format="json"
        ).status_code
        == 400
    )
    assert (
        auth_client.post(f"{detail(comment)}reply/", {"body": ""}, format="json").status_code == 400
    )


# --- permissions: 403 is a role failure ---------------------------------------------------
def member_client(client_as: Any, workspace: Any, role: str, email: str) -> Any:
    person = get_user_model().objects.create_user(email=email, password=PASSWORD)
    Membership.objects.create(user=person, workspace=workspace, role=role)
    return client_as(person)


def test_a_viewer_can_read_but_not_triage_note_or_reply(
    client_as: Any, advanced: Any, workspace: Any
) -> None:
    comment = make_comment(advanced)
    viewer = member_client(client_as, workspace, Role.VIEWER, "v@example.com")

    assert viewer.get(LIST).status_code == 200
    assert viewer.get(detail(comment)).status_code == 200
    assert viewer.patch(detail(comment), {"status": "done"}, format="json").status_code == 403
    assert viewer.post(f"{detail(comment)}notes/", {"body": "x"}, format="json").status_code == 403
    assert viewer.post(f"{detail(comment)}reply/", {"body": "x"}, format="json").status_code == 403


def test_a_contributor_can_triage_but_not_speak_for_the_brand(
    client_as: Any, advanced: Any, workspace: Any
) -> None:
    comment = make_comment(advanced)
    contributor = member_client(client_as, workspace, Role.CONTRIBUTOR, "c@example.com")

    assert contributor.patch(detail(comment), {"status": "done"}, format="json").status_code == 200
    assert (
        contributor.post(f"{detail(comment)}notes/", {"body": "x"}, format="json").status_code
        == 201
    )
    assert (
        contributor.post(f"{detail(comment)}reply/", {"body": "x"}, format="json").status_code
        == 403
    )


def test_an_unauthenticated_caller_is_refused() -> None:
    from rest_framework.test import APIClient

    assert APIClient().get(LIST).status_code == 401


# --- the flag ------------------------------------------------------------------------------
def test_with_the_flag_off_every_endpoint_answers_404(
    auth_client: Any, target: Any, paid_workspace: Any
) -> None:
    comment = make_comment(target)
    FeatureFlag.objects.create(
        organization=paid_workspace.organization, key=INBOX_S2, enabled=False
    )

    for method, url in [
        ("get", LIST),
        ("get", SUMMARY),
        ("get", detail(comment)),
        ("patch", detail(comment)),
        ("post", f"{detail(comment)}notes/"),
        ("post", f"{detail(comment)}reply/"),
        ("get", SAVED),
        ("post", SAVED),
    ]:
        assert getattr(auth_client, method)(url).status_code == 404, f"{method} {url}"


def test_the_flag_off_keeps_what_teammates_already_wrote(
    auth_client: Any, target: Any, paid_workspace: Any
) -> None:
    comment = make_comment(target)
    auth_client.post(f"{detail(comment)}notes/", {"body": "keep me"}, format="json")
    flag = FeatureFlag.objects.create(
        organization=paid_workspace.organization, key=INBOX_S2, enabled=False
    )
    flag.enabled = True
    flag.save()

    assert auth_client.get(detail(comment)).json()["notes"][0]["body"] == "keep me"


# --- tenancy: another organization and a sibling workspace are both 404 ---------------------
def foreign_comment(plans: dict[str, Any]) -> AudienceComment:
    owner = get_user_model().objects.create_user(email="owner2@example.com", password=PASSWORD)
    theirs = provision_workspace(owner, name="Not Yours")
    theirs.organization.plan = plans["advanced"]
    theirs.organization.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=theirs, platform="instagram", handle="@no", provider_account_id="acct-no"
    )
    return make_comment(make_target(theirs, owner, account, age_days=1), "not yours")


def test_another_organizations_comment_is_a_404_everywhere(
    auth_client: Any, target: Any, plans: dict[str, Any]
) -> None:
    theirs = foreign_comment(plans)

    assert theirs.pk not in ids(auth_client.get(LIST))
    assert auth_client.get(detail(theirs)).status_code == 404
    assert auth_client.patch(detail(theirs), {"status": "done"}, format="json").status_code == 404
    assert (
        auth_client.post(f"{detail(theirs)}notes/", {"body": "x"}, format="json").status_code == 404
    )
    assert (
        auth_client.post(f"{detail(theirs)}reply/", {"body": "x"}, format="json").status_code == 404
    )
    assert not CommentTriage.objects.filter(comment=theirs).exists()
    assert not CommentNote.objects.filter(comment=theirs).exists()


def test_a_sibling_workspaces_comment_is_a_404(
    auth_client: Any, workspace: Any, user: Any, plans: dict[str, Any]
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    account = SocialAccount.objects.create(
        workspace=sibling, platform="instagram", handle="@b", provider_account_id="acct-sib"
    )
    theirs = make_comment(make_target(sibling, user, account, age_days=1), "second brand")
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}

    assert auth_client.get(detail(theirs), **here).status_code == 404
    assert theirs.pk not in ids(auth_client.get(LIST, **here))
    assert auth_client.get(detail(theirs), HTTP_X_WORKSPACE_ID=str(sibling.pk)).status_code == 200


def test_a_filter_cannot_reach_across_a_workspace(
    auth_client: Any, target: Any, plans: dict[str, Any]
) -> None:
    theirs = foreign_comment(plans)

    assert ids(auth_client.get(LIST, {"post": theirs.post_target.post_id})) == []


# --- saved replies -------------------------------------------------------------------------------
def test_saved_replies_are_created_listed_edited_and_removed(
    auth_client: Any, workspace: Any
) -> None:
    created = auth_client.post(
        SAVED, {"title": "Shipping", "body": "We ship within 3 days."}, format="json"
    )
    assert created.status_code == 201
    pk = created.json()["id"]

    assert [r["title"] for r in auth_client.get(SAVED).json()["results"]] == ["Shipping"]
    assert (
        auth_client.patch(f"{SAVED}{pk}/", {"body": "Within 2 days."}, format="json").status_code
        == 200
    )
    assert SavedReply.objects.get(pk=pk).body == "Within 2 days."
    assert auth_client.delete(f"{SAVED}{pk}/").status_code == 204
    assert not SavedReply.objects.exists()


def test_a_saved_reply_title_is_unique_within_a_workspace_regardless_of_case(
    auth_client: Any, workspace: Any
) -> None:
    auth_client.post(SAVED, {"title": "Shipping", "body": "a"}, format="json")

    assert (
        auth_client.post(SAVED, {"title": "shipping", "body": "b"}, format="json").status_code
        == 400
    )


def test_two_workspaces_may_share_a_saved_reply_title(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    SavedReply.objects.create(workspace=sibling, title="Shipping", body="theirs")

    response = auth_client.post(
        SAVED,
        {"title": "Shipping", "body": "mine"},
        format="json",
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )

    assert response.status_code == 201


def test_a_saved_reply_is_validated(auth_client: Any, workspace: Any) -> None:
    assert auth_client.post(SAVED, {"title": "", "body": "x"}, format="json").status_code == 400
    assert (
        auth_client.post(SAVED, {"title": "t", "body": "x" * 1001}, format="json").status_code
        == 400
    )
    assert (
        auth_client.post(SAVED, {"title": "t" * 81, "body": "x"}, format="json").status_code == 400
    )


def test_a_viewer_reads_saved_replies_but_cannot_write(
    client_as: Any, workspace: Any, advanced_workspace: Any
) -> None:
    viewer = member_client(client_as, advanced_workspace, Role.VIEWER, "v2@example.com")
    SavedReply.objects.create(workspace=advanced_workspace, title="t", body="b")

    assert viewer.get(SAVED).status_code == 200
    assert viewer.post(SAVED, {"title": "n", "body": "b"}, format="json").status_code == 403


def test_another_workspaces_saved_reply_is_a_404(
    auth_client: Any, workspace: Any, plans: dict[str, Any]
) -> None:
    owner = get_user_model().objects.create_user(email="o3@example.com", password=PASSWORD)
    theirs = SavedReply.objects.create(
        workspace=provision_workspace(owner, name="Elsewhere"), title="t", body="b"
    )

    assert auth_client.get(f"{SAVED}{theirs.pk}/").status_code == 404
    assert (
        auth_client.patch(f"{SAVED}{theirs.pk}/", {"body": "x"}, format="json").status_code == 404
    )
    assert auth_client.delete(f"{SAVED}{theirs.pk}/").status_code == 404
    assert theirs.pk not in [r["id"] for r in auth_client.get(SAVED).json()["results"]]


def test_the_automatic_coding_row_is_visible_in_the_list(auth_client: Any, target: Any) -> None:
    comment = make_comment(target, "Do you make it in blue?")
    coding.code_comments([comment])

    row = auth_client.get(LIST).json()["results"][0]

    assert (row["intent"], row["coding_source"]) == ("product_request", "rules")
    assert CommentCoding.objects.count() == 1


def test_a_provider_that_sent_only_some_flags_does_not_break_the_list(
    auth_client: Any, target: Any
) -> None:
    """The unsent flags are "no", and the comment is still listed."""
    make_comment(target, capabilities={"can_reply": True})

    [row] = auth_client.get(LIST).json()["results"]

    assert row["capabilities"] == {
        "can_reply": True,
        "can_hide": False,
        "can_like": False,
        "can_delete": False,
        "can_private_reply": False,
    }
    [feed] = auth_client.get("/api/v1/analytics/comments/").json()
    assert feed["capabilities"]["can_hide"] is False


def test_a_comment_with_no_flags_at_all_reports_none_not_false(
    auth_client: Any, target: Any
) -> None:
    make_comment(target, capabilities=None)

    [row] = auth_client.get(LIST).json()["results"]

    assert row["capabilities"] is None
