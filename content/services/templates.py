"""Post templates (P1-09).

Two operations: validate a payload on the way in, and copy it into a new post
on the way out.

**Validation happens on save, not on apply.** A template is written once and
applied many times; catching a bad platform option at write time costs one
400, catching it at apply time costs one failure per use, at the moment
somebody is trying to get a post out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction

from common.exceptions import OCCSError
from content.models import MediaAsset, Platform, PostTarget, PostTemplate, TemplateKind
from content.services import options as option_rules
from content.services.posts import create_post
from content.services.rules import OPTIONS_SCHEMA_VERSION

if TYPE_CHECKING:
    from accounts.models import User
    from content.models import Post

#: `DOC` needs Phase 3's `Post.content_kind` and its block renderer before it
#: can become anything. Declared in the enum, refused here — the same
#: declare-then-gate shape `ai.services.pipeline.DIRECTLY_CREATABLE_MODES`
#: uses.
CREATABLE_KINDS = frozenset({TemplateKind.SOCIAL})


class ContentKindNotAvailableError(OCCSError):
    default_code = "content_kind_not_available"
    default_detail = "That kind of template is not available yet."


class InvalidTemplatePayloadError(OCCSError):
    default_code = "invalid_platform_options"
    default_detail = "This template's platform options are not valid."


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Returns the payload with its options cleaned, or raises."""
    platform_options = payload.get("platform_options") or {}
    if not isinstance(platform_options, dict):
        raise InvalidTemplatePayloadError(detail={"platform_options": "Expected an object."})

    cleaned: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    for platform, values in platform_options.items():
        if platform not in Platform.values:
            errors[platform] = f"Unknown platform '{platform}'."
            continue
        if not isinstance(values, dict):
            errors[platform] = "Expected an object of options."
            continue
        try:
            cleaned[platform] = option_rules.validate(platform, values)
        except option_rules.OptionError as exc:
            errors[platform] = exc.errors

    if errors:
        raise InvalidTemplatePayloadError(detail=errors)

    media_ids = payload.get("media_asset_ids") or []
    if not isinstance(media_ids, list) or not all(isinstance(i, int) for i in media_ids):
        raise InvalidTemplatePayloadError(
            detail={"media_asset_ids": "Expected a list of media ids."}
        )

    return {
        "master_body": str(payload.get("master_body") or ""),
        "media_asset_ids": media_ids,
        "platform_options": cleaned,
    }


def ensure_creatable(content_kind: str) -> None:
    if content_kind not in CREATABLE_KINDS:
        raise ContentKindNotAvailableError(detail={"content_kind": content_kind})


@transaction.atomic
def apply_template(template: PostTemplate, *, author: User) -> Post:
    """Copies the template into a new draft post.

    Media ids are resolved **against the template's own workspace**, so a
    hand-edited payload cannot reference another tenant's asset — an id that
    does not resolve is simply absent. Missing ids are skipped rather than
    fatal: a template outlives the media it was saved with, and refusing to
    apply would strand the template over one deleted image.
    """
    payload = template.payload or {}
    wanted = [i for i in (payload.get("media_asset_ids") or []) if isinstance(i, int)]
    found = {
        asset.pk: asset
        for asset in MediaAsset.objects.filter(workspace=template.workspace, pk__in=wanted)
    }
    assets = [found[i] for i in wanted if i in found]

    post = create_post(
        workspace=template.workspace,
        author=author,
        master_body=str(payload.get("master_body") or ""),
        media_assets=assets,
    )

    for platform, values in (payload.get("platform_options") or {}).items():
        if platform not in Platform.values:
            continue
        PostTarget.objects.update_or_create(
            post=post,
            platform=platform,
            social_account=None,
            defaults={
                "platform_options": values,
                "options_schema_version": OPTIONS_SCHEMA_VERSION,
            },
        )
    return post
