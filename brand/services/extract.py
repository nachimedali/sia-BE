"""What a website says about the brand, read from its pages (S1-03).

**Deterministic first.** Everything a page *states* — its name, description,
logo, social links, languages, products and prices, colours and fonts — is read
from markup with stdlib parsing, not inferred: `og:site_name`, JSON-LD
`Organization` and `Product`, Shopify's and WooCommerce's public product
endpoints, OpenGraph product tags, `theme-color`, CSS. A value that is not on
the page is **absent**, never filled with something plausible.

What a page only *implies* — who the audience is, how the voice sounds, which
category it belongs to, who the competitors are — is left to
`brand.inference`, and labelled as inferred there.

Every extracted value carries the URL it was read from.
"""

from __future__ import annotations

import colorsys
import contextlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from content.models import Platform

#: Product and price caps: enough to seed a brand kit, not a catalog mirror.
MAX_PRODUCTS = 12
MAX_PALETTE = 5

_SKIP_TEXT = {"script", "style", "noscript", "svg", "template", "head"}
_BLOCK = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "li",
    "blockquote",
    "figcaption",
    "td",
    "dd",
    "div",
    "section",
}


@dataclass
class Page:
    """One parsed page. Everything the extractors read, nothing more."""

    url: str
    title: str = ""
    lang: str = ""
    metas: dict[str, str] = field(default_factory=dict)
    anchors: list[tuple[str, str]] = field(default_factory=list)  # (absolute href, text)
    stylesheets: list[str] = field(default_factory=list)
    icons: list[tuple[str, str]] = field(default_factory=list)  # (rel, absolute href)
    hreflangs: list[str] = field(default_factory=list)
    jsonld: list[Any] = field(default_factory=list)
    css: list[str] = field(default_factory=list)  # <style> blocks and style="" attributes
    images: list[tuple[str, str]] = field(default_factory=list)  # (absolute src, hint text)
    blocks: list[str] = field(default_factory=list)  # visible text, one entry per block
    headings: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)  # visible `<li>` text, in order


class _Parser(HTMLParser):
    def __init__(self, page: Page) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self._skip = 0
        self._in_title = False
        self._in_jsonld = False
        self._in_style = False
        self._jsonld_buf: list[str] = []
        self._style_buf: list[str] = []
        self._buf: list[str] = []
        self._anchor: str | None = None
        self._anchor_text: list[str] = []
        self._heading = False
        self._in_li = False

    def _abs(self, href: str) -> str:
        return urljoin(self.page.url, href.strip())

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.page.blocks.append(text)
            if self._heading:
                self.page.headings.append(text)
            if self._in_li:
                self.page.list_items.append(text)
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.page.lang = a["lang"]
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a and key not in self.page.metas:
                self.page.metas[key] = a["content"].strip()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if not href:
                pass
            elif "stylesheet" in rel:
                self.page.stylesheets.append(self._abs(href))
            elif "icon" in rel:
                self.page.icons.append((rel, self._abs(href)))
            elif "alternate" in rel and a.get("hreflang"):
                self.page.hreflangs.append(a["hreflang"])
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []
        elif tag == "style":
            self._in_style = True
            self._style_buf = []
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self._anchor = self._abs(a["href"])
            self._anchor_text = []
        elif tag == "img" and a.get("src"):
            hint = " ".join(
                [a.get("alt", ""), a.get("class", ""), a.get("id", ""), a.get("src", "")]
            )
            self.page.images.append((self._abs(a["src"]), hint.lower()))
        if a.get("style"):
            self.page.css.append(a["style"])
        if tag in _SKIP_TEXT and tag != "head":
            self._skip += 1
        if tag in _BLOCK or tag == "br":
            self._flush()
            self._heading = tag in {"h1", "h2", "h3"}
            self._in_li = tag == "li"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_buf).strip()
            with contextlib.suppress(ValueError, TypeError):
                self.page.jsonld.append(json.loads(raw))
        if tag == "style" and self._in_style:
            self._in_style = False
            self.page.css.append("".join(self._style_buf))
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._anchor is not None:
            self.page.anchors.append(
                (self._anchor, re.sub(r"\s+", " ", "".join(self._anchor_text)).strip())
            )
            self._anchor = None
        if tag in _SKIP_TEXT and tag != "head" and self._skip:
            self._skip -= 1
        if tag in _BLOCK:
            self._flush()
            self._heading = False
            self._in_li = False

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_style:
            self._style_buf.append(data)
            return
        if self._in_title:
            self.page.title += data
            return
        if self._skip:
            return
        self._buf.append(data)
        if self._anchor is not None:
            self._anchor_text.append(data)


