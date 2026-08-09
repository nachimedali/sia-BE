"""Publish idempotency groundwork (design.md §11, I9).

`channels/` (Phase 9) does not exist yet, so nothing calls this outside its
own tests — the same deferred-caller shape as
`products.services.guards.ensure_generation_ready` (design.md A54). When the
publish task lands, it calls `assign_idempotency_key` at schedule time, checks
`PostTarget.provider_post_id` before sending, and passes the same key to
`PlatformAdapter.publish` where the provider accepts one.

The key is a SHA-256 of `PostTarget.id` + the scheduled timestamp, not a
random token: a Celery retry of the *same* attempt calls this with the same
two inputs and must get the same key back, which a random value could not
guarantee without itself being persisted first.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from content.models import PostTarget


def idempotency_key_for(post_target_id: int, scheduled_at: dt.datetime) -> str:
    """Stable across retries of the same publish attempt — `post_target_id`
    and `scheduled_at` are unchanged between them. Rescheduling a post to a
    new time deliberately produces a new key: that is a different publish,
    not a retry of the old one, and must not be treated as a duplicate of it.
    """
    raw = f"{post_target_id}:{scheduled_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def assign_idempotency_key(post_target: PostTarget, scheduled_at: dt.datetime) -> str:
    """Persists the key so a worker restarting mid-retry still has it —
    computing it fresh on every attempt without storing it would work for the
    provider-side idempotency parameter but not for the local
    `provider_post_id` pre-send check design.md §11 also asks for."""
    key = idempotency_key_for(post_target.id, scheduled_at)
    if post_target.idempotency_key != key:
        post_target.idempotency_key = key
        post_target.save(update_fields=["idempotency_key", "updated_at"])
    return key
