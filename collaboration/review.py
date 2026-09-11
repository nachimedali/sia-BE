"""Token-scoped review — sending a post to someone with no account (P2-09).

The `ReviewLink` row itself lives in `collaboration.models` with the rest of
this app's tables; what is here is everything that *happens* to one.

**The reminder token pattern, reused wholesale** rather than reinvented: minted
at send, only its SHA-256 hash persisted, expiring, and resolved by one lookup
that is itself the access control. That shape is already correct and already
tested (`reminders.services.resolve_token`); a second, subtly different
credential is how one of the two ends up without a TTL.

Two purposes, and the difference between them is the whole design:

| | `APPROVE` | `GUEST_VIEW` |
|---|---|---|
| lifetime | 7 days | 30 days |
| uses | **single** — spent on approval | **multi** |
| what it can do | read, comment, approve once | read, comment |

**Multi-use is the deviation, and it is why `revoke` exists.** A single-use
token expires by being consumed; a link a client can open all month cannot, so
"stop that person seeing this" needs an explicit endpoint or it has no answer
at all.

**Sharing sets `Post.visibility = SHARED`.** Minting a link *is* the act of
sharing, and the guest queryset filters on visibility (P2-03) — leaving the two
as separate steps would make the common failure "I sent the link and they see
nothing", with no error anywhere to explain it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from collaboration.models import (
    ROUTE,
    SINGLE_USE,
    TTL,
    Comment,
    ReviewLink,
    ReviewLinkPurpose,
    Thread,
)
from common.mail import Email, get_mail_sender
from common.visibility import Visibility
from content.models import Post

if TYPE_CHECKING:
    from accounts.models import User


@transaction.atomic
def issue(
    post: Post,
    *,
    purpose: str,
    email: str,
    created_by: User,
    display_name: str = "",
) -> tuple[ReviewLink, str]:
    """Mint a link, share the post, and email the recipient.

    Supersedes any outstanding link of the same purpose to the same address, for
    the reason a verification email does: without it, re-sending leaves the
    earlier link live, which widens the window on one that may have gone to a
    mistyped address.
    """
    normalised = email.strip().lower()
    ReviewLink.objects.filter(
        post=post, purpose=purpose, email=normalised, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())

    raw = secrets.token_urlsafe(32)
    link = ReviewLink.objects.create(
        post=post,
        purpose=purpose,
        email=normalised,
        display_name=display_name,
        created_by=created_by,
        token_hash=ReviewLink.hash_token(raw),
        expires_at=timezone.now() + TTL[purpose],
    )

    if post.visibility != Visibility.SHARED:
        post.visibility = Visibility.SHARED
        post.save(update_fields=["visibility", "updated_at"])

    url = f"{settings.SITE_URL}/{ROUTE[purpose]}/{raw}"
    get_mail_sender().send(
        Email(
            to=normalised,
            subject=(
                "A post is waiting for your approval"
                if purpose == ReviewLinkPurpose.APPROVE
                else "A post has been shared with you"
            ),
            template="review_link",
            context={
                "post": post,
                "review_url": url,
                "workspace": post.workspace,
                "needs_approval": purpose == ReviewLinkPurpose.APPROVE,
            },
        )
    )
    return link, raw


def resolve(raw: str) -> ReviewLink | None:
    """The one lookup every public review view goes through.

    `None` for an unknown, expired, spent or revoked token — the caller cannot
    tell those apart, which is the point: distinguishing them would confirm
    that a post exists behind a token someone guessed.
    """
    link = (
        ReviewLink.objects.select_related("post", "post__workspace")
        .filter(token_hash=ReviewLink.hash_token(raw))
        .first()
    )
    return link if link is not None and link.is_usable else None


def spend(link: ReviewLink) -> ReviewLink:
    """Consume a single-use link. A no-op for `GUEST_VIEW`, which is revoked
    rather than spent."""
    if link.purpose in SINGLE_USE and link.used_at is None:
        link.used_at = timezone.now()
        link.save(update_fields=["used_at"])
    return link


def revoke(link: ReviewLink) -> ReviewLink:
    """Idempotent: two people revoking the same link at once is an ordinary
    race, not a conflict worth surfacing."""
    if link.revoked_at is None:
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
    return link


def guest_context(link: ReviewLink) -> dict[str, Any]:
    """What a guest is allowed to see: the post as it would publish, plus the
    threads and comments marked `SHARED`.

    Assembled here rather than in the serializer so the audience filter is
    applied once, in one place — the same reason the queryset mixins exist.

    The previews come from `render_post`, the **one renderer** (Part 7 rule 1).
    A client signing off on a rendering that is not what publish sends would be
    signing off on nothing.
    """
    from common.visibility import Audience, visible_values
    from content.services.adaptation import render_post

    allowed = visible_values(Audience.CLIENT)
    post = link.post
    platforms = [target.platform for target in post.targets.all()] or list(post.workspace.platforms)
    threads = Thread.objects.filter(post=post, visibility__in=allowed).prefetch_related(
        Prefetch(
            "comments",
            queryset=Comment.objects.filter(visibility__in=allowed).select_related(
                "author", "guest_link"
            ),
        )
    )
    return {
        "link": link,
        "post": post,
        "payloads": {
            platform: payload.as_dict()
            for platform, payload in render_post(post, platforms).items()
        },
        "threads": threads,
    }


@transaction.atomic
def guest_comment(link: ReviewLink, *, body: str, thread: Any | None = None) -> Any:
    """A client's reply, landing in the same thread the team is working in.

    **Shared, never internal** — a guest cannot author something the client
    surface would then hide from them, and cannot see the internal asides
    around it either.

    With no thread named, one is opened for them. A client's first remark
    usually starts a conversation rather than joining one, and forcing them to
    pick a thread from a list they can only partly see is a worse question than
    it looks.
    """

    if thread is None:
        thread = Thread.objects.create(
            workspace=link.post.workspace,
            post=link.post,
            title=f"From {link.reviewer_name}",
            visibility=Visibility.SHARED,
        )
    return Comment.objects.create(
        thread=thread,
        author=None,
        guest_link=link,
        body=body,
        visibility=Visibility.SHARED,
    )
