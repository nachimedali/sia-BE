"""Reading a page: what it states is found; what it does not state is absent."""

from __future__ import annotations

from brand.fetching import FakeSiteFetcher
from brand.services import extract


def _page(url: str) -> extract.Page:
    fetched = FakeSiteFetcher().fetch(url)
    assert fetched is not None and fetched.ok, url
    return extract.parse_page(fetched.url, fetched.text)


def _css(path: str) -> str:
    fetched = FakeSiteFetcher().fetch(path)
    assert fetched is not None
    return fetched.text


# --- identity ------------------------------------------------------------------------
def test_identity_is_read_from_og_jsonld_and_links() -> None:
    home = _page("https://djerbaolive.tn/")
    about = _page("https://djerbaolive.tn/pages/about")

    found = extract.extract_identity([home, about], "djerbaolive.tn")

    assert found["name"]["value"] == "Djerba Olive Co."
    assert found["description"]["value"].startswith("Cold-pressed extra virgin olive oil")
    assert found["logo_url"]["value"] == "https://djerbaolive.tn/assets/logo.png"
    # The share button is not the brand's profile.
    assert found["socials"]["value"] == [
        {"platform": "instagram", "url": "https://www.instagram.com/djerba.olive"},
        {"platform": "linkedin", "url": "https://www.linkedin.com/company/djerba-olive"},
    ]
    assert found["languages"]["value"] == ["EN", "FR"]
    assert found["country"]["value"] == "TN"
    assert found["timezone"] == {
        "value": "Africa/Tunis",
        "source": "https://djerbaolive.tn/",
        "confidence": "high",
    }


def test_a_country_name_in_jsonld_resolves_and_languages_come_from_hreflang() -> None:
    found = extract.extract_identity([_page("https://nabeulceramics.com/")], "nabeulceramics.com")

    assert found["name"]["value"] == "Nabeul Ceramics"
    assert found["country"]["value"] == "TN"
    assert found["languages"]["value"] == ["FR", "EN"]
    assert found["socials"]["value"][0]["platform"] == "tiktok"


def test_what_a_page_does_not_say_is_absent_not_guessed() -> None:
    found = extract.extract_identity([_page("https://plainstudio.com/")], "plainstudio.com")

    assert found["name"]["value"] == "Plain Studio"
    # "Hello." is no description; a .com says nothing about a timezone.
    assert "description" not in found
    assert "timezone" not in found
    assert "socials" not in found
    assert "logo_url" not in found


def test_a_country_code_domain_gives_a_low_confidence_timezone() -> None:
    page = extract.parse_page(
        "https://atelier.fr/", "<html><head><title>Atelier</title></head></html>"
    )

    found = extract.extract_identity([page], "atelier.fr")

    assert found["timezone"]["value"] == "Europe/Paris"
    assert found["timezone"]["confidence"] == "low"


# --- products --------------------------------------------------------------------------
def test_shopify_products_carry_name_url_price_and_image() -> None:
    fetched = FakeSiteFetcher().fetch("https://djerbaolive.tn/products.json")
    assert fetched is not None
    import json

    items = extract.products_from_shopify("https://djerbaolive.tn/", json.loads(fetched.text))

    assert [i["name"] for i in items][:2] == ["First harvest EVOO 500 ml", "Everyday olive oil 1 L"]
    assert items[0]["url"] == "https://djerbaolive.tn/products/first-harvest"
    assert items[0]["price"] == {"amount": "38.5", "currency": ""}
    assert items[0]["image"] == "https://djerbaolive.tn/cdn/first-harvest.jpg"


def test_jsonld_and_opengraph_products_are_read_with_their_currency() -> None:
    pages = [
        _page("https://nabeulceramics.com/product/dinner-plate"),
        _page("https://nabeulceramics.com/product/serving-bowl"),
    ]

    items = extract.products_from_pages(pages)

    assert {i["name"]: i["price"] for i in items} == {
        "Dinner plate 28 cm": {"amount": "42", "currency": "TND"},
        "Serving bowl": {"amount": "55", "currency": "TND"},
    }


def test_woocommerce_prices_are_converted_from_minor_units() -> None:
    payload = [
        {
            "name": "Tile set",
            "permalink": "https://x.tn/p/tiles",
            "prices": {"price": "60000", "currency_code": "TND", "currency_minor_unit": 3},
        }
    ]

    items = extract.products_from_woocommerce("https://x.tn/", payload)

    assert items == [
        {
            "name": "Tile set",
            "url": "https://x.tn/p/tiles",
            "source": "https://x.tn/wp-json/wc/store/v1/products",
            "price": {"amount": "60", "currency": "TND"},
        }
    ]


def test_merging_deduplicates_by_url_and_by_name_and_caps_the_list() -> None:
    a = [{"name": "Bowl", "url": "https://x/bowl", "source": "a"}]
    b = [
        {"name": "bowl", "url": "https://x/other", "source": "b"},
        {"name": "Cup", "url": "https://x/bowl/", "source": "b"},
    ]
    many = [{"name": f"P{i}", "url": f"https://x/{i}", "source": "c"} for i in range(30)]

    assert [p["source"] for p in extract.merge_products(a, b)] == ["a"]
    assert len(extract.merge_products(many)) == extract.MAX_PRODUCTS


# --- palette and fonts -------------------------------------------------------------------
def test_the_palette_is_read_by_use_and_assigned_roles() -> None:
    home = _page("https://djerbaolive.tn/")
    css = [*home.css, _css("https://djerbaolive.tn/assets/site.css")]

    palette = extract.extract_palette(css, home.metas, "https://djerbaolive.tn/")

    roles = {c["role"]: c["hex"] for c in palette}
    assert roles["Primary"] == "#3F5A1E"
    assert roles["Accent"] == "#C8A13B"
    assert roles["Ink"] == "#1F2A14"
    assert roles["Background"] == "#F6F1E1"


def test_no_colour_on_the_site_means_no_palette_not_an_invented_one() -> None:
    assert extract.extract_palette([], {}, "https://plainstudio.com/") == []
    # Greys only: an ink and a background, but no "primary" brand colour.
    roles = [
        c["role"]
        for c in extract.extract_palette(["body{color:#111111;background:#FAFAFA}"], {}, "s")
    ]
    assert "Primary" not in roles


def test_fonts_come_from_google_fonts_and_heading_and_body_rules() -> None:
    home = _page("https://djerbaolive.tn/")
    css = [_css("https://djerbaolive.tn/assets/site.css")]

    fonts = extract.extract_fonts(css, home.stylesheets, "https://djerbaolive.tn/")

    assert fonts["display"]["name"] == "Playfair Display"
    assert fonts["body"]["name"] == "Lato"


def test_generic_and_system_fonts_are_not_brand_fonts() -> None:
    fonts = extract.extract_fonts(["body{font-family:system-ui,-apple-system,sans-serif}"], [], "s")
    assert fonts == {}


# --- text ----------------------------------------------------------------------------------
def test_sentences_keep_prose_and_drop_navigation() -> None:
    home = _page("https://djerbaolive.tn/")

    found = [s for s, _ in extract.sentences([home])]

    assert "Shop" not in found
    assert any(s.startswith("Our Chemlali olives are picked") for s in found)
