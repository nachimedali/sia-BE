"""Reading one product page into a new-product brief (S1, the product half).

Sources, most structured first — the first one that states a field wins it:

1. Shopify's public product JSON (`/products/<handle>.json`),
2. WooCommerce's Store API (`/wp-json/wc/store/v1/products?slug=<slug>`),
3. JSON-LD `Product` on the page,
4. OpenGraph `product` tags,
5. the page's own markup (`<h1>`, meta description, product-looking images).

**Only what a source states is returned.** A field nobody states is absent —
never a category guessed from the title, never a price in a currency the page
did not show. Claims are matched from the catalogue's own phrases
(`CreativeOption.metadata.phrases`, rule 18) and come back with the sentence
that contains them, so the user can see why each was proposed; a claim found on
a page is still only a claim until the user attaches its proof.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from brand.services.extract import Page, _found, _ld_nodes, _price, _types, parse_page, woo_price

SHORT_MAX = 160
LONG_MAX = 1200
MAX_IMAGES = 8
MAX_FEATURES = 8

#: Image hints that are page furniture, not the product.
_NOT_PRODUCT = re.compile(r"logo|icon|sprite|badge|avatar|payment|flag|banner|placeholder", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _html_text(html: str, base: str) -> Page:
    """A fragment of product HTML (Shopify's `body_html`) parsed like a page,
    so its paragraphs and bullet lists are read the same way."""
    return parse_page(base, f"<html><body>{html}</body></html>")


def short_from(text: str) -> str:
    """Whole sentences up to `SHORT_MAX` — never a word cut in half. A first
    sentence longer than that is cut at a word boundary instead."""
    out = ""
    for sentence in _SENTENCE.split(_clean(text)):
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > SHORT_MAX:
            break
        out = candidate
    if not out and text:
        out = _clean(text)[:SHORT_MAX].rsplit(" ", 1)[0]
    return out


def _features(items: list[str], navigation: set[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        text = _clean(item)
        if not (2 < len(text) <= 80) or text.casefold() in navigation:
            continue
        if text not in out:
            out.append(text)
    return out[:MAX_FEATURES]


def _images(urls: list[str], base: str) -> list[str]:
    out: list[str] = []
    for raw in urls:
        if not raw or raw.startswith("data:"):
            continue
        url = urljoin(base, raw.strip())
        if urlsplit(url).scheme not in {"http", "https"} or url.lower().endswith(".svg"):
            continue
        if url not in out:
            out.append(url)
    return out[:MAX_IMAGES]


def _ld_image_urls(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in values:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            out.append(item["url"])
    return out


def claims_in(texts: list[str], claim_rows: list[tuple[str, list[str]]]) -> list[dict[str, str]]:
    """`[{key, quote}]` for each catalogue claim whose phrase appears in a text.
    The quote is the sentence that holds the phrase, verbatim. Sentences are
    split and folded once, however many claims are matched against them."""
    sentences = [
        (sentence, sentence.casefold())
        for text in texts
        for sentence in _SENTENCE.split(_clean(text))
    ]
    found: list[dict[str, str]] = []
    for key, phrases in claim_rows:
        folded = [phrase.casefold() for phrase in phrases]
        hit = next((s for s, low in sentences if any(p in low for p in folded)), None)
        if hit:
            found.append({"key": key, "quote": hit[:SHORT_MAX]})
    return found


def _structured(
    fields: dict[str, Any], body_html: str, image_srcs: list[Any], base: str
) -> dict[str, Any]:
    """One shop API's product row in the brief's shape, so Shopify and
    WooCommerce fill the form through the same steps."""
    body = _html_text(body_html, base)
    description = " ".join(body.blocks)
    return {
        **fields,
        "short": short_from(description),
        "long": description[:LONG_MAX],
        "features": _features(body.list_items, set()),
        "images": _images(
            [str(i.get("src") or "") for i in image_srcs if isinstance(i, dict)], base
        ),
        "_text": description,
    }


def extract_product(
    page: Page,
    *,
    shopify: tuple[str, Any] | None = None,
    woocommerce: tuple[str, Any] | None = None,
    claim_rows: list[tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """The brief fields this page states, each `{"value", "source"}`.
    `shopify` / `woocommerce` are `(url read, payload)` from the shop's API."""
    url = page.url
    out: dict[str, Any] = {}

    def put(field: str, value: Any, source: str) -> None:
        if field in out or value in (None, "", [], {}):
            return
        out[field] = _found(value, source)

    via = ""  # the first structured source that described the product
    texts: list[str] = []
    navigation = {_clean(text).casefold() for _, text in page.anchors if text}

    # 1-2 · the shop's own product data: Shopify's product JSON, then
    # WooCommerce's Store API — the same fields, read the same way.
    shops: list[tuple[str, str, dict[str, Any]]] = []
    if shopify and isinstance((product := (shopify[1] or {}).get("product")), dict):
        variants = product.get("variants") or [{}]
        variant = variants[0] if isinstance(variants[0], dict) else {}
        price = _price(variant["price"]) if variant.get("price") not in (None, "") else None
        fields = {
            "name": product.get("title"),
            "brand": product.get("vendor"),
            "sku": variant.get("sku"),
            "price": price,
        }
        shops.append(
            (
                "shopify",
                shopify[0],
                _structured(
                    fields, str(product.get("body_html") or ""), product.get("images") or [], url
                ),
            )
        )
    if (
        woocommerce
        and isinstance(woocommerce[1], list)
        and woocommerce[1]
        and isinstance(woocommerce[1][0], dict)
    ):
        row = woocommerce[1][0]
        fields = {
            "name": row.get("name"),
            "sku": row.get("sku"),
            "price": woo_price(row.get("prices") or {}),
        }
        html = str(row.get("description") or row.get("short_description") or "")
        shops.append(
            (
                "woocommerce",
                str(row.get("permalink") or woocommerce[0]),
                _structured(fields, html, row.get("images") or [], url),
            )
        )
    for kind, source, data in shops:
        via = via or kind
        put("name", _clean(data["name"])[:80], source)
        put("brand", _clean(data.get("brand"))[:80], source)
        put("sku", _clean(data["sku"])[:64], source)
        put("price", data["price"], source)
        for field in ("short", "long", "features", "images"):
            put(field, data[field], source)
        texts.append(data["_text"])

    # 3 · JSON-LD Product
    for node in _ld_nodes(page):
        if "Product" not in _types(node):
            continue
        via = via or "json-ld"
        offers = node.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        description = _clean(node.get("description"))
        brand = node.get("brand")
        put("name", _clean(node.get("name"))[:80], url)
        put("brand", _clean(brand.get("name") if isinstance(brand, dict) else brand)[:80], url)
        put("sku", _clean(node.get("sku"))[:64], url)
        if isinstance(offer, dict) and (offer.get("price") or offer.get("lowPrice")) is not None:
            put(
                "price",
                _price(offer.get("price") or offer.get("lowPrice"), offer.get("priceCurrency", "")),
                url,
            )
        put("short", short_from(description), url)
        put("long", description[:LONG_MAX], url)
        put("images", _images(_ld_image_urls(node.get("image")), url), url)
        texts.append(description)
        break

    # 4 · OpenGraph product tags
    metas = page.metas
    if metas.get("og:type", "").lower() == "product" or "product:price:amount" in metas:
        via = via or "opengraph"
        put("name", _clean(metas.get("og:title"))[:80], url)
        amount = metas.get("product:price:amount") or metas.get("og:price:amount")
        if amount:
            currency = metas.get("product:price:currency") or metas.get("og:price:currency", "")
            put("price", _price(amount, currency), url)
        put("short", short_from(metas.get("og:description", "")), url)
        put("images", _images([metas.get("og:image", "")], url), url)
        texts.append(metas.get("og:description", ""))

    # 5 · the page's own markup
    put("name", (page.headings[0] if page.headings else "")[:80], url)
    put("short", short_from(metas.get("description", "")), url)
    put(
        "images",
        _images([src for src, hint in page.images if not _NOT_PRODUCT.search(hint)], url),
        url,
    )
    put("features", _features(page.list_items, navigation), url)
    texts.extend([metas.get("description", ""), *page.blocks])

    # Shopify's product JSON states no currency; the page's own tags may. A
    # price no source gave a currency for keeps it blank — the form says so
    # rather than assuming the workspace's.
    price = out.get("price", {}).get("value")
    stated = metas.get("product:price:currency") or metas.get("og:price:currency", "")
    if price and not price.get("currency") and stated:
        price["currency"] = stated.upper()[:3]

    out["via"] = via or "page"
    lang = page.lang.split("-")[0].lower()
    if lang:
        out["language"] = _found(lang, url)
    put("url", url, url)

    claims = claims_in([t for t in texts if t], claim_rows or [])
    if claims:
        out["claims"] = _found(claims, url)
    return out