def parse_page(url: str, html: str) -> Page:
    page = Page(url=url)
    parser = _Parser(page)
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        pass  # malformed markup: keep what was read before it broke
    parser._flush()
    page.title = re.sub(r"\s+", " ", page.title).strip()
    return page


# --- JSON-LD helpers -------------------------------------------------------------


def _ld_nodes(page: Page) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, `@graph` flattened."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            out.append(node)
            if "@graph" in node:
                walk(node["@graph"])
            if node.get("@type") == "ItemList":
                for element in node.get("itemListElement") or []:
                    if isinstance(element, dict):
                        walk(element.get("item") or element)

    for block in page.jsonld:
        walk(block)
    return out


def _types(node: dict[str, Any]) -> set[str]:
    kind = node.get("@type", "")
    return {kind} if isinstance(kind, str) else {k for k in kind if isinstance(k, str)}


_ORG_TYPES = {
    "Organization",
    "Corporation",
    "LocalBusiness",
    "Store",
    "OnlineStore",
    "Brand",
    "Restaurant",
    "CafeOrCoffeeShop",
}


def _org(pages: list[Page]) -> tuple[dict[str, Any], str] | None:
    for page in pages:
        for node in _ld_nodes(page):
            if _types(node) & _ORG_TYPES:
                return node, page.url
    return None


# --- identity --------------------------------------------------------------------

_TITLE_SPLIT = re.compile(r"\s+[|–—·•:-]\s+")  # noqa: RUF001 — title separators


def _found(value: Any, source: str, confidence: str = "high") -> dict[str, Any]:
    return {"value": value, "source": source, "confidence": confidence}


_SOCIAL_HOSTS: dict[str, str] = {
    "instagram.com": Platform.INSTAGRAM,
    "tiktok.com": Platform.TIKTOK,
    "linkedin.com": Platform.LINKEDIN,
    "facebook.com": Platform.FACEBOOK,
    "fb.com": Platform.FACEBOOK,
    "youtube.com": Platform.YOUTUBE,
    "youtu.be": Platform.YOUTUBE,
    "x.com": Platform.X,
    "twitter.com": Platform.X,
    "pinterest.com": Platform.PINTEREST,
    "threads.net": Platform.THREADS,
}

#: Country → the IANA zone most of its businesses schedule in. Only countries
#: with a single zone or one dominant commercial zone; a country absent from
#: here leaves the timezone unknown rather than guessed.
COUNTRY_TIMEZONES: dict[str, str] = {
    "TN": "Africa/Tunis",
    "MA": "Africa/Casablanca",
    "DZ": "Africa/Algiers",
    "EG": "Africa/Cairo",
    "FR": "Europe/Paris",
    "BE": "Europe/Brussels",
    "CH": "Europe/Zurich",
    "LU": "Europe/Luxembourg",
    "DE": "Europe/Berlin",
    "AT": "Europe/Vienna",
    "NL": "Europe/Amsterdam",
    "IT": "Europe/Rome",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "SE": "Europe/Stockholm",
    "DK": "Europe/Copenhagen",
    "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki",
    "PL": "Europe/Warsaw",
    "GR": "Europe/Athens",
    "TR": "Europe/Istanbul",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "QA": "Asia/Qatar",
    "SN": "Africa/Dakar",
    "CI": "Africa/Abidjan",
}
_TLD_COUNTRY = {"uk": "GB", "co.uk": "GB"}

_COUNTRY_NAMES = {
    "tunisia": "TN",
    "tunisie": "TN",
    "morocco": "MA",
    "maroc": "MA",
    "algeria": "DZ",
    "france": "FR",
    "belgium": "BE",
    "belgique": "BE",
    "switzerland": "CH",
    "suisse": "CH",
    "germany": "DE",
    "italy": "IT",
    "spain": "ES",
    "portugal": "PT",
    "united kingdom": "GB",
    "uk": "GB",
}


def _country(pages: list[Page], domain: str) -> tuple[str, str, str] | None:
    """(ISO code, source, confidence). JSON-LD address first; the domain's
    country-code TLD as a low-confidence fallback; a generic TLD gives none."""
    found = _org(pages)
    if found:
        node, source = found
        address = node.get("address")
        addresses = address if isinstance(address, list) else [address]
        for item in addresses:
            if isinstance(item, dict):
                raw = item.get("addressCountry")
                if isinstance(raw, dict):
                    raw = raw.get("name") or raw.get("@id")
                if isinstance(raw, str) and raw.strip():
                    code = raw.strip()
                    code = code.upper() if len(code) == 2 else _COUNTRY_NAMES.get(code.lower(), "")
                    if code:
                        return code, source, "high"
    parts = domain.lower().split(".")
    tld = ".".join(parts[-2:]) if ".".join(parts[-2:]) in _TLD_COUNTRY else parts[-1]
    code = _TLD_COUNTRY.get(tld, tld.upper() if len(tld) == 2 else "")
    if code and code in COUNTRY_TIMEZONES:
        return code, f"https://{domain}/", "low"
    return None


