"""The publish journal over `PostTarget`, and the media a provider is handed (SA-31, SA-32).

`TargetJournal` is what an adapter writes to before each step it takes. Every
`record` saves at once, outside any transaction the caller might hold
(`publish_post_by_id` deliberately runs without one): a step that happened at
the provider must be on disk before the next one starts, or a retry would
repeat it.

`publish_media` builds `PublishMedia` from the workspace's own `MediaAsset`
rows, in the payload's order, at publish time — so a signed URL is fresh, not
the one minted when the post was scheduled, and the bytes are there for a
provider that cannot fetch our storage.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from django.conf import settings
from django.utils import timezone

from channels.adapters.base import PlatformError, PublishMedia, absolute_media_url
from content.models import MediaAsset, PostTarget


class TargetJournal:
    def __init__(self, target: PostTarget) -> None:
        self._target = target

    @property
    def provider_post_id(self) -> str:
        return self._target.provider_post_id

    @property
    def provider_state(self) -> str:
        return self._target.provider_state

    @property
    def provider_media_ids(self) -> list[str]:
        return list(self._target.provider_media_ids or [])

    def record(
        self,
        *,
        provider_post_id: str | None = None,
        provider_state: str | None = None,
        media_ids: list[str] | None = None,
    ) -> None:
        target = self._target
        fields = ["provider_state_at", "updated_at"]
        if provider_post_id is not None:
            target.provider_post_id = provider_post_id
            fields.append("provider_post_id")
        if provider_state is not None:
            target.provider_state = provider_state
            fields.append("provider_state")
        if media_ids is not None:
            target.provider_media_ids = list(media_ids)
            fields.append("provider_media_ids")
        target.provider_state_at = timezone.now()
        target.save(update_fields=fields)


def _url(asset: MediaAsset) -> str:
    """A URL a provider can fetch: signed for long enough on object storage,
    absolute on the local filesystem (dev, where uploads are used instead)."""
    signed_url: Any = asset.file.storage.url
    try:
        url = str(signed_url(asset.file.name, expire=settings.SOCIALAPI_MEDIA_URL_TTL_SECONDS))
    except TypeError:  # FileSystemStorage.url takes no expiry
        url = str(asset.file.url)
    return absolute_media_url(url)


def _size(asset: MediaAsset) -> int:
    try:
        return int(asset.file.size)
    except (OSError, ValueError):
        return 0


def publish_media(target: PostTarget) -> list[PublishMedia]:
    """The target's media as rendered, resolved inside its own workspace. An
    asset that no longer exists is a terminal error, not a post without it."""
    media = (target.rendered_payload or {}).get("media") or []
    ids = [int(item["id"]) for item in media if "id" in item]
    if not ids:
        return []
    assets = {
        asset.pk: asset
        for asset in MediaAsset.objects.filter(pk__in=ids, workspace_id=target.post.workspace_id)
    }
    missing = [media_id for media_id in ids if media_id not in assets or not assets[media_id].file]
    if missing:
        raise PlatformError(
            "A media file in this post no longer exists, so it was not published.",
            code="publish_media_missing",
            detail={"media": missing},
        )
    return [
        PublishMedia(
            id=asset.pk,
            url=_url(asset),
            filename=(asset.file.name or "media").rsplit("/", 1)[-1],
            mime=asset.mime or "application/octet-stream",
            size=_size(asset),
            open=partial(asset.file.open, "rb"),
        )
        for asset in (assets[media_id] for media_id in ids)
    ]
