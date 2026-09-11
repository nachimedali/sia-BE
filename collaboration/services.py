"""Internal collaboration behaviour (P2-01, P2-02).

Views parse and serialise; this module decides. Everything that changes a
thread goes through here so notification fan-out (P2-12) has one site per event
to be called from — **explicitly**, never by a signal (Part 7 rule 8).
"""

from __future__ import annotations

import unicodedata
from typing import Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from collaboration.models import (
    Annotation,
    Comment,
    Reaction,
    Thread,
    ThreadStatus,
    anchor_digest,
)
from common.exceptions import OCCSError
from common.visibility import Visibility
from content.models import Post
from notifications import services as notifications
from notifications.models import EventKey


class InvalidAnchorError(OCCSError):
    """400. A range that does not describe a piece of this post's body."""

    default_code = "invalid_anchor"
    default_detail = "That range does not exist in this post."


class InvalidReactionError(OCCSError):
    default_code = "invalid_reaction"
    default_detail = "A reaction is a single emoji."


@transaction.atomic
def open_thread(
    post: Post,
    *,
    author: User,
    title: str,
    body: str,
    visibility: str = Visibility.INTERNAL,
    assignee: User | None = None,
    anchor: tuple[int, int] | None = None,
) -> Thread:
    """A thread and its opening comment are one act, not two.

    A thread with no comment is a title nobody can answer, and letting one
    exist means every reader has to handle it. One transaction, so a failure
    writing the comment cannot leave one behind.

    `workspace` is copied from the post rather than passed in: two arguments
    that must agree are one argument and one bug.
    """
    thread = Thread.objects.create(
        workspace=post.workspace,
        post=post,
        title=title,
        visibility=visibility,
        assignee=assignee,
        opened_by=author,
    )
    add_comment(thread, author=author, body=body, visibility=visibility, anchor=anchor)
    return thread


@transaction.atomic
def add_comment(
    thread: Thread,
    *,
    author: User,
    body: str,
    parent: Comment | None = None,
    visibility: str = Visibility.INTERNAL,
    anchor: tuple[int, int] | None = None,
) -> Comment:
    comment = Comment.objects.create(
        thread=thread, author=author, body=body, parent=parent, visibility=visibility
    )
    if anchor is not None:
        annotate(comment, anchor=anchor)

    # Explicit, not a signal (Part 7 rule 8). Everyone already in the
    # conversation, plus whoever owns it — `notify` drops the author and
    # deduplicates, so a person who is both is told once and the author is not
    # told about their own remark.
    notifications.thread_event(
        thread,
        event_key=EventKey.THREAD_COMMENTED,
        actor=author,
        recipients=[thread.assignee, thread.opened_by, *_participants(thread)],
    )
    return comment


def _participants(thread: Thread) -> list[Any]:
    """Everyone who has already said something here. A reply that reaches only
    the thread's owner leaves the person who raised the point in the dark."""
    return [
        comment.author
        for comment in thread.comments.select_related("author")
        if comment.author_id is not None
    ]


def annotate(comment: Comment, *, anchor: tuple[int, int]) -> Annotation:
    """Pin a comment to `[start, end)` of its post's body.

    The text is read here, once, and stored — so the annotation carries what it
    was written about even after the body no longer does.
    """
    start, end = anchor
    body = comment.thread.post.master_body
    if not (0 <= start < end <= len(body)):
        raise InvalidAnchorError(
            detail={"anchor_start": start, "anchor_end": end, "body_length": len(body)}
        )

    text = body[start:end]
    return Annotation.objects.create(
        comment=comment,
        anchor_start=start,
        anchor_end=end,
        anchor_text=text,
        anchor_hash=anchor_digest(text),
    )


def reanchor(post: Post) -> int:
    """Re-check every annotation on this post against the current body, and
    return how many changed state.

    Called explicitly from `content.services.posts` after a content edit and
    from `content.services.revisions.restore` — **not from a signal** (Part 7
    rule 8), and not lazily at read time, because a reader must not be the one
    who discovers their annotation moved.

    Symmetric on purpose: an annotation whose text comes back is un-orphaned.
    That is not the silent re-anchoring P2-02 forbids — the range is the one
    originally chosen and the text under it is byte-identical to what was
    annotated. Only a *search* for the text elsewhere would be a move, and
    nothing here searches.
    """
    body = post.master_body
    changed = 0
    for annotation in Annotation.objects.filter(comment__thread__post=post):
        orphaned = not annotation.matches(body)
        if orphaned != annotation.orphaned:
            annotation.orphaned = orphaned
            annotation.save(update_fields=["orphaned", "updated_at"])
            changed += 1
    return changed


def set_status(thread: Thread, *, status: str, actor: User) -> Thread:
    """`DONE` records who resolved it and when; leaving `DONE` clears both.

    Clearing rather than keeping the old pair matters: a reopened thread that
    still reads "resolved by Sam on Tuesday" invites everyone to assume it is
    handled.
    """
    fields = ["status", "updated_at"]
    thread.status = status
    if status == ThreadStatus.DONE:
        thread.resolved_by = actor
        thread.resolved_at = timezone.now()
        fields += ["resolved_by", "resolved_at"]
    elif thread.resolved_at is not None:
        thread.resolved_by = None
        thread.resolved_at = None
        fields += ["resolved_by", "resolved_at"]

    thread.save(update_fields=fields)
    return thread


def assign(thread: Thread, *, assignee: User | None, actor: User | None = None) -> Thread:
    thread.assignee = assignee
    thread.save(update_fields=["assignee", "updated_at"])
    if assignee is not None:
        notifications.thread_event(
            thread,
            event_key=EventKey.THREAD_ASSIGNED,
            actor=actor,
            recipients=[assignee],
        )
    return thread


def react(comment: Comment, *, user: User, emoji: str) -> Reaction:
    """Idempotent — a second 👍 from the same person is the same reaction."""
    reaction, _created = Reaction.objects.get_or_create(
        comment=comment, user=user, emoji=_clean_emoji(emoji)
    )
    return reaction


def unreact(comment: Comment, *, user: User, emoji: str) -> None:
    Reaction.objects.filter(comment=comment, user=user, emoji=_clean_emoji(emoji)).delete()


def _clean_emoji(raw: str) -> str:
    """One grapheme, and nothing that could be mistaken for text.

    Counting *code points* would refuse a flag (2) or a family sequence (up to
    11); counting bytes would accept a sentence. So: normalise, refuse
    whitespace, and cap the length at what the longest standard sequence needs.
    A reaction row is rendered as-is next to a person's name, and letting a
    caller put a paragraph there turns a reaction bar into a second comment
    field with no moderation behind it.
    """
    emoji = unicodedata.normalize("NFC", raw.strip())
    if not emoji or any(character.isspace() for character in emoji) or len(emoji) > 16:
        raise InvalidReactionError(detail={"emoji": raw})
    return emoji