def extract_identity(pages: list[Page], domain: str) -> dict[str, Any]:
    home = pages[0]
    out: dict[str, Any] = {}
    org = _org(pages)

    name = home.metas.get("og:site_name") or ""
    name_source = home.url
    if not name and org and isinstance(org[0].get("name"), str):
        name, name_source = org[0]["name"], org[1]
    if not name and home.title:
        name = _TITLE_SPLIT.split(home.title)[0]
    if name.strip():
        out["name"] = _found(
            name.strip()[:120],
            name_source,
            "high" if home.metas.get("og:site_name") or org else "medium",
        )

    description = home.metas.get("description") or home.metas.get("og:description") or ""
    if not description and org and isinstance(org[0].get("description"), str):
        description = org[0]["description"]
    if not description:
        description = next((b for b in home.blocks if 60 <= len(b) <= 400), "")
    if description.strip():
        out["description"] = _found(
            description.strip()[:400],
            home.url,
            "high" if home.metas.get("description") else "medium",
        )

    logo = ""
    if org:
        raw = org[0].get("logo")
        if isinstance(raw, dict):
            raw = raw.get("url")
        if isinstance(raw, str):
            logo = urljoin(org[1], raw)
    if not logo:
        logo = next((src for src, hint in home.images if "logo" in hint), "")
    if not logo:
        logo = next((href for rel, href in home.icons if "apple-touch-icon" in rel), "")
    if logo:
        out["logo_url"] = _found(logo, home.url, "medium")

    socials: dict[str, str] = {}
    for page in pages:
        for href, _text in page.anchors:
            host = (urlsplit(href).hostname or "").removeprefix("www.").removeprefix("m.")
            platform = _SOCIAL_HOSTS.get(host)
            path = urlsplit(href).path.strip("/")
            if (
                platform
                and path
                and not path.startswith(("share", "sharer", "intent"))
                and platform not in socials
            ):
                socials[platform] = href
    if org:
        for href in org[0].get("sameAs") or []:
            if isinstance(href, str):
                host = (urlsplit(href).hostname or "").removeprefix("www.")
                platform = _SOCIAL_HOSTS.get(host)
                if platform and platform not in socials:
                    socials[platform] = href
    if socials:
        out["socials"] = _found([{"platform": p, "url": u} for p, u in socials.items()], home.url)

    languages: list[str] = []
    for code in [home.lang, *home.hreflangs]:
        base = code.split("-")[0].strip().upper()
        if base and base != "X" and base not in languages and len(base) == 2:
            languages.append(base)
    if languages:
        out["languages"] = _found(languages, home.url)

    country = _country(pages, domain)
    if country:
        code, source, confidence = country
        out["country"] = _found(code, source, confidence)
        if code in COUNTRY_TIMEZONES:
            out["timezone"] = _found(COUNTRY_TIMEZONES[code], source, confidence)
    return out


# --- products ----------------------------------------------------------------------


