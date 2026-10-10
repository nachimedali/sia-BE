"""`DOC` posts — Universal Content (P3-01, P3-02, P3-03).

A `DOC` is a `Post` with a rich-text body, **zero targets and no adaptation**,
reaching `PUBLISHED` only by a manual mark. It is a *subtype*, not a sibling
model: a sibling would duplicate seven subsystems to avoid one field —
approval chains, threads, revisions, labels, campaign membership, permissions
and quota counting — and the calendar alone settles it, since every view,
filter, bulk action and saved view would have to merge two querysets forever.

**Rich text is a structured block format, never HTML.** HTML in a database is
a sanitisation liability and an export dead end: every read becomes a trust
decision, and the only safe renderer is one that has already parsed it.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.exceptions import ValidationError

from content.models import ContentKind, Post, PostStatus
from content.services.adaptation import render_post
from content.services.blocks import MAX_BLOCKS, validate_document
from content.services.posts import create_post

pytestmark = pytest.mark.django_db

POSTS_URL = "/api/v1/posts/"


def _doc(workspace: Any, user: Any, body: Any = None) -> Post:
    return create_post(
        workspace=workspace,
        author=user,
        content_kind=ContentKind.DOC,
        doc_body=body if body is not None else [{"type": "paragraph", "text": "Hello."}],
    )


# --- the block format ---------------------------------------------------------


class TestBlockFormat:
    def test_a_paragraph_is_accepted(self) -> None:
        assert validate_document([{"type": "paragraph", "text": "Hello."}])

    def test_every_declared_block_type_round_trips(self) -> None:
        document = [
            {"type": "heading", "level": 2, "text": "Q3 plan"},
            {"type": "paragraph", "text": "We ship in March."},
            {"type": "bullet_list", "items": ["One", "Two"]},
            {"type": "ordered_list", "items": ["First", "Second"]},
            {"type": "quote", "text": "Ship it."},
            {"type": "code", "text": "print(1)", "language": "python"},
            {"type": "divider"},
        ]

        assert validate_document(document) == document

    def test_the_document_is_a_list_not_an_object(self) -> None:
        with pytest.raises(ValidationError):
            validate_document({"type": "paragraph", "text": "Hello."})

    def test_an_unknown_block_type_is_refused(self) -> None:
        # Not ignored: a block silently dropped is content the author wrote and
        # cannot see, which is worse than being told it is unsupported.
        with pytest.raises(ValidationError):
            validate_document([{"type": "iframe", "src": "https://example.com"}])

    def test_an_unknown_key_on_a_known_block_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document([{"type": "paragraph", "text": "Hi", "onclick": "alert(1)"}])

    def test_a_missing_required_key_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document([{"type": "heading", "level": 2}])

    def test_a_wrongly_typed_value_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document([{"type": "paragraph", "text": 42}])

    def test_a_heading_level_outside_the_range_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document([{"type": "heading", "level": 7, "text": "Too deep"}])

    def test_a_list_of_non_strings_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document([{"type": "bullet_list", "items": ["fine", {"nested": "no"}]}])

    def test_html_in_text_is_stored_as_text_and_never_as_markup(self) -> None:
        # The block format is the defence: there is no field whose contract is
        # "this will be interpreted", so a tag is characters and stays that way.
        document = [{"type": "paragraph", "text": "<script>alert(1)</script>"}]

        assert validate_document(document)[0]["text"] == "<script>alert(1)</script>"

    def test_a_document_longer_than_the_cap_is_refused(self) -> None:
        # An unbounded document is a denial of service with extra steps — the
        # same reasoning that caps recurrence expansion at 500 slots (P1-10).
        with pytest.raises(ValidationError):
            validate_document([{"type": "divider"}] * (MAX_BLOCKS + 1))

    def test_the_cap_itself_is_allowed(self) -> None:
        assert len(validate_document([{"type": "divider"}] * MAX_BLOCKS)) == MAX_BLOCKS


class TestMarks:
    def test_a_mark_over_a_real_range_is_accepted(self) -> None:
        assert validate_document(
            [
                {
                    "type": "paragraph",
                    "text": "Hello",
                    "marks": [{"type": "bold", "start": 0, "end": 5}],
                }
            ]
        )

    def test_a_mark_past_the_end_of_the_text_is_refused(self) -> None:
        # Anchored by offset like an annotation (P2-02); an offset past the end
        # is not renderable and would be silently clamped by whoever drew it.
        with pytest.raises(ValidationError):
            validate_document(
                [
                    {
                        "type": "paragraph",
                        "text": "Hi",
                        "marks": [{"type": "bold", "start": 0, "end": 9}],
                    }
                ]
            )

    def test_an_inverted_mark_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document(
                [
                    {
                        "type": "paragraph",
                        "text": "Hello",
                        "marks": [{"type": "bold", "start": 4, "end": 2}],
                    }
                ]
            )

    def test_an_empty_mark_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document(
                [
                    {
                        "type": "paragraph",
                        "text": "Hello",
                        "marks": [{"type": "bold", "start": 2, "end": 2}],
                    }
                ]
            )

    def test_a_link_needs_an_href(self) -> None:
        with pytest.raises(ValidationError):
            validate_document(
                [
                    {
                        "type": "paragraph",
                        "text": "Hello",
                        "marks": [{"type": "link", "start": 0, "end": 5}],
                    }
                ]
            )

    def test_a_link_to_a_script_url_is_refused(self) -> None:
        # The one place the block format can still carry executable intent.
        with pytest.raises(ValidationError):
            validate_document(
                [
                    {
                        "type": "paragraph",
                        "text": "Hello",
                        "marks": [
                            {"type": "link", "start": 0, "end": 5, "href": "javascript:alert(1)"}
                        ],
                    }
                ]
            )

    def test_an_http_link_is_accepted(self) -> None:
        assert validate_document(
            [
                {
                    "type": "paragraph",
                    "text": "Hello",
                    "marks": [
                        {"type": "link", "start": 0, "end": 5, "href": "https://example.com"}
                    ],
                }
            ]
        )

    def test_a_mark_on_a_block_that_has_no_text_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_document(
                [{"type": "divider", "marks": [{"type": "bold", "start": 0, "end": 1}]}]
            )


class TestImageBlocks:
    def test_an_image_block_resolves_against_this_workspace(
        self, workspace: Any, user: Any, make_png_upload: Any
    ) -> None:
        from content.services.media import ingest_media

        asset = ingest_media(workspace=workspace, upload=make_png_upload())

        assert validate_document(
            [{"type": "image", "media_asset_id": asset.id, "alt_text": "A tile"}],
            workspace=workspace,
        )

    def test_another_tenants_asset_is_refused(
        self, workspace: Any, other_user: Any, user: Any, make_png_upload: Any
    ) -> None:
        # P1-11 found exactly this: an id-typed media reference validated as an
        # `int` happily accepts another tenant's asset. Validating the *type*
        # is not validating the *reference*.
        from content.services.media import ingest_media
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        asset = ingest_media(workspace=theirs, upload=make_png_upload())

        with pytest.raises(ValidationError):
            validate_document(
                [{"type": "image", "media_asset_id": asset.id, "alt_text": ""}], workspace=workspace
            )

    def test_an_image_block_without_a_workspace_to_check_against_is_refused(self) -> None:
        # Fail closed. A caller that forgot the workspace must not get a free
        # pass on the only cross-tenant check in the format.
        with pytest.raises(ValidationError):
            validate_document([{"type": "image", "media_asset_id": 1, "alt_text": ""}])


# --- the DOC subtype ----------------------------------------------------------


class TestDocPosts:
    def test_a_post_is_social_unless_someone_says_otherwise(
        self, workspace: Any, user: Any
    ) -> None:
        post = create_post(workspace=workspace, author=user, master_body="Hello")

        assert post.content_kind == ContentKind.SOCIAL

    def test_render_post_early_returns_for_a_doc(self, workspace: Any, user: Any) -> None:
        # Part 7 rule 1 holds by *early return*, not by bypass: callers still go
        # through the one renderer, and it answers "nothing to adapt".
        doc = _doc(workspace, user)

        assert render_post(doc, ["instagram", "linkedin"]) == {}

    def test_a_doc_has_no_targets_when_scheduled_is_refused(
        self, workspace: Any, user: Any
    ) -> None:
        import datetime as dt

        from django.utils import timezone

        from common.exceptions import StateConflict
        from scheduling.services import schedule_post

        doc = _doc(workspace, user)

        with pytest.raises(StateConflict):
            schedule_post(
                post=doc,
                delivery_mode="AUTO_PUBLISH",
                scheduled_at=timezone.now() + dt.timedelta(days=1),
                actor=user,
            )

    def test_a_doc_reaches_published_by_a_manual_mark(self, workspace: Any, user: Any) -> None:
        from content.services.posts import mark_doc_published

        doc = _doc(workspace, user)

        assert mark_doc_published(doc, actor=user).status == PostStatus.PUBLISHED

    def test_a_social_post_cannot_be_marked_published_that_way(
        self, workspace: Any, user: Any
    ) -> None:
        # The manual mark exists because a DOC has no publish path. Letting it
        # touch a SOCIAL post would be a second, unaudited route to PUBLISHED.
        from common.exceptions import StateConflict
        from content.services.posts import mark_doc_published

        post = create_post(workspace=workspace, author=user, master_body="Hello")

        with pytest.raises(StateConflict):
            mark_doc_published(post, actor=user)

    def test_a_doc_keeps_its_body_out_of_master_body(self, workspace: Any, user: Any) -> None:
        # Two bodies in one column would make every `master_body` reader — the
        # renderer, the digest, the search — parse before it could trust.
        doc = _doc(workspace, user, [{"type": "paragraph", "text": "The quarterly plan."}])

        assert doc.master_body == ""
        assert doc.doc_body[0]["text"] == "The quarterly plan."

    def test_a_social_post_may_not_carry_a_doc_body(self, workspace: Any, user: Any) -> None:
        with pytest.raises(ValidationError):
            create_post(
                workspace=workspace,
                author=user,
                master_body="Hello",
                doc_body=[{"type": "paragraph", "text": "No."}],
            )


# --- the API ------------------------------------------------------------------


class TestDocApi:
    def test_creating_a_doc(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(
            POSTS_URL,
            {"content_kind": "DOC", "doc_body": [{"type": "heading", "level": 1, "text": "Plan"}]},
            format="json",
        )

        assert response.status_code == 201, response.json()
        assert response.json()["content_kind"] == "DOC"

    def test_a_malformed_block_is_a_400_naming_the_block(
        self, auth_client: Any, workspace: Any
    ) -> None:
        response = auth_client.post(
            POSTS_URL,
            {"content_kind": "DOC", "doc_body": [{"type": "iframe"}]},
            format="json",
        )

        assert response.status_code == 400
        assert "iframe" in str(response.json())

    def test_filtering_by_content_kind(self, auth_client: Any, workspace: Any, user: Any) -> None:
        _doc(workspace, user)
        create_post(workspace=workspace, author=user, master_body="Social one")

        response = auth_client.get(f"{POSTS_URL}?content_kind=DOC")

        assert response.status_code == 200
        assert [row["content_kind"] for row in response.json()["results"]] == ["DOC"]

    def test_an_unknown_content_kind_filter_is_a_400(
        self, auth_client: Any, workspace: Any
    ) -> None:
        # A typo that returns everything is worse than one that 400s — the same
        # rule `?status=` already follows.
        response = auth_client.get(f"{POSTS_URL}?content_kind=ESSAY")

        assert response.status_code == 400

    def test_another_workspaces_doc_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        doc = _doc(theirs, other_user)

        assert auth_client.get(f"{POSTS_URL}{doc.id}/").status_code == 404


# --- quota (P3-03) ------------------------------------------------------------


class TestDocQuota:
    """`counts_docs_against_quota` governs whether a document draws on the same
    **org-pooled** post allowance a social post does (P3-03, L-1).

    These run on the trial plan because that is the plan the pool belongs to:
    on a paid plan `consume_trial_post` is correctly a no-op, so asserting
    there would prove nothing about the flag.
    """

    @pytest.fixture(autouse=True)
    def _on_the_trial_plan(self, workspace: Any, plans: dict[str, Any]) -> None:
        workspace.organization.plan = plans["trial"]
        workspace.organization.save(update_fields=["plan"])

    def test_a_doc_consumes_a_trial_post_when_the_plan_says_so(
        self, workspace: Any, user: Any
    ) -> None:
        from content.services.posts import mark_doc_published

        organization = workspace.organization
        organization.plan.counts_docs_against_quota = True
        organization.plan.save(update_fields=["counts_docs_against_quota"])
        before = organization.trial_posts_used

        mark_doc_published(_doc(workspace, user), actor=user)

        organization.refresh_from_db()
        assert organization.trial_posts_used == before + 1

    def test_a_doc_is_free_when_the_plan_says_so(self, workspace: Any, user: Any) -> None:
        from content.services.posts import mark_doc_published

        organization = workspace.organization
        organization.plan.counts_docs_against_quota = False
        organization.plan.save(update_fields=["counts_docs_against_quota"])
        before = organization.trial_posts_used

        mark_doc_published(_doc(workspace, user), actor=user)

        organization.refresh_from_db()
        assert organization.trial_posts_used == before

    def test_an_exhausted_trial_refuses_the_doc_with_402(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        from common.exceptions import QuotaExceeded
        from content.services.posts import mark_doc_published

        organization = workspace.organization
        organization.plan.counts_docs_against_quota = True
        organization.plan.save(update_fields=["counts_docs_against_quota"])
        organization.trial_posts_used = organization.plan.trial_post_quota
        organization.save(update_fields=["trial_posts_used"])

        with pytest.raises(QuotaExceeded):
            mark_doc_published(_doc(workspace, user), actor=user)

    def test_publishing_a_doc_twice_spends_one_post(self, workspace: Any, user: Any) -> None:
        # Idempotent on the quota, not merely on the status: a double-click
        # must not cost the customer two of a six-post package.
        from content.services.posts import mark_doc_published

        organization = workspace.organization
        organization.plan.counts_docs_against_quota = True
        organization.plan.save(update_fields=["counts_docs_against_quota"])
        before = organization.trial_posts_used
        doc = _doc(workspace, user)

        mark_doc_published(doc, actor=user)
        mark_doc_published(doc, actor=user)

        organization.refresh_from_db()
        assert organization.trial_posts_used == before + 1


class TestUpdateGuardsTheInvariant:
    """`update_post` (P3-01 code review): the same block-format and
    cross-tenant checks `create_post` runs must hold on **every** write path,
    or a PATCH is a second, unvalidated way into the same column.
    """

    def test_patching_doc_body_still_validates_the_block_format(
        self, workspace: Any, user: Any
    ) -> None:
        from content.services.posts import update_post

        doc = _doc(workspace, user)

        with pytest.raises(ValidationError):
            update_post(doc, author=user, doc_body=[{"type": "iframe"}])

    def test_patching_doc_body_still_refuses_another_tenants_image(
        self, workspace: Any, other_user: Any, user: Any, make_png_upload: Any
    ) -> None:
        from content.services.media import ingest_media
        from content.services.posts import update_post
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        asset = ingest_media(workspace=theirs, upload=make_png_upload())
        doc = _doc(workspace, user)

        with pytest.raises(ValidationError):
            update_post(
                doc,
                author=user,
                doc_body=[{"type": "image", "media_asset_id": asset.id, "alt_text": ""}],
            )

    def test_patching_doc_body_still_enforces_the_block_cap(
        self, workspace: Any, user: Any
    ) -> None:
        from content.services.posts import update_post

        doc = _doc(workspace, user)

        with pytest.raises(ValidationError):
            update_post(doc, author=user, doc_body=[{"type": "divider"}] * 1001)

    def test_a_valid_doc_body_patch_is_accepted(self, workspace: Any, user: Any) -> None:
        from content.services.posts import update_post

        doc = _doc(workspace, user)
        new_body = [{"type": "paragraph", "text": "Revised."}]

        updated = update_post(doc, author=user, doc_body=new_body)

        assert updated.doc_body == new_body

    def test_a_social_post_cannot_be_patched_into_carrying_a_doc_body(
        self, workspace: Any, user: Any
    ) -> None:
        # The invariant create_post enforces at birth — DOC body, DOC kind, or
        # neither — must hold after a PATCH too, or the two can drift apart via
        # a partial update that only names one of them.
        from content.services.posts import update_post

        post = create_post(workspace=workspace, author=user, master_body="Hello")

        with pytest.raises(ValidationError):
            update_post(post, author=user, doc_body=[{"type": "paragraph", "text": "No."}])

    def test_flipping_content_kind_to_doc_with_no_body_matches_creates_own_leniency(
        self, workspace: Any, user: Any
    ) -> None:
        # `create_post(content_kind=DOC)` with no explicit body validates an
        # empty document and succeeds — `update_post` has to agree, or the same
        # request means something different depending on which endpoint wrote
        # it.
        from content.services.posts import update_post

        post = create_post(workspace=workspace, author=user, master_body="Hello")

        updated = update_post(post, author=user, content_kind=ContentKind.DOC)

        assert updated.content_kind == ContentKind.DOC

    def test_patching_an_unrelated_field_on_a_doc_does_not_require_touching_the_body(
        self, workspace: Any, user: Any
    ) -> None:
        # The guard must not force every PATCH to resend doc_body.
        from content.services.posts import update_post

        doc = _doc(workspace, user)

        updated = update_post(doc, author=user, category=None)

        assert updated.doc_body == doc.doc_body


class TestApprovalVoidsOnDocEdit:
    """`doc_body` is a content field for the approval-revert rule too.

    A DOC can reach `APPROVED` through the ordinary chain — nothing excludes
    it — so the same reasoning that reverts a SOCIAL post on a `master_body`
    edit has to cover a document's own content, or an approval silently
    outlives the text it was granted for.
    """

    def test_editing_an_approved_docs_body_reverts_it_to_pending_review(
        self, workspace: Any, user: Any
    ) -> None:
        from content.models import PostStatus
        from content.services.posts import update_post

        doc = _doc(workspace, user)
        doc.status = PostStatus.APPROVED
        doc.save(update_fields=["status"])

        updated = update_post(
            doc, author=user, doc_body=[{"type": "paragraph", "text": "Changed."}]
        )

        assert updated.status == PostStatus.PENDING_REVIEW


class TestDocRevisionHistory:
    """Found in review: `snapshot_of` never touched `doc_body`, so editing a
    document's content produced zero `PostRevision` rows — silently, since
    `master_body`/media/targets never change for a DOC, and `record()` only
    appends when *something* moved. A brief with no edit history contradicts
    the explicit reason `Campaign.brief` gives for `DOC` being a subtype at
    all: "the brief gets threads, revisions and approval like anything else
    written here."
    """

    def test_editing_a_docs_body_records_a_revision(self, workspace: Any, user: Any) -> None:
        from content.services.posts import update_post

        doc = _doc(workspace, user, [{"type": "paragraph", "text": "v1"}])

        update_post(doc, author=user, doc_body=[{"type": "paragraph", "text": "v2"}])

        latest = doc.revisions.order_by("-sequence").first()
        assert latest is not None
        assert latest.sequence == 2
        assert latest.diff["doc_body"] == [
            [{"type": "paragraph", "text": "v1"}],
            [{"type": "paragraph", "text": "v2"}],
        ]

    def test_a_doc_save_that_changes_nothing_records_nothing(
        self, workspace: Any, user: Any
    ) -> None:
        from content.services.posts import update_post

        doc = _doc(workspace, user, [{"type": "paragraph", "text": "Same"}])

        update_post(doc, author=user, doc_body=[{"type": "paragraph", "text": "Same"}])

        assert doc.revisions.count() == 1

    def test_restoring_an_old_revision_actually_brings_the_text_back(
        self, workspace: Any, user: Any
    ) -> None:
        from content.services.posts import update_post
        from content.services.revisions import restore

        doc = _doc(workspace, user, [{"type": "paragraph", "text": "v1"}])
        update_post(doc, author=user, doc_body=[{"type": "paragraph", "text": "v2"}])
        assert doc.doc_body == [{"type": "paragraph", "text": "v2"}]

        restored = restore(doc, sequence=1, author=user)

        assert restored.doc_body == [{"type": "paragraph", "text": "v1"}]
