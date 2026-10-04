"""The website import, end to end (S1-01…S1-05).

    start_import  → a QUEUED row, then `brand.tasks.run_brand_import` on `ai_q`
    run_import    → read the site, stage by stage, into `BrandImport.result`
    review        → accept or edit one section at a time
    apply         → a new `BrandCore` version, and the wizard's fields pre-filled
    skip          → "I'll fill it in myself", recorded

**Nothing the import reads is used until a person has reviewed it** — the same
rule as everything else in this product that a machine proposes (L-2).
**A user's edit outranks a later import**: re-importing keeps every section the
user edited, and says which (`kept`). **Apply never overwrites what the user
already typed into the workspace**; it fills what is still blank or still the
placeholder registration put there.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from brand.fetching import FetchedPage, SiteFetcher, get_site_fetcher
from brand.inference import get_brand_inference
from brand.models import SECTIONS, BrandCore, BrandImport, ImportStage, ImportStatus, ReviewState
from brand.services import extract
from categories.models import Category
from common.exceptions import OCCSError, StateConflict
from content.models import Platform
from workspaces.models import Workspace
from workspaces.services.provisioning import default_workspace_name

#: Pages read per import, home included. Enough for home, shop, about,
#: contact and a few product pages; small enough to be polite and fast.
MAX_PAGES = 10
MAX_STYLESHEETS = 3
#: Links worth reading, by what their path says they are.
_INTERESTING = re.compile(
    r"/(about|a-propos|qui-sommes|our-story|story|notre-histoire|histoire|shop|boutique|store|products?|collections?|catalog|catalogue|contact|faq)(/|$|\.html)",
    re.I,
)
_UNREACHABLE = "We couldn't read that website. Check the address, or fill the details in yourself."
_BROKEN = "Something went wrong reading the website. Try again, or fill the details in yourself."
_PRODUCT_PATH = re.compile(r"/(products?|produits?|shop|boutique|item|p)/[^/]+", re.I)


class InvalidWebsiteError(OCCSError):
    default_code = "invalid_website"
    default_detail = "Enter a website like yourbrand.com."


class ImportRunningError(StateConflict):
    default_code = "import_running"
    default_detail = "An import of this workspace's website is already running."


class ImportNotReadyError(StateConflict):
    default_code = "import_not_ready"
    default_detail = "This import has not finished reading the website."


class ReviewIncompleteError(StateConflict):
    default_code = "review_incomplete"
    default_detail = "Review every section before applying the import."


class InvalidSectionError(OCCSError):
    default_code = "invalid_section"
    default_detail = "That section's value is not valid."


# --- start -------------------------------------------------------------------------


def normalise_url(raw: str) -> tuple[str, str]:
    """`(url, domain)` for what the user typed, or `InvalidWebsiteError`.

    Syntactic only: the real fetcher re-checks, after DNS, that the address is
    public (`brand.fetching.assert_public_url`)."""
    value = (raw or "").strip()
    if not value:
        raise InvalidWebsiteError()
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in {"http", "https"}
        or not re.fullmatch(r"([a-z0-9-]+\.)+[a-z]{2,}", host)
        or host.endswith((".local", ".internal", ".localhost"))
        or parts.username
        or parts.password
    ):
        raise InvalidWebsiteError()
    return f"{parts.scheme}://{host}/", host.removeprefix("www.")


def start_import(workspace: Workspace, *, user: User, url: str) -> BrandImport:
    normalised, domain = normalise_url(url)
    if BrandImport.objects.filter(
        workspace=workspace, status__in=[ImportStatus.QUEUED, ImportStatus.RUNNING]
    ).exists():
        raise ImportRunningError()
    run = BrandImport.objects.create(
        workspace=workspace, created_by=user, url=normalised, domain=domain
    )

    from brand.tasks import run_brand_import

    run_brand_import.delay(run.pk)
    run.refresh_from_db()  # under eager Celery the reading has already happened
    return run


def skip(workspace: Workspace, *, user: User) -> BrandImport:
    """Recorded once; the wizard then treats Import and Review as answered."""
    existing = BrandImport.objects.filter(
        workspace=workspace, status__in=[ImportStatus.SKIPPED, ImportStatus.APPLIED]
    ).first()
    if existing:
        return existing
    return BrandImport.objects.create(
        workspace=workspace,
        created_by=user,
        status=ImportStatus.SKIPPED,
        stage=ImportStage.DONE,
        progress=100,
    )


# --- run ---------------------------------------------------------------------------

_STAGE_PROGRESS = {
    ImportStage.RESOLVING: 5,
    ImportStage.READING: 30,
    ImportStage.PRODUCTS: 50,
    ImportStage.AUDIENCE: 62,
    ImportStage.COMPETITORS: 72,
    ImportStage.VOICE: 82,
    ImportStage.PALETTE: 94,
    ImportStage.DONE: 100,
}


def _advance(run: BrandImport, stage: str, **fields: Any) -> None:
    run.stage = stage
    run.progress = max(run.progress, _STAGE_PROGRESS[ImportStage(stage)])
    for name, value in fields.items():
        setattr(run, name, value)
    run.save(update_fields=["stage", "progress", *fields.keys(), "updated_at"])


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host == domain


def _json(page: FetchedPage | None) -> Any:
    import json

    if page is None or not page.ok or not page.text.strip():
        return None
    try:
        return json.loads(page.text)
    except ValueError:
        return None


def _read(fetcher: SiteFetcher, url: str, read: list[str]) -> FetchedPage | None:
    page = fetcher.fetch(url)
    if page is not None and page.ok:
        read.append(page.url)
    return page


def run_import(run: BrandImport) -> BrandImport:
    """Read the website into `run.result`. Never raises: a failure is a
    FAILED row with a message the user can act on."""
    fetcher = get_site_fetcher()
    read: list[str] = []
    run.status = ImportStatus.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    try:
        _advance(run, ImportStage.RESOLVING)
        home_raw = _read(fetcher, run.url, read)
        if home_raw is None or not home_raw.ok or not home_raw.text.strip():
            return _fail(run, _UNREACHABLE, read)
        domain = (urlsplit(home_raw.url).hostname or run.domain).lower().removeprefix("www.")
        base = f"{urlsplit(home_raw.url).scheme}://{urlsplit(home_raw.url).netloc}/"
        home = extract.parse_page(home_raw.url, home_raw.text)
        pages = [home]

        _advance(run, ImportStage.READING, pages=read)
        links = [href.split("#")[0] for href, _ in home.anchors if _same_site(href, domain)]
        wanted = [u for u in dict.fromkeys(links) if _INTERESTING.search(urlsplit(u).path)]
        for url in wanted[: MAX_PAGES - 1]:
            fetched = _read(fetcher, url, read)
            if fetched is not None and fetched.ok and "html" in fetched.content_type:
                pages.append(extract.parse_page(fetched.url, fetched.text))
        _advance(run, ImportStage.READING, pages=read)

        _advance(run, ImportStage.PRODUCTS)
        identity = extract.extract_identity(pages, domain)
        currency = ""
        shopify = extract.products_from_shopify(
            base, _json(_read(fetcher, urljoin(base, "/products.json?limit=24"), read)), currency
        )
        woo = (
            []
            if shopify
            else extract.products_from_woocommerce(
                base,
                _json(
                    _read(fetcher, urljoin(base, "/wp-json/wc/store/v1/products?per_page=24"), read)
                ),
            )
        )
        on_pages = extract.products_from_pages(pages)
        if not (shopify or woo or on_pages):
            product_links = [
                u for u in dict.fromkeys(links) if _PRODUCT_PATH.search(urlsplit(u).path)
            ]
            for url in product_links[: max(0, MAX_PAGES - len(pages))]:
                fetched = _read(fetcher, url, read)
                if fetched is not None and fetched.ok:
                    pages.append(extract.parse_page(fetched.url, fetched.text))
            on_pages = extract.products_from_pages(pages)
        products = extract.merge_products(shopify, woo, on_pages)
        _advance(run, ImportStage.PRODUCTS, pages=read)

        _advance(run, ImportStage.AUDIENCE)
        categories = list(
            Category.objects.filter(parent__isnull=True, is_active=True).values_list(
                "name", flat=True
            )
        )
        name = (identity.get("name") or {}).get("value", domain)
        text = extract.site_text(pages)
        inferred = get_brand_inference().infer(
            name=name, text=text, categories=categories, products=[p["name"] for p in products]
        )
        _advance(run, ImportStage.COMPETITORS)
        _advance(run, ImportStage.VOICE)

        _advance(run, ImportStage.PALETTE)
        css = [c for page in pages for c in page.css]
        sheets = [u for page in pages for u in page.stylesheets]
        for url in [u for u in dict.fromkeys(sheets) if _same_site(u, domain)][:MAX_STYLESHEETS]:
            fetched = _read(fetcher, url, read)
            if fetched is not None and fetched.ok:
                css.append(fetched.text)
        palette = extract.extract_palette(css, home.metas, base)
        fonts = extract.extract_fonts(css, sheets, base)

        run.result = _assemble(identity, products, inferred, palette, fonts, pages, base)
        run.status = ImportStatus.SUCCEEDED
        run.finished_at = timezone.now()
        _advance(
            run,
            ImportStage.DONE,
            pages=read,
            result=run.result,
            status=run.status,
            finished_at=run.finished_at,
        )
    except Exception:  # any reading failure is a FAILED import, never a 500 in a task
        return _fail(run, _BROKEN, read)
    return run


def _fail(run: BrandImport, message: str, read: list[str]) -> BrandImport:
    run.status = ImportStatus.FAILED
    run.error = message
    run.pages = read
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error", "pages", "finished_at", "updated_at"])
    return run


def _assemble(
    identity: dict[str, Any],
    products: list[dict[str, Any]],
    inferred: dict[str, Any],
    palette: list[dict[str, str]],
    fonts: dict[str, dict[str, str]],
    pages: list[extract.Page],
    base: str,
) -> dict[str, Any]:
    """The result the review screen shows, every section present (possibly
    empty) so the user can fill what the site did not say."""
    voice = dict(inferred.get("voice") or {})
    if "quote" in voice:
        voice["quote_source"] = next(
            (
                url
                for sentence, url in extract.sentences(pages)
                if voice["quote"].casefold() in sentence.casefold()
            ),
            base,
        )
    category = None
    if inferred.get("category"):
        row = Category.objects.filter(parent__isnull=True, name=inferred["category"]).first()
        if row:
            category = {"id": row.pk, "name": row.name}
    return {
        "identity": identity,
        "market": {
            **({"category": category} if category else {}),
            **(
                {"business_type": inferred["business_type"]}
                if inferred.get("business_type")
                else {}
            ),
        },
        "sections": {
            "products": {"items": products},
            "audience": inferred.get("audience") or {"summary": "", "segments": []},
            "competitors": {"items": inferred.get("competitors") or []},
            "voice": voice,
            "palette": {"colors": palette, "fonts": fonts},
        },
        "inferred": ["audience", "competitors", "voice", "market"],
    }


# --- review --------------------------------------------------------------------------


def _clean_section(section: str, value: Any) -> dict[str, Any]:
    """The edited value, in the section's own shape, or `InvalidSectionError`."""
    if not isinstance(value, dict):
        raise InvalidSectionError(detail={"section": section})
    if section == "products":
        items = []
        for row in value.get("items") or []:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                raise InvalidSectionError(
                    "Every product needs a name.", detail={"section": section}
                )
            item: dict[str, Any] = {
                "name": str(row["name"]).strip()[:160],
                "url": str(row.get("url") or "")[:500],
            }
            price = row.get("price")
            if isinstance(price, dict) and price.get("amount") not in (None, ""):
                checked = extract._price(price.get("amount"), price.get("currency", ""))
                if checked is None:
                    raise InvalidSectionError(
                        "A price must be a number.", detail={"section": section}
                    )
                item["price"] = checked
            if row.get("image"):
                item["image"] = str(row["image"])[:500]
            items.append(item)
        return {"items": items[: extract.MAX_PRODUCTS]}
    if section == "audience":
        segments = [str(s).strip()[:40] for s in value.get("segments") or [] if str(s).strip()]
        return {"summary": str(value.get("summary") or "").strip()[:400], "segments": segments[:8]}
    if section == "competitors":
        items = []
        for row in value.get("items") or []:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                raise InvalidSectionError(
                    "Every competitor needs a name.", detail={"section": section}
                )
            items.append(
                {
                    "name": str(row["name"]).strip()[:80],
                    "domain": str(row.get("domain") or "").strip()[:120],
                    "why": str(row.get("why") or "").strip()[:200],
                    "relationship": "you",
                }
            )
        return {"items": items[:8]}
    if section == "voice":
        from workspaces.models import BrandVoice

        preset = value.get("preset")
        if preset and preset not in BrandVoice.values:
            raise InvalidSectionError("Unknown voice.", detail={"section": section})
        out: dict[str, Any] = {
            "descriptors": [
                str(d).strip()[:24] for d in value.get("descriptors") or [] if str(d).strip()
            ][:5]
        }
        if preset:
            out["preset"] = preset
        if value.get("quote"):
            out["quote"] = str(value["quote"]).strip()[:240]
        return out
    if section == "palette":
        colors = []
        for row in value.get("colors") or []:
            if not isinstance(row, dict) or not re.fullmatch(
                r"#[0-9A-Fa-f]{6}", str(row.get("hex") or "")
            ):
                raise InvalidSectionError(
                    "Colours must be hex values like #4A36A0.", detail={"section": section}
                )
            colors.append(
                {"role": str(row.get("role") or "Colour")[:24], "hex": str(row["hex"]).upper()}
            )
        fonts = {
            slot: {"name": str(spec.get("name") or "").strip()[:60]}
            for slot, spec in (value.get("fonts") or {}).items()
            if slot in {"display", "body"}
            and isinstance(spec, dict)
            and str(spec.get("name") or "").strip()
        }
        return {"colors": colors[: extract.MAX_PALETTE], "fonts": fonts}
    raise InvalidSectionError(detail={"section": section})


