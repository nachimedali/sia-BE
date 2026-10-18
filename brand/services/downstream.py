"""What applying a reviewed import does beyond writing the kit (S1-04, S1-05).

Four things, each of them a proposal a person has already accepted by finishing
the review, and each one stopping short of overriding what a person did by hand:

* `diff_sections`   the field-level difference between this version and the last;
* `create_product_drafts`  products the site lists that the workspace lacks;
* `propose_taste`   a new `TasteProfile` version from the voice (versioned,
                    never edited — rule 12), activated only if it replaces
                    nothing a person wrote;
* `track_competitors`  verified competitors become competitor trend sources.

None of these raises into the apply: a refused product (plan cap) or a
competitor with no handle is reported back with its reason, because a
half-finished apply that rolled back the kit over a plan limit would lose the
review the user just did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from django.db import transaction

from accounts.models import User
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

MAX_CHANGES = 60
MAX_DRAFTS = 12
MAX_TRACKED = 4
IMPORT_VOICE_SOURCE = "brand_import"


# --- S1-05: the field-level difference ----------------------------------------------------
def _price(row: dict[str, Any]) -> str:
    price = row.get("price")
    if not isinstance(price, dict) or not price.get("amount"):
        return ""
    return f"{price['amount']} {price.get('currency', '')}".strip()


def _flat(section: str, value: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """key → (human label, comparable text). Lists are keyed by identity so a
    reordered list is no change and one added row is one change."""
    out: dict[str, tuple[str, str]] = {}
    if section == "products":
        for row in value.get("items") or []:
            key = (row.get("url") or row.get("name") or "").rstrip("/").casefold()
            out[f"item:{key}"] = (f"Product “{row.get('name', '')}”", "listed")
            if _price(row):
                out[f"price:{key}"] = (f"Price of “{row.get('name', '')}”", _price(row))
        for row in value.get("offers") or []:
            out[f"offer:{row.get('text', '').casefold()}"] = ("Offer", row.get("text", ""))
    elif section == "audience":
        out["summary"] = ("Audience summary", str(value.get("summary") or ""))
        for segment in value.get("segments") or []:
            out[f"segment:{str(segment).casefold()}"] = (f"Audience segment “{segment}”", "listed")
    elif section == "competitors":
        for row in value.get("items") or []:
            out[f"c:{str(row.get('name', '')).casefold()}"] = (
                f"Competitor “{row.get('name', '')}”",
                str(row.get("relationship") or ""),
            )
    elif section == "voice":
        out["preset"] = ("Voice", str(value.get("preset") or ""))
        out["quote"] = ("Voice quote", str(value.get("quote") or ""))
        for word in value.get("descriptors") or []:
            out[f"d:{str(word).casefold()}"] = (f"Voice word “{word}”", "listed")
    elif section == "palette":
        for row in value.get("colors") or []:
            out[f"color:{str(row.get('hex', '')).upper()}"] = (
                f"Colour {str(row.get('hex', '')).upper()}",
                str(row.get("role") or ""),
            )
        for slot, spec in (value.get("fonts") or {}).items():
            if isinstance(spec, dict):
                out[f"font:{slot}"] = (f"{slot.capitalize()} font", str(spec.get("name") or ""))
    return {key: pair for key, pair in out.items() if pair[1] != "" or key.startswith("item:")}


def diff_sections(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    skip: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """What changed, section by section. A section in `skip` (kept from a hand
    edit) did not change, by definition."""
    changes: list[dict[str, str]] = []
    for section in after:
        if section in skip:
            continue
        old = _flat(section, (before.get(section) or {}).get("value") or {})
        new = _flat(section, (after.get(section) or {}).get("value") or {})
        for key in [*new, *[k for k in old if k not in new]]:
            if key in new and key not in old:
                kind, was, now = "added", "", new[key][1]
            elif key in old and key not in new:
                kind, was, now = "removed", old[key][1], ""
            elif old[key][1] != new[key][1]:
                kind, was, now = "changed", old[key][1], new[key][1]
            else:
                continue
            label = (new.get(key) or old[key])[0]
            changes.append(
                {"section": section, "field": label, "kind": kind, "before": was, "after": now}
            )
    return changes[:MAX_CHANGES]


# --- S1-04: product drafts -----------------------------------------------------------------
@dataclass
class Downstream:
    products_created: list[str] = field(default_factory=list)
    products_skipped: list[dict[str, str]] = field(default_factory=list)
    taste_version: int | None = None
    taste_activated: bool = False
    competitors_tracked: list[str] = field(default_factory=list)
    competitors_skipped: list[dict[str, str]] = field(default_factory=list)


def create_product_drafts(
    workspace: Workspace, items: list[dict[str, Any]], out: Downstream
) -> None:
    """A `Product` row for each listed product the workspace does not have yet
    (matched by name, case-insensitively). A product is a draft until it is
    generation-ready, which the completeness score already says — so no new
    status. The plan's product cap is respected: what does not fit is reported."""
    from common.exceptions import OCCSError
    from products.models import Product
    from products.services.products import create_product

    have = {
        n.casefold()
        for n in Product.objects.filter(workspace=workspace).values_list("name", flat=True)
    }
    for row in items[:MAX_DRAFTS]:
        name = str(row.get("name") or "").strip()
        if not name or name.casefold() in have:
            continue
        fields: dict[str, Any] = {"name": name[:200]}
        if workspace.category_id:
            fields["category"] = workspace.category
        try:
            # A savepoint of its own: a refused step must not poison the apply's
            # transaction, which is also writing the kit the user just reviewed.
            with transaction.atomic():
                create_product(workspace=workspace, **fields)
        except OCCSError as error:
            out.products_skipped.append({"name": name, "reason": str(error.detail)[:160]})
            break  # the cap will not move for the next one
        have.add(name.casefold())
        out.products_created.append(name)


