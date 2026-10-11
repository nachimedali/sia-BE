"""The heads-up before a scheduled post goes live.

Beat runs `send_due` every minute (`remind_q`). A post is due for one when it
is on the calendar — `SCHEDULED` for auto-publish, `REMINDER_ARMED` for
reminder mode — and its slot falls within `POST_HEADS_UP_LEAD_MINUTES`.
`Post.heads_up_sent_for` records which slot was announced, so a post is
announced once per slot: rescheduling moves `scheduled_at` away from it and
earns a fresh email, because the old one named a time that is no longer true.

The post in the email comes from `render_post`, the renderer publish uses, so
"rendered exactly as it will go out" is a claim the code keeps.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from accounts.tasks import send_templated_email
from billing.services.flags import TRANSACTIONAL_EMAIL, flag_enabled
from common.mail import base_context, first_name_of
from content.models import DeliveryMode, Platform, Post, PostStatus
from learn.services.segments import resolve_zone

logger = logging.getLogger(__name__)

_TITLE_CHARS = 70
_EXCERPT_CHARS = 180


def lead() -> dt.timedelta:
    return dt.timedelta(minutes=settings.POST_HEADS_UP_LEAD_MINUTES)


def send_due(*, now: dt.datetime | None = None) -> int:
    moment = now or timezone.now()
    due = (
        Post.objects.filter(
            status__in=[PostStatus.SCHEDULED, PostStatus.REMINDER_ARMED],
            scheduled_at__gt=moment,
            scheduled_at__lte=moment + lead(),
        )
        .filter(Q(heads_up_sent_for__isnull=True) | ~Q(heads_up_sent_for=F("scheduled_at")))
        .select_related("author", "workspace", "workspace__organization")
        .prefetch_related("targets", "media_attachments__media_asset")
    )
    enabled: dict[int | None, bool] = {}
    sent = 0
    for post in due:
        organization = post.workspace.organization
        key = organization.pk if organization else None
        if key not in enabled:
            enabled[key] = flag_enabled(organization, TRANSACTIONAL_EMAIL)
        if not enabled[key]:
            continue
        _send_one(post, moment)
        sent += 1
    return sent


def _send_one(post: Post, moment: dt.datetime) -> None:
    # Recorded before the send is queued: a send that exhausts its retries
    # loses one heads-up, while recording after would resend it every minute
    # until the slot passed. One task per post, so one failing address cannot
    # hold up the rest of the scan.
    Post.objects.filter(pk=post.pk).update(heads_up_sent_for=post.scheduled_at)
    context = context_for(post, moment)
    send_templated_email.delay(
        to=post.author.email,
        subject=f"Goes live in {context['countdown']}: {context['post_title']}",
        template="post_heads_up",
        context=context,
    )
    logger.info("post heads-up queued", extra={"post_id": post.pk})


def context_for(post: Post, moment: dt.datetime) -> dict[str, object]:
    from checks.services import fingerprint, latest
    from content.services.adaptation import render_post

    assert post.scheduled_at is not None
    workspace = post.workspace
    zone = resolve_zone(workspace.timezone or "UTC")
    slot = post.scheduled_at.astimezone(zone)
    site = base_context()["site_url"]
    post_url = f"{site}/app/posts/{post.pk}"

    targets = list(post.targets.all())
    platforms = list(dict.fromkeys(t.platform for t in targets))
    rendered = render_post(post, platforms[:1]).get(platforms[0]) if platforms else None
    body = rendered.body if rendered else post.master_body
    image = next((m for m in rendered.media if m.kind == "IMAGE"), None) if rendered else None

    context: dict[str, object] = {
        "first_name": first_name_of(post.author),
        "workspace_name": workspace.name,
        "countdown": duration(post.scheduled_at - moment),
        "lead_time": duration(lead()),
        "slot_day": slot.strftime("%a %d %b"),
        "slot_time": slot.strftime("%H:%M"),
        "timezone": str(zone),
        "post_id": post.pk,
        "post_title": _title(post),
        "caption_excerpt": _clip(body, _EXCERPT_CHARS),
        "platforms": [Platform(p).label for p in platforms] or ["your channels"],
        "format": rendered.post_format.title() if rendered else "",
        "auto_publish": post.delivery_mode == DeliveryMode.AUTO_PUBLISH,
        "post_url": post_url,
        # Straight to the editor's Schedule card.
        "reschedule_url": f"{post_url}#s-schedule",
    }
    if image is not None:
        url = image.url if image.url.startswith(("http://", "https://")) else f"{site}{image.url}"
        context.update(post_image_url=url, post_image_alt=image.alt)

    # Checks are shown only when a run describes the post as it is now; a
    # missing or stale run says nothing, rather than "all passed".
    run = latest(post)
    if run is not None and run.fingerprint == fingerprint(post):
        flagged = [r for r in run.results if r["status"] in {"FIX", "BLOCK"}]
        if flagged:
            context.update(
                fix_count=len(flagged),
                fix_summary="; ".join(r["label"] for r in flagged[:2]) + ".",
            )
        elif all(r["status"] == "PASS" for r in run.results):
            context["checks_total"] = len(run.results)
    return context


def duration(span: dt.timedelta) -> str:
    """ "1 hour 50 minutes", "2 hours", "1 day" — at most two units."""
    minutes = max(0, round(span.total_seconds() / 60))
    if minutes == 0:
        return "under a minute"
    units = [("day", minutes // 1440), ("hour", minutes % 1440 // 60), ("minute", minutes % 60)]
    parts = [f"{n} {name}{'' if n == 1 else 's'}" for name, n in units if n]
    return " ".join(parts[:2])


def _title(post: Post) -> str:
    first_line = next((line for line in post.master_body.splitlines() if line.strip()), "")
    return _clip(first_line, _TITLE_CHARS) or f"Post P-{post.pk}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
