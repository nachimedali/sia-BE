"""Import a new product from its page (S1, the product half).

    start_product_import → a QUEUED row, then `brand.tasks.run_product_import`
    run_product_import   → read the page (and its shop's JSON) into `result`
    product_image        → one found photo's bytes, for the form's own upload

Nothing here creates a product. The result fills the new-product form, the
user reviews it, and the product is created by the normal create endpoint —
so an import can never bypass the brief's validation or the plan's quota.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.utils import timezone

from accounts.models import User
from ai.models import CreativeOption
from brand.fetching import FetchedImage, get_site_fetcher
from brand.models import ImportStatus, ProductImport, ProductStage
from brand.services.extract import parse_page
from brand.services.imports import InvalidWebsiteError, advance, fail, json_body, url_parts
from brand.services.product_page import extract_product
from common.exceptions import ProviderError
from workspaces.models import Workspace

_UNREACHABLE = "We couldn't read that page. Check the link, or fill the product in yourself."
_NOT_A_PRODUCT = (
    "That page does not look like a product page: no product name or photo was found on it."
)
_BROKEN = "Something went wrong reading that page. Try again, or fill the product in yourself."

_PROGRESS = {
    ProductStage.OPENING: 10,
    ProductStage.DETAILS: 35,
    ProductStage.PHOTOS: 55,
    ProductStage.DESCRIPTION: 75,
    ProductStage.CLAIMS: 90,
    ProductStage.DONE: 100,
}


class InvalidProductUrlError(InvalidWebsiteError):
    default_code = "invalid_product_url"
    default_detail = "Paste the full link to the product page, like yourshop.com/products/…"


class ImageUnavailableError(ProviderError):
    default_code = "image_unavailable"
    default_detail = "That photo could not be read from the product page."


def normalise_product_url(raw: str) -> tuple[str, str]:
    """`(url, domain)` keeping the path and query a product page lives at."""
    parts = url_parts(raw, InvalidProductUrlError)
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    url = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    return url, host.removeprefix("www.")


def start_product_import(workspace: Workspace, *, user: User, url: str) -> ProductImport:
    normalised, domain = normalise_product_url(url)
    run = ProductImport.objects.create(
        workspace=workspace, created_by=user, url=normalised, domain=domain
    )

    from brand.tasks import run_product_import

    run_product_import.delay(run.pk)
    run.refresh_from_db()  # under eager Celery the reading has already happened
    return run


def _claim_rows() -> list[tuple[str, list[str]]]:
    rows = CreativeOption.objects.filter(kind="claim", is_active=True).order_by("sort_order", "id")
    return [
        (row.key, [str(p) for p in (row.metadata or {}).get("phrases") or [] if str(p).strip()])
        for row in rows
    ]


def run_product_import(run: ProductImport) -> ProductImport:
    """Read the page into `run.result`. Never raises: a failure is a FAILED row
    with a message the user can act on."""
    fetcher = get_site_fetcher()
    read: list[str] = []
    run.status = ImportStatus.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    try:
        advance(run, ProductStage.OPENING, _PROGRESS)
        fetched = fetcher.fetch(run.url)
        if fetched is None or not fetched.ok or not fetched.text.strip():
            return fail(run, _UNREACHABLE, read)
        read.append(fetched.url)
        page = parse_page(fetched.url, fetched.text)

        advance(run, ProductStage.DETAILS, _PROGRESS, pages=read)
        path = urlsplit(fetched.url).path.rstrip("/")
        shopify: tuple[str, Any] | None = None
        woocommerce: tuple[str, Any] | None = None
        if "/products/" in f"{path}/" and not path.endswith(".json"):
            shop_url = urljoin(fetched.url, f"{path}.json")
            shop_page = fetcher.fetch(shop_url)
            shop_data = json_body(shop_page)
            if shop_data is not None:
                read.append(shop_url)
                shopify = (shop_url, shop_data)
        if shopify is None and "/product/" in f"{path}/":
            slug = path.rsplit("/", 1)[-1]
            woo_url = urljoin(fetched.url, f"/wp-json/wc/store/v1/products?slug={slug}")
            woo_data = json_body(fetcher.fetch(woo_url))
            if isinstance(woo_data, list) and woo_data:
                read.append(woo_url)
                woocommerce = (woo_url, woo_data)

        # Photos are listed, not downloaded, so no stage waits on them.
        advance(run, ProductStage.DESCRIPTION, _PROGRESS, pages=read)
        result = extract_product(
            page, shopify=shopify, woocommerce=woocommerce, claim_rows=_claim_rows()
        )
        advance(run, ProductStage.CLAIMS, _PROGRESS)
        # A page no product markup describes and that shows no price is not a
        # product page — a home page has a heading and photos too.
        if ("name" not in result and "images" not in result) or (
            result.get("via") == "page" and "price" not in result
        ):
            return fail(run, _NOT_A_PRODUCT, read)

        advance(
            run,
            ProductStage.DONE,
            _PROGRESS,
            pages=read,
            result=result,
            status=ImportStatus.SUCCEEDED,
            finished_at=timezone.now(),
        )
    except Exception:  # any reading failure is a FAILED import, never a 500 in a task
        return fail(run, _BROKEN, read)
    return run


def product_image(run: ProductImport, index: int) -> FetchedImage:
    """One of the photos this import found — only those, so the endpoint can
    never be pointed at an arbitrary address."""
    images = (run.result.get("images") or {}).get("value") or []
    if run.status != ImportStatus.SUCCEEDED or not 0 <= index < len(images):
        from rest_framework.exceptions import NotFound

        raise NotFound
    image = get_site_fetcher().fetch_image(str(images[index]))
    if image is None:
        raise ImageUnavailableError(detail={"url": images[index]})
    return image