def review(run: BrandImport, *, section: str, action: str, value: Any = None) -> BrandImport:
    if run.status != ImportStatus.SUCCEEDED:
        raise ImportNotReadyError(detail={"status": run.status})
    if section not in SECTIONS:
        raise InvalidSectionError(detail={"section": section})
    if action == "accept":
        run.review = {**run.review, section: run.review.get(section) or ReviewState.ACCEPTED}
    elif action == "edit":
        run.edits = {**run.edits, section: _clean_section(section, value)}
        run.review = {**run.review, section: ReviewState.EDITED}
    else:
        raise InvalidSectionError("Unknown review action.", detail={"action": action})
    run.save(update_fields=["review", "edits", "updated_at"])
    return run


# --- apply ---------------------------------------------------------------------------


def active_core(workspace: Workspace) -> BrandCore | None:
    return BrandCore.objects.filter(workspace=workspace).order_by("-version").first()


def _fill(workspace: Workspace, run: BrandImport, sections: dict[str, Any]) -> list[str]:
    """Pre-fill the wizard's fields from the import — only those the user has
    not answered. Returns the field names filled."""
    identity = run.result.get("identity") or {}
    market = run.result.get("market") or {}

    def value(key: str) -> Any:
        return (identity.get(key) or {}).get("value")

    filled: list[str] = []

    def put(field: str, new: Any, *, blank: bool) -> None:
        if new in (None, "", []) or not blank:
            return
        setattr(workspace, field, new)
        filled.append(field)

    owner_email = workspace.organization.owner.email if workspace.organization.owner_id else ""
    put(
        "name",
        (value("name") or "")[:120],
        blank=workspace.name in ("", default_workspace_name(owner_email)),
    )
    put("website", run.url, blank=not workspace.website)
    put("description", (value("description") or "")[:280], blank=not workspace.description)
    voice = sections.get("voice", {}).get("value", {})
    # The voice always has a value (the model default), so "blank" means "still
    # in the wizard": after onboarding, the workspace's voice is the user's.
    put("brand_voice_default", voice.get("preset"), blank=not workspace.onboarding_complete)
    category = market.get("category")
    if category and not workspace.category_id:
        workspace.category_id = category["id"]
        filled.append("category")
    put("business_type", market.get("business_type"), blank=not workspace.business_type)
    audience = sections.get("audience", {}).get("value", {})
    put(
        "target_audience",
        (audience.get("summary") or "")[:280],
        blank=not workspace.target_audience,
    )
    put("timezone", value("timezone"), blank=workspace.timezone in ("", "UTC"))
    # Regions are markets (country codes); a site's languages are not markets.
    country = value("country")
    put("regions", [country] if country else [], blank=not workspace.regions)
    socials = [
        row["platform"] for row in value("socials") or [] if row.get("platform") in Platform.values
    ]
    put("platforms", socials, blank=not workspace.platforms)
    if filled:
        workspace.save(update_fields=[*filled, "updated_at"])
    return filled