def _price(amount: Any, currency: Any = "") -> dict[str, str] | None:
    try:
        value = Decimal(str(amount).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    return {"amount": format(value.normalize(), "f"), "currency": str(currency or "").upper()[:3]}


def _product(
    name: Any, url: str, source: str, *, price: dict[str, str] | None = None, image: str = ""
) -> dict[str, Any] | None:
    if not isinstance(name, str) or not name.strip():
        return None
    item: dict[str, Any] = {
        "name": re.sub(r"\s+", " ", name).strip()[:160],
        "url": url,
        "source": source,
    }
    if price:
        item["price"] = price
    if image:
        item["image"] = image
    return item


def products_from_shopify(base_url: str, payload: Any, currency: str = "") -> list[dict[str, Any]]:
    """Shopify's public `/products.json`: titles, handles, first variant price."""
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    source = urljoin(base_url, "/products.json")
    for row in payload.get("products") or []:
        if not isinstance(row, dict):
            continue
        variants = row.get("variants") or [{}]
        images = row.get("images") or [{}]
        item = _product(
            row.get("title"),
            urljoin(base_url, f"/products/{row.get('handle', '')}"),
            source,
            price=_price(variants[0].get("price"), currency) if variants[0].get("price") else None,
            image=str(images[0].get("src") or "") if images else "",
        )
        if item:
            out.append(item)
    return out


def woo_price(prices: dict[str, Any]) -> dict[str, str] | None:
    """A WooCommerce Store API price: minor units with `currency_minor_unit`."""
    if prices.get("price") in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        amount = Decimal(str(prices["price"])) / (Decimal(10) ** minor)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return _price(amount, prices.get("currency_code", ""))


def products_from_woocommerce(base_url: str, payload: Any) -> list[dict[str, Any]]:
    """WooCommerce Store API (`/wp-json/wc/store/v1/products`). Prices come in
    minor units with `currency_minor_unit`."""
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    source = urljoin(base_url, "/wp-json/wc/store/v1/products")
    for row in payload:
        if not isinstance(row, dict):
            continue
        price = woo_price(row.get("prices") or {})
        images = row.get("images") or [{}]
        item = _product(
            row.get("name"),
            str(row.get("permalink") or base_url),
            source,
            price=price,
            image=str(images[0].get("src") or "") if images else "",
        )
        if item:
            out.append(item)
    return out


def products_from_pages(pages: list[Page]) -> list[dict[str, Any]]:
    """JSON-LD `Product` and OpenGraph `product` pages."""
    out: list[dict[str, Any]] = []
    for page in pages:
        for node in _ld_nodes(page):
            if "Product" not in _types(node):
                continue
            offers = node.get("offers")
            offer = offers[0] if isinstance(offers, list) and offers else offers
            price = None
            if isinstance(offer, dict):
                amount = offer.get("price") or offer.get("lowPrice")
                if amount is not None:
                    price = _price(amount, offer.get("priceCurrency", ""))
            image = node.get("image")
            image = image[0] if isinstance(image, list) and image else image
            item = _product(
                node.get("name"),
                urljoin(
                    page.url,
                    str(
                        node.get("url")
                        or (offer.get("url") if isinstance(offer, dict) else "")
                        or page.url
                    ),
                ),
                page.url,
                price=price,
                image=urljoin(page.url, image) if isinstance(image, str) else "",
            )
            if item:
                out.append(item)
        if page.metas.get("og:type", "").lower() == "product":
            amount = page.metas.get("product:price:amount") or page.metas.get("og:price:amount")
            item = _product(
                page.metas.get("og:title") or page.title,
                page.url,
                page.url,
                price=_price(
                    amount,
                    page.metas.get("product:price:currency")
                    or page.metas.get("og:price:currency", ""),
                )
                if amount
                else None,
                image=page.metas.get("og:image", ""),
            )
            if item:
                out.append(item)
    return out


def merge_products(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First source wins per product; deduplicated by URL then by name."""
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for item in group:
            key_url = item["url"].rstrip("/")
            key_name = item["name"].casefold()
            if key_url in seen_urls or key_name in seen_names:
                continue
            seen_urls.add(key_url)
            seen_names.add(key_name)
            out.append(item)
            if len(out) >= MAX_PRODUCTS:
                return out
    return out


# --- palette and fonts -------------------------------------------------------------

_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})(?:[\s,/]+([\d.]+%?))?\s*\)")
_CUSTOM_PROP = re.compile(r"(--[\w-]+)\s*:\s*([^;}{]+)")
_DECL = re.compile(
    r"(?:^|[;{\s])(color|background|background-color|border-color|fill|stroke|accent-color)\s*:\s*([^;}{]+)",
    re.I,
)


def _norm_hex(raw: str) -> str:
    raw = raw.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    return "#" + raw.upper()


def _colors_in(value: str) -> list[str]:
    out = [_norm_hex(m.group(0)) for m in _HEX.finditer(value)]
    for m in _RGB.finditer(value):
        alpha = m.group(4)
        if alpha:
            a = float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1)
            if a < 0.5:
                continue
        r, g, b = (min(255, int(x)) for x in m.group(1, 2, 3))
        out.append(f"#{r:02X}{g:02X}{b:02X}")
    return out


def _hls(hex_color: str) -> tuple[float, float, float]:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def extract_palette(
    css_texts: list[str], metas: dict[str, str], source: str
) -> list[dict[str, str]]:
    """Up to five roles — Primary, Accent, Ink, Background, plus one more
    brand colour — from how often a colour is *used*, with declared brand
    variables and `theme-color` weighted up. A role with no evidence is left
    out: a site with no saturated colour gets no "primary", not a made-up one."""
    weight: Counter[str] = Counter()
    for color in _colors_in(metas.get("theme-color", "")):
        weight[color] += 6
    for css in css_texts:
        for name, value in _CUSTOM_PROP.findall(css):
            boost = 4 if re.search(r"primary|brand|accent|main", name, re.I) else 1
            for color in _colors_in(value):
                weight[color] += boost
        for _prop, value in _DECL.findall(css):
            for color in _colors_in(value):
                weight[color] += 1
    if not weight:
        return []

    ranked = [c for c, _ in weight.most_common()]
    saturated = [c for c in ranked if (lambda h: h[2] > 0.25 and 0.12 < h[1] < 0.88)(_hls(c))]
    darks = [c for c in ranked if _hls(c)[1] < 0.22]
    lights = [c for c in ranked if _hls(c)[1] > 0.88]

    roles: list[tuple[str, str]] = []
    if saturated:
        roles.append(("Primary", saturated[0]))
        primary_hue = _hls(saturated[0])[0]
        distinct = [
            c
            for c in saturated[1:]
            if min(abs(_hls(c)[0] - primary_hue), 1 - abs(_hls(c)[0] - primary_hue)) > 0.08
        ]
        if distinct:
            roles.append(("Accent", distinct[0]))
            if len(distinct) > 1:
                roles.append(("Highlight", distinct[1]))
    if darks:
        roles.append(("Ink", darks[0]))
    if lights:
        roles.append(("Background", lights[0]))
    order = {"Primary": 0, "Accent": 1, "Ink": 2, "Background": 3, "Highlight": 4}
    roles.sort(key=lambda r: order[r[0]])
    return [
        {"role": role, "hex": hex_color, "source": source}
        for role, hex_color in roles[:MAX_PALETTE]
    ]


_GENERIC_FONTS = {
    "serif",
    "sans-serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "ui-sans-serif",
    "ui-serif",
    "ui-monospace",
    "-apple-system",
    "blinkmacsystemfont",
    "inherit",
    "initial",
    "unset",
    "emoji",
    "segoe ui",
    "helvetica",
    "helvetica neue",
    "arial",
    "roboto",
    "sans",
    "apple color emoji",
}
_FONT_DECL = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def _first_family(value: str) -> str:
    for part in value.split(","):
        name = part.strip().strip("'\"").strip()
        if name and not name.startswith("var(") and name.lower() not in _GENERIC_FONTS:
            return name
    return ""


def extract_fonts(
    css_texts: list[str], stylesheet_urls: list[str], source: str
) -> dict[str, dict[str, str]]:
    """Display and body families. Google Fonts URLs name them outright; CSS
    rules for headings and body say which is which."""
    google: list[str] = []
    for url in stylesheet_urls:
        if "fonts.googleapis.com" in url:
            for family in parse_qs(urlsplit(url).query).get("family", []):
                name = family.split(":")[0].replace("+", " ").strip()
                if name and name not in google:
                    google.append(name)
    display = body = ""
    for css in css_texts:
        for selector, block in _FONT_DECL.findall(css):
            match = re.search(r"font-family\s*:\s*([^;]+)", block)
            if not match:
                continue
            family = _first_family(match.group(1))
            if not family:
                continue
            sel = selector.lower()
            if not display and re.search(r"\bh1\b|\bh2\b|heading|title|display|hero", sel):
                display = family
            elif not body and re.search(r"\bbody\b|\bhtml\b|\bp\b|:root", sel):
                body = family
        for match in re.finditer(r"--[\w-]*(?:font|family)[\w-]*\s*:\s*([^;}{]+)", css):
            family = _first_family(match.group(1))
            if family and family not in google:
                google.append(family)
    display = display or (google[0] if google else "")
    body = body or (google[1] if len(google) > 1 else (google[0] if google else ""))
    out: dict[str, dict[str, str]] = {}
    if display:
        out["display"] = {"name": display, "source": source}
    if body:
        out["body"] = {"name": body, "source": source}
    return out


# --- text ------------------------------------------------------------------------


def sentences(pages: list[Page]) -> list[tuple[str, str]]:
    """(sentence, url) pairs of readable prose, nav and button text excluded."""
    out: list[tuple[str, str]] = []
    for page in pages:
        for block in page.blocks:
            for sentence in re.split(r"(?<=[.!?])\s+", block):
                words = sentence.split()
                if 6 <= len(words) <= 32 and sentence[-1:] in ".!?":
                    out.append((sentence.strip(), page.url))
    return out


def site_text(pages: list[Page], limit: int = 6000) -> str:
    """The prose the inference reads, page by page, capped."""
    chunks: list[str] = []
    size = 0
    for page in pages:
        for block in page.blocks:
            if len(block) < 25:
                continue
            chunks.append(block)
            size += len(block)
            if size > limit:
                return "\n".join(chunks)
    return "\n".join(chunks)
