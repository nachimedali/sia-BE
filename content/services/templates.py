"""Post templates (P1-09).

Two operations: validate a payload on the way in, and copy it into a new post
on the way out.

**Validation happens on save, not on apply.** A template is written once and
applied many times; catching a bad platform option at write time costs one
400, catching it at apply time costs one failure per use, at the moment
somebody is trying to get a post out.
"""

from __future__ import annotations

import re
from decimal import Decimal
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
    from products.models import Product
    from workspaces.models import Workspace

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


class InvalidTemplateRulesError(OCCSError):
    default_code = "invalid_template_rules"
    default_detail = "This template's products, accounts or modes are not valid."


class UnknownPlaceholderError(OCCSError):
    default_code = "unknown_placeholder"
    default_detail = "This template's text uses a placeholder that does not exist."


class PlaceholderNeedsProductError(OCCSError):
    default_code = "placeholder_needs_product"
    default_detail = "A placeholder is filled from a product; choose at least one."


#: S18. How a slot's image and text are made. The first of each is the default.
IMAGE_MODES = {"product_photo": "The product's own photo", "generate": "A new generated image"}
TEXT_MODES = {"placeholder": "The template's text, placeholders filled", "generate": "New text"}

#: S18. `{key}` in a template's text, filled from the slot's product — read
#: only, never written back.
PLACEHOLDERS = {
    "product": "Product name",
    "price": "Price with currency",
    "link": "Product link",
    "description": "Short description",
}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def placeholders_in(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def fill_placeholders(text: str, product: Product | None) -> str:
    """The text with each known `{key}` replaced from `product`; unchanged
    without one. Unknown keys are refused at save, so none reach here."""
    if product is None:
        return text
    price = (
        f"{product.price.quantize(Decimal('0.001'))} {product.price_currency}"
        if product.price is not None
        else ""
    )
    values = {
        "product": product.name,
        "price": price,
        "link": product.product_url,
        "description": product.short_description,
    }
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _ids(payload: dict[str, Any], key: str) -> list[int]:
    value = payload.get(key) or []
    if not isinstance(value, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in value
    ):
        raise InvalidTemplateRulesError(detail={key: "Expected a list of ids."})
    return list(dict.fromkeys(value))


def _validate_rules(payload: dict[str, Any], *, workspace: Workspace) -> dict[str, Any]:
    """S18's part of a payload: every id resolved inside this workspace, so a
    template can never name another tenant's product or account."""
    from channels.models import SocialAccount
    from products.models import Product

    rules: dict[str, Any] = {}
    for key, model in (("product_ids", Product), ("social_account_ids", SocialAccount)):
        wanted = _ids(payload, key)
        found = set(
            model.objects.filter(workspace=workspace, pk__in=wanted).values_list("pk", flat=True)
        )
        if missing := [i for i in wanted if i not in found]:
            raise InvalidTemplateRulesError(detail={key: missing})
        rules[key] = wanted
    for key, modes in (("image_mode", IMAGE_MODES), ("text_mode", TEXT_MODES)):
        mode = payload.get(key) or next(iter(modes))
        if mode not in modes:
            raise InvalidTemplateRulesError(detail={key: mode, "allowed": list(modes)})
        rules[key] = mode
    used = placeholders_in(str(payload.get("master_body") or ""))
    if unknown := sorted(used - PLACEHOLDERS.keys()):
        raise UnknownPlaceholderError(detail={"placeholders": unknown, "known": list(PLACEHOLDERS)})
    if used and not rules["product_ids"]:
        raise PlaceholderNeedsProductError(detail={"placeholders": sorted(used)})
    # Defaults are not stored, so a template saved with none of these stays
    # byte-identical and an unchanged save is not a new version.
    defaults = {
        "product_ids": [],
        "social_account_ids": [],
        "image_mode": next(iter(IMAGE_MODES)),
        "text_mode": next(iter(TEXT_MODES)),
    }
    return {key: value for key, value in rules.items() if value != defaults[key]}


def validate_payload(
    payload: dict[str, Any], *, workspace: Workspace | None = None
) -> dict[str, Any]:
    """Returns the payload with its options cleaned, or raises. With a
    `workspace` whose organization has S18 on, its products, accounts and
    modes are validated and kept; otherwise they are dropped, as before."""
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

    cleaned_payload = {
        "master_body": str(payload.get("master_body") or ""),
        "media_asset_ids": media_ids,
        "platform_options": cleaned,
    }
    if workspace is not None and rules_enabled(workspace):
        cleaned_payload |= _validate_rules(payload, workspace=workspace)
    return cleaned_payload


def rules_enabled(workspace: Workspace) -> bool:
    from billing.services.flags import TEMPLATE_RULES_S18, flag_enabled

    return flag_enabled(workspace.organization, TEMPLATE_RULES_S18)


def template_products(template: PostTemplate) -> list[Product]:
    """The template's products that still exist in its workspace, in order."""
    from products.models import Product

    wanted = [i for i in (template.payload or {}).get("product_ids") or [] if isinstance(i, int)]
    found = {p.pk: p for p in Product.objects.filter(workspace=template.workspace, pk__in=wanted)}
    return [found[i] for i in wanted if i in found]


def ensure_creatable(content_kind: str) -> None:
    if content_kind not in CREATABLE_KINDS:
        raise ContentKindNotAvailableError(detail={"content_kind": content_kind})


@transaction.atomic
def apply_template(
    template: PostTemplate,
    *,
    author: User,
    product: Product | None = None,
    body: str | None = None,
    media: list[MediaAsset] | None = None,
) -> Post:
    """Copies the template into a new draft post.

    S18: with a `product`, the post is for it and its placeholders are filled
    from it; `body` and `media` replace the template's own text and media
    (a slot's generated or product-photo version). The product is only read.

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
    assets = [found[i] for i in wanted if i in found] if media is None else media
    text = (
        body
        if body is not None
        else fill_placeholders(str(payload.get("master_body") or ""), product)
    )

    post = create_post(
        workspace=template.workspace,
        author=author,
        master_body=text,
        media_assets=assets,
    )
    post.template = template
    post.template_version = template.version
    post.product = product
    post.save(update_fields=["template", "template_version", "product"])

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