# --- S1-04: the taste profile -----------------------------------------------------------------
def propose_taste(workspace: Workspace, voice: dict[str, Any], user: User, out: Downstream) -> None:
    from taste.services import profiles

    if not voice:
        return
    active = profiles.active_profile(workspace)
    tone = ", ".join(voice.get("descriptors") or []) or str(voice.get("preset") or "").lower()
    if not tone:
        return
    carried = dict(active.voice) if active else {}
    # The version created is a proposal in the person's own voice when they have
    # one: it only replaces a profile this import wrote earlier.
    replaces_nothing_written = (
        active is None or (active.voice or {}).get("source") == IMPORT_VOICE_SOURCE
    )
    created = profiles.create_profile(
        workspace=workspace,
        created_by=user,
        voice={**carried, "tone": tone, "source": IMPORT_VOICE_SOURCE},
        hard_constraints=dict(active.hard_constraints) if active else {},
        structural=dict(active.structural) if active else {},
        topic_posture=dict(active.topic_posture) if active else {},
    )
    out.taste_version = created.version
    if replaces_nothing_written:
        profiles.activate(created)
        out.taste_activated = True


# --- S1-04: competitor trend sources ------------------------------------------------------------
def _handle(url: str) -> str:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[-1].lstrip("@") if parts else ""


def track_competitors(workspace: Workspace, items: list[dict[str, Any]], out: Downstream) -> None:
    """Only **verified** competitors, and only through the handle their own
    website shows — an inferred name is a guess, and watching a guess would put
    some stranger's account in the workspace's comparisons."""
    from trends.services import competitors

    wanted = set(workspace.platforms or [])
    for row in items:
        if len(out.competitors_tracked) >= MAX_TRACKED:
            break
        name = str(row.get("name") or "")
        if row.get("relationship") != "verified":
            continue
        socials = [s for s in row.get("socials") or [] if _handle(str(s.get("url") or ""))]
        pick = next(
            (s for s in socials if s.get("platform") in wanted), socials[0] if socials else None
        )
        if pick is None:
            out.competitors_skipped.append(
                {"name": name, "reason": "Its website shows no account to watch."}
            )
            continue
        try:
            with transaction.atomic():
                competitors.track(
                    workspace, platform=pick["platform"], handle=_handle(pick["url"]), label=name
                )
        except Exception as error:  # a plan cap (OCCSError), no category (ValidationError)…
            logger.info("competitor %s not tracked: %s", name, error)
            reason = str(getattr(error, "detail", error))[:160]
            out.competitors_skipped.append({"name": name, "reason": reason})
            continue
        out.competitors_tracked.append(name)
