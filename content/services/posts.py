"""Post authoring (implementation.md §4.1: business logic in services/, never
in serializers or views; a Celery task body is a call into this module too).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rest_framework.exceptions import ValidationError

from accounts.models import User
from categories.models import Category
from collaboration import services as collaboration
from common.exceptions import OCCSError, StateConflict
from content.models import (
    ContentKind,
    MediaAsset,
    Post,
    PostMediaAttachment,
    PostStatus,
    PostTarget,
)
from content.services import options as option_rules
from content.services import revisions
from content.services.blocks import validate_document
from content.services.rules import OPTIONS_SCHEMA_VERSION
from workspaces.models import Workspace
from workspaces.services import approvals


class MediaNotAttachedError(OCCSError):
    """400, not a quietly created row: an asset that is not on the post has no
    *use* to describe, and alt text describes a use (P1-06)."""

    default_code = "media_not_attached"
    default_detail = "That media asset is not attached to this post."


class InvalidPlatformOptionsError(OCCSError):
    """Carries the per-key errors, so a composer marks the field that is wrong
    rather than showing one message for a form of eight inputs."""

    default_code = "invalid_platform_options"
    default_detail = "These platform options are not valid."


class NoTargetForPlatformError(OCCSError):
    default_code = "no_target_for_platform"
    default_detail = "This post has no target on that platform."


def _replace_media(post: Post, media_assets: Sequence[MediaAsset]) -> None:
    """Full replace, not a diff: a composer sends the whole ordered list on
    every save, so reconciling an add/remove/reorder delta would be solving a
    problem nobody has yet.

    The unfiltered delete is deliberate — it takes the per-target alt-text
    override rows with it. An override for an asset the post no longer carries
    is a row pointing at nothing, and leaving it would resurrect stale text the
    day that asset is re-added.
    """
    PostMediaAttachment.objects.filter(post=post).delete()
    PostMediaAttachment.objects.bulk_create(
        PostMediaAttachment(post=post, media_asset=asset, order=index)
        for index, asset in enumerate(media_assets)
    )


def create_post(
    *,
    workspace: Workspace,
    author: User,
    master_body: str = "",
    category: Category | None = None,
    media_assets: Sequence[MediaAsset] = (),
    content_kind: str = ContentKind.SOCIAL,
    doc_body: Any | None = None,
) -> Post:
    if doc_body is not None and content_kind != ContentKind.DOC:
        # Rejected rather than ignored. A `SOCIAL` post carrying a document
        # body is a row two readers would disagree about, and the author who
        # wrote that text would never be told it went nowhere.
        raise ValidationError({"doc_body": "Only a DOC post carries a document body."})
    if content_kind == ContentKind.DOC:
        doc_body = validate_document(doc_body or [], workspace=workspace)

    post = Post.objects.create(
        workspace=workspace,
        author=author,
        master_body=master_body,
        category=category,
        content_kind=content_kind,
        doc_body=doc_body or [],
    )
    if media_assets:
        _replace_media(post, media_assets)
    # After the media, not before: revision 1 is the anchor every later diff is
    # measured against, and an anchor that omits the media the post was created
    # with would make the next save look like the media had just been added.
    revisions.record(post, author=author, reason="created", force=True)
    return post


#: Fields whose change means "the content changed", not just the metadata
#: around it. Deliberately narrow: `source`/`product`/`generation`/`category`
#: are all written by other phases' services (autopilot, repurposing) through
#: this same function, and none of them should silently undo an approval.
_CONTENT_FIELDS = frozenset({"master_body", "media_asset_ids", "doc_body"})


def update_post(
    post: Post, *, author: User | None = None, reason: str = "edited", **fields: Any
) -> Post:
    """`fields` is exactly what the caller wants to change — a PATCH that
    omits `media_asset_ids` must not touch attachment order, so the view only
    passes keys that were actually present in the request body.

    **A locked post refuses every edit with a 409** (P2-11). Checked here
    rather than in the view so a task, a command or a later endpoint inherits
    it; an `admin` unlocks, and that is audited.

    **Editing an unlocked `APPROVED` post reverts it to `PENDING_REVIEW`**
    (design.md §8.8) — approval attaches to content, not to the record, so a
    change to what would actually publish voids it. The two rules meet exactly
    once, and coherently: approval locks, an admin unlocks, and the first edit
    after that sends the post back for review. Only a content change does this;
    `product`/`generation`/`source`/`category` are metadata other phases'
    services write through this same function, and none of them is the thing an
    approver signed off on.
    """
    approvals.ensure_unlocked(post)

    # **The same invariant `create_post` enforces at birth, held on every
    # later write too** (found in review: a PATCH reached `setattr` with zero
    # validation, which reopened the cross-tenant image hole `_validate_image`
    # exists to close, skipped the 1,000-block cap, and let `content_kind` and
    # `doc_body` disagree — exactly the state `create_post`'s own check exists
    # to forbid). Checked against the **resulting** kind, not the post's
    # current one: a caller converting a draft to a document in one request
    # sends both fields together, and only the combination after the patch is
    # meaningful.
    if "doc_body" in fields:
        resulting_kind = fields.get("content_kind", post.content_kind)
        if resulting_kind != ContentKind.DOC:
            raise ValidationError({"doc_body": "Only a DOC post carries a document body."})
        fields["doc_body"] = validate_document(fields["doc_body"], workspace=post.workspace)

    touches_content = bool(_CONTENT_FIELDS & fields.keys())
    media_assets = fields.pop("media_asset_ids", None)

    for name, value in fields.items():
        setattr(post, name, value)

    update_fields = [*fields.keys()]
    reverted = touches_content and post.status == PostStatus.APPROVED
    if reverted:
        post.status = PostStatus.PENDING_REVIEW
        update_fields.append("status")

    if update_fields:
        post.save(update_fields=[*update_fields, "updated_at"])

    if media_assets is not None:
        _replace_media(post, media_assets)

    if reverted:
        approvals.log(
            workspace=post.workspace,
            verb="post.edited_after_approval",
            target_repr=str(post),
            meta={"post": post.pk},
        )

    if touches_content:
        # An explicit call, not a signal (Part 7 rule 8), and not a lazy check
        # at read time — a reader must not be the one who discovers that their
        # annotation no longer describes the text under it (P2-02).
        collaboration.reanchor(post)

    # One revision for the whole edit, recorded after every table it touched —
    # a body change and a media reorder submitted together are one version of
    # the post, not two.
    revisions.record(post, author=author, reason=reason)
    return post


def set_alt_text(
    post: Post,
    *,
    media_asset: MediaAsset,
    alt_text: str,
    target: PostTarget | None = None,
    author: User | None = None,
) -> PostMediaAttachment:
    """Describe one image, either for the whole post or for one platform.

    Separate from `update_post` on purpose. A composer sends its whole media
    list on every save, which is the right shape for selection and order and
    the wrong one for a description someone writes once in an image panel —
    folding alt text into that payload would mean every save either carries
    every description or silently drops the ones it omits.

    Not a content change for approval purposes either: adding a description to
    an already-approved image improves accessibility without altering what an
    approver signed off on, and reverting the post to `PENDING_REVIEW` for it
    would teach people not to bother.
    """
    approvals.ensure_unlocked(post)

    base = PostMediaAttachment.objects.filter(
        post=post, media_asset=media_asset, target_override__isnull=True
    ).first()
    if base is None:
        raise MediaNotAttachedError(detail={"media_asset": media_asset.pk, "post": post.pk})

    if target is None:
        base.alt_text = alt_text
        base.save(update_fields=["alt_text"])
        revisions.record(post, author=author, reason="alt text")
        return base

    override, _created = PostMediaAttachment.objects.update_or_create(
        post=post,
        media_asset=media_asset,
        target_override=target,
        defaults={"alt_text": alt_text, "order": base.order},
    )
    revisions.record(post, author=author, reason="alt text")
    return override


def target_for_platform(post: Post, platform: str) -> PostTarget:
    target = post.targets.filter(platform=platform).first()
    if target is None:
        raise NoTargetForPlatformError(detail={"platform": platform, "post": post.pk})
    return target


def set_platform_options(
    post: Post,
    *,
    platform: str,
    options: dict[str, Any],
    author: User | None = None,
) -> PostTarget:
    """Writes one platform's composer settings onto its target (P1-11).

    Creates the target if it does not exist yet: a composer configures a
    platform long before the post is scheduled, and `build_targets` does not
    run until it is. The row created here carries no `social_account` — which
    account publishes is the schedule service's decision, and this is not it.

    Validation is `options.validate`, the strict one, because this *is* the way
    in. `render_post` uses the lenient `resolve` on the way out, for stored
    rows that a later rule change made unusable.
    """
    approvals.ensure_unlocked(post)

    try:
        cleaned = option_rules.validate(platform, options, workspace=post.workspace)
    except option_rules.OptionError as exc:
        raise InvalidPlatformOptionsError(detail=exc.errors) from exc

    target, _created = PostTarget.objects.get_or_create(
        post=post, platform=platform, social_account=None
    )
    target.platform_options = cleaned
    target.options_schema_version = OPTIONS_SCHEMA_VERSION
    target.save(update_fields=["platform_options", "options_schema_version", "updated_at"])

    revisions.record(post, author=author, reason=f"{platform} options")
    return target


def mark_doc_published(post: Post, *, actor: User) -> Post:
    """A `DOC` reaching `PUBLISHED` by hand (P3-01).

    **The only status transition in the system with no delivery behind it.** A
    document has no targets, no adaptation and no provider; "published" here
    means the team has agreed it is finished, which is a claim only a person
    can make. It is refused on a `SOCIAL` post because that would be a second,
    unaudited route past the publish pipeline — exactly what Part 7 rule 13
    exists to prevent.

    **Idempotent on the quota, not merely on the status** (P3-03). A
    double-click must not cost a customer two posts out of a six-post package,
    so the trial is spent on the transition into `PUBLISHED` and never again.
    """
    from billing.services import trial
    from billing.services.entitlements import entitlements_for

    if post.content_kind != ContentKind.DOC:
        raise StateConflict(
            "Only a document is published by hand; a social post goes through scheduling.",
            detail={"content_kind": post.content_kind},
        )
    if post.status == PostStatus.PUBLISHED:
        return post

    # Before the status write, so an exhausted trial leaves the document
    # exactly as it was rather than published-but-unpaid.
    if entitlements_for(post.workspace).plan.counts_docs_against_quota:
        trial.consume_trial_post(post.workspace)

    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status", "updated_at"])
    revisions.record(post, author=actor, reason="published")
    return post
