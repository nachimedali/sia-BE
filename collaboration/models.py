"""Internal collaboration — the Jira-ticket model (BUILD-PLAN C-03, P2-01).

**Two comment surfaces exist and they never merge** (L-3):

* **before publish, internal** — this module. A team deciding what to change
  and when to ship. Carries status, assignment and resolution, because those
  are what make a remark actionable rather than decorative. Workspace-internal,
  available on every plan, because it is how the product gets used at all.
* **after publish, the audience** — `analytics.AudienceComment`. Real people on
  a real platform. Ingested, never authored here.

They are separate tables on purpose. A schema that lets them share one will
eventually leak an internal note into a public reply, and that failure is not
recoverable by an apology.

**This replaces `workspaces.PostComment`**, which existed only inside the
approval feature and could not outlive it — a remark on a draft that is not
under review had nowhere to go. The rows were carried over into a thread per
post by `0002_threads_from_post_comments`; nothing was dropped.

`Thread.post` is non-null. Phase 5's `ContentCandidate` is the other thing a
thread will hang off (BUILD-PLAN writes it `post_or_candidate`), and it lands
as that phase's own nullable column plus the constraint that exactly one is
set — the same shape `Post.generation` used before Phase 7, and the reason
neither is nullable-and-unconstrained today.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.tokens import digest as token_digest
from common.visibility import Visibility


class ThreadStatus(models.TextChoices):
    """A work item's state, not a post's.

    `LATER` is the one that earns its place: without it a team defers a remark
    by resolving it, and the remark is then indistinguishable from one that was
    acted on. "Not now" and "done" are different answers and a digest that
    conflates them is measuring nothing.
    """

    OPEN = "OPEN", "Open"
    LATER = "LATER", "Later"
    DONE = "DONE", "Done"


class Thread(models.Model):
    """One work item attached to a post: what to change, when to publish, what
    to optimise. **Independent of whether an approval is in flight** — that
    independence is the whole of C-03, and it is why this model carries no FK
    to `ApprovalAction`.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="threads"
    )
    post = models.ForeignKey("content.Post", on_delete=models.CASCADE, related_name="threads")
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=8, choices=ThreadStatus.choices, default=ThreadStatus.OPEN)
    #: Whether the client sees this thread at all. Separate from
    #: `Comment.visibility` and not redundant with it: a thread shared with a
    #: client can still carry an internal aside, and an internal thread on a
    #: shared post must stay invisible whatever its comments say. Both are
    #: filtered in the queryset (`collaboration.views`), never in a serializer.
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.INTERNAL
    )
    #: Nullable and `SET_NULL`: an unassigned thread is the normal state, and
    #: deleting a person must not take the team's open work with them.
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_threads",
    )
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="opened_threads",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_threads",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["post", "-created_at"]),
            # The board view asks "what is still open in this workspace",
            # across every post — no post-leading index helps it.
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.status})"


class Comment(models.Model):
    """One remark in a thread.

    Mutable, unlike `ApprovalAction`: editing a typo in your own comment is
    ordinary, and append-only would make it a second comment saying "typo".
    The record that must survive inconvenience is the approval trail, not the
    conversation around it.
    """

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="comments")
    #: **Exactly one of `author` and `guest_link` is set**, enforced below. A
    #: guest reviewer holds a link, not an account (P2-09), and inventing a
    #: `User` row for them would create something that can be invited,
    #: assigned and emailed — and that would appear in the member list of a
    #: workspace they do not belong to.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="collaboration_comments",
    )
    guest_link = models.ForeignKey(
        "collaboration.ReviewLink",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    body = models.TextField()
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies"
    )
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.INTERNAL
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["thread", "created_at"])]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # In the database, not in a service: a comment signed by nobody is
            # unattributable feedback, and one signed by both a member and a
            # guest link is a lie about who said it.
            models.CheckConstraint(
                condition=models.Q(author__isnull=False, guest_link__isnull=True)
                | models.Q(author__isnull=True, guest_link__isnull=False),
                name="comment_has_exactly_one_author",
            )
        ]

    def __str__(self) -> str:
        return f"comment {self.pk} in thread {self.thread_id}"

    @property
    def author_label(self) -> str:
        """Who to show beside the remark. A guest is named from their link, so
        a client's feedback is not signed with a raw email address."""
        if self.author is not None:
            return self.author.email
        return self.guest_link.reviewer_name if self.guest_link is not None else "Unknown"


class Reaction(models.Model):
    """An emoji from one person on one comment.

    Unique per `(comment, user, emoji)`: clicking 👍 twice is one reaction, and
    a count that says otherwise is a count nobody trusts. Removing one is a
    delete rather than a flag — a reaction has no history worth keeping.
    """

    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comment_reactions"
    )
    #: Grapheme clusters run long — a single flag emoji is 2 code points and a
    #: family sequence can reach 11. Validated as a short single cluster in
    #: `services.react`; the column only has to hold one.
    emoji = models.CharField(max_length=32)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["comment", "user", "emoji"], name="unique_reaction_per_user_comment_emoji"
            )
        ]

    def __str__(self) -> str:
        return f"{self.emoji} on comment {self.comment_id}"