@transaction.atomic
def apply(run: BrandImport, *, user: User) -> tuple[BrandCore, list[str], list[str]]:
    """`(core, kept, filled)`: the new version, the sections kept from an
    earlier hand edit, and the workspace fields pre-filled."""
    if run.status != ImportStatus.SUCCEEDED:
        raise ImportNotReadyError(detail={"status": run.status})
    missing = [s for s in SECTIONS if s not in run.review]
    if missing:
        raise ReviewIncompleteError(detail={"missing": missing})

    previous = active_core(run.workspace)
    found = run.result.get("sections") or {}
    sections: dict[str, Any] = {}
    kept: list[str] = []
    for section in SECTIONS:
        edited = run.review.get(section) == ReviewState.EDITED
        earlier = (previous.sections.get(section) if previous else None) or {}
        if not edited and earlier.get("origin") == "you":
            sections[section] = earlier
            kept.append(section)
            continue
        sections[section] = {
            "value": run.edits.get(section) if edited else found.get(section, {}),
            "origin": "you" if edited else "site",
            "source": "" if edited else run.url,
        }

    core = BrandCore.objects.create(
        workspace=run.workspace,
        version=(previous.version + 1) if previous else 1,
        source_import=run,
        sections=sections,
        identity=run.result.get("identity") or {},
        created_by=user,
    )
    filled = _fill(run.workspace, run, sections)
    run.status = ImportStatus.APPLIED
    run.save(update_fields=["status", "updated_at"])
    return core, kept, filled
