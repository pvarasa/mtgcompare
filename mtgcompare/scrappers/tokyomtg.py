"""TokyoMTG (tokyomtg.com) scraper.

TokyoMTG has no public API — site is bespoke PHP with server-rendered
HTML at `/cardpage.html?query=<name>&p=q&cx=jpy`. Each printing is
wrapped in a `div.pwrapper` with Bootstrap nav-tabs for
Regular/Played/Foil/PlayedFoil. We only pull the Regular (NM) tab of
English-version entries that have stock.

``cx=jpy`` is load-bearing: without it the site geo-IPs the client and
serves prices in the local currency (EUR from a German Hetzner egress,
USD from US IPs, etc.), and our ``_PRICE_RE`` only matches the ¥ glyph.
Before this was set, prod silently returned 0 rows for every search.

The default `User-Agent` gets a 429, so the scraper merges a full
``Accept-*`` browser fingerprint via ``SESSION_HEADERS``.

The `parse_search_html` function is pure and is what tests exercise.
"""
import re

from selectolax.parser import HTMLParser

from ._base import HtmlSearchScrapper

BASE_URL = "https://tokyomtg.com"
SEARCH_URL = f"{BASE_URL}/cardpage.html"

# "¥14,990" — BS4 decodes the `&yen;` entity to the `¥` character.
_PRICE_RE = re.compile(r"¥\s*([\d,]+)")
# "Stock: 3".
_STOCK_RE = re.compile(r"Stock:\s*(\d+)")

ENGLISH_BADGE = "English Version"


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from a TokyoMTG /cardpage.html response."""
    tree = HTMLParser(html)
    target = card_name.strip().lower()
    records: list[dict] = []

    for wrap in tree.css("div.pwrapper"):
        badge = wrap.css_first("span.lang-badge")
        if not badge or badge.text(deep=True, strip=True) != ENGLISH_BADGE:
            continue

        info = wrap.css_first("div.col.mx-2")
        if not info:
            continue

        name_el = info.css_first("a > h3")
        set_el = info.css_first("h3 > a > b")
        detail_link_el = info.css_first("a[href*='carddetails.html']")
        if not (name_el and set_el and detail_link_el):
            continue

        card = name_el.text(deep=True, strip=True)
        if card.lower() != target:
            continue

        # The first (active) tab-pane is always Regular/NM non-foil.
        reg_pane = wrap.css_first("div.tab-pane.show.active")
        if not reg_pane:
            continue
        price_el = reg_pane.css_first("h3.price-text")
        if not price_el:
            continue  # Out of stock — no price-text node.

        pane_text = price_el.text(deep=True, separator=" ", strip=True)
        price_match = _PRICE_RE.search(pane_text)
        stock_match = _STOCK_RE.search(pane_text)
        if not (price_match and stock_match):
            continue

        stock = int(stock_match.group(1))
        if stock <= 0:
            continue

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (detail_link_el.attributes.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

        records.append({
            "shop": "TokyoMTG",
            "card": card,
            "set": set_el.text(deep=True, strip=True),
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "stock": stock,
            "condition": "NM",
            "link": link,
        })
    return records


class TokyoMtgScrapper(HtmlSearchScrapper):
    SHOP_NAME = "TokyoMTG"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrappers.tokyomtg"
    SESSION_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)

    def search_params(self, card_name: str) -> dict:
        return {"query": card_name, "p": "q", "cx": "jpy"}