def anchor_digest(text: str) -> str:
    """The comparison key for an annotation's anchor.

    Exact, with no normalisation: collapsing whitespace here would make
    "we ship  Monday" and "we ship Monday" the same anchor, and the second is
    an edit somebody made on purpose.
    """
    return hashlib.sha256(text.encode()).hexdigest()


class Annotation(models.Model):
    """A comment pinned to a range of the post's body (P2-02).

    **Anchored by range *and* content hash, and never re-anchored by search.**
    When the text under `[start:end)` no longer hashes to `anchor_hash`, the
    annotation is marked `orphaned` and shown detached, carrying the words it
    was written about. It is never moved to whatever now occupies that range,
    and it is never fuzzily re-matched elsewhere in the body: a note reading
    "this claim is wrong" silently relocated onto a different sentence is worse
    than the same note shown as unattached, because only one of the two is
    visibly wrong to the person reading it.

    Recovery is symmetric and costs nothing: `services.reanchor` recomputes on
    every content edit, so undoing the change that orphaned an annotation
    re-attaches it. That is not a move — the range is the one originally
    chosen, and the text under it is byte-identical to what was annotated.
    """

    #: One annotation per comment: the anchor is *why* the comment exists, not
    #: an attribute it might have several of. A second range is a second
    #: comment.
    comment = models.OneToOneField(Comment, on_delete=models.CASCADE, related_name="annotation")
    #: Half-open `[anchor_start, anchor_end)` over `Post.master_body`, in
    #: Python string indices — the same units the composer selects in.
    anchor_start = models.PositiveIntegerField()
    anchor_end = models.PositiveIntegerField()
    #: The exact substring at mint time. Stored rather than derived because an
    #: orphaned annotation still has to show what it was about, and by then the
    #: body no longer contains it.
    anchor_text = models.TextField()
    anchor_hash = models.CharField(max_length=64)
    orphaned = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["anchor_start", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # A zero-width anchor selects nothing and can never mismatch, so it
            # would be an annotation that is permanently, meaninglessly valid.
            models.CheckConstraint(
                condition=models.Q(anchor_end__gt=models.F("anchor_start")),
                name="annotation_range_is_not_empty",
            )
        ]

    def __str__(self) -> str:
        state = "orphaned" if self.orphaned else "anchored"
        return f"annotation on comment {self.comment_id} ({state})"

    def matches(self, body: str) -> bool:
        """Whether `body` still carries this annotation's text at its range."""
        return anchor_digest(body[self.anchor_start : self.anchor_end]) == self.anchor_hash


class ReviewLinkPurpose(models.TextChoices):
    APPROVE = "APPROVE", "Approve this post"
    GUEST_VIEW = "GUEST_VIEW", "View and comment"


#: Per purpose, because they answer different questions. Seven days is how long
#: a sign-off request stays actionable before it should be re-sent; thirty is a
#: campaign's worth of client access. Not commercial numbers — nothing is sold
#: by the link — so they live here rather than on a `Plan` row (Part 7 rule 10).
TTL: dict[str, dt.timedelta] = {
    ReviewLinkPurpose.APPROVE: dt.timedelta(days=7),
    ReviewLinkPurpose.GUEST_VIEW: dt.timedelta(days=30),
}

#: Which purposes are spent by being used. `GUEST_VIEW` is deliberately absent.
SINGLE_USE: frozenset[str] = frozenset({ReviewLinkPurpose.APPROVE})

#: The frontend route each purpose lands on. Public, `noindex`, no app chrome —
#: the same treatment `/r/{token}` already gets.
ROUTE: dict[str, str] = {
    ReviewLinkPurpose.APPROVE: "a",
    ReviewLinkPurpose.GUEST_VIEW: "g",
}


class ReviewLink(models.Model):
    """One issued link. Hash-only, like every other credential here."""

    post = models.ForeignKey("content.Post", on_delete=models.CASCADE, related_name="review_links")
    purpose = models.CharField(max_length=16, choices=ReviewLinkPurpose.choices)
    #: Who it was sent to. Stored so a workspace can see and revoke by
    #: recipient — "who can see this post" is unanswerable otherwise, and that
    #: is the question asked in a hurry.
    email = models.EmailField()
    #: A display name for the reviewer, so their comments are not signed with a
    #: raw address. Optional; falls back to the local part of the email.
    display_name = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_review_links",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["post", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.purpose} link for post {self.post_id} → {self.email}"

    @property
    def is_usable(self) -> bool:
        if self.revoked_at is not None or self.expires_at <= timezone.now():
            return False
        return not (self.purpose in SINGLE_USE and self.used_at is not None)

    @property
    def reviewer_name(self) -> str:
        return self.display_name or self.email.split("@")[0]

    @staticmethod
    def hash_token(raw: str) -> str:
        """Delegates to `common.tokens` (P6-06).

        Kept as a method because call sites read better for it, but there is
        one implementation of the digest — two would eventually disagree, and
        a review link and a report share hashing differently would be a quiet,
        permanent authentication difference between surfaces that look alike.
        """
        return token_digest(raw)
