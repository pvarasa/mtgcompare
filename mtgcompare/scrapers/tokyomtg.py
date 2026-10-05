"""TokyoMTG (tokyomtg.com) scraper.

TokyoMTG has no public API — site is bespoke PHP with server-rendered
HTML at `/cardpage.html?query=<name>&p=q&cx=jpy`. Each printing is
wrapped in a `div.pwrapper` with Bootstrap nav-tabs for
Regular/Played/Foil/PlayedFoil. We only pull the Regular (NM) tab of
English-version entries that have stock.

The Regular pane is picked by its id (``reg_<n>``), never by which tab is
active: the site activates the first tab that *has stock*, so a printing
with no regular copies opens on Played or Foil. Reading the active pane
reported those prices as NM non-foil — 23 of 50 records across five
staple cards on 2026-10-02, e.g. a ¥114,990 Judge-foil Force of Will.

Results come 20 printings per page, paged by the ``b`` offset
(``b=20``, ``b=40``, ...). The site's filter form (English / non-foil /
in stock) would cut that down, but it lives in the PHP session and dropped
valid printings when tested, so the parser does the filtering instead.

``cx=jpy`` is load-bearing: without it the site geo-IPs the client and
serves prices in the local currency (EUR from a German Hetzner egress,
USD from US IPs, etc.), and our ``_PRICE_RE`` only matches the ¥ glyph.
Before this was set, prod silently returned 0 rows for every search.

The default `User-Agent` gets a 429, so the scraper merges a full
``Accept-*`` browser fingerprint via ``SESSION_HEADERS``.

Printings are labelled by set *name* ("Double Masters"), with special
printings under a "<set> Variants" / "<set> Alt Art" name and nothing to
tell a set's regular card from its retro-frame one. So the variant is
``other`` for the former and unknown (``None``) otherwise; the Scryfall
enrichment maps names to set codes and settles what it can.

The `parse_search_html` function is pure and is what tests exercise.
"""
import re

from selectolax.parser import HTMLParser

from .html_base import HtmlSearchScrapper, to_usd
from .names import matched_name
from .variants import OTHER

BASE_URL = "https://tokyomtg.com"
SEARCH_URL = f"{BASE_URL}/cardpage.html"

# "¥14,990" — BS4 decodes the `&yen;` entity to the `¥` character.
_PRICE_RE = re.compile(r"¥\s*([\d,]+)")
# "Stock: 3".
_STOCK_RE = re.compile(r"Stock:\s*(\d+)")

ENGLISH_BADGE = "English Version"
_VARIANT_SET_RE = re.compile(r"\s(Variants|Alt Art)$")


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from a TokyoMTG /cardpage.html response."""
    tree = HTMLParser(html)
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

        card = matched_name(name_el.text(deep=True, strip=True), card_name)
        if card is None:
            continue

        # Regular = NM non-foil. Select it by id, not by "active" (see the
        # module docstring). The underscore matters: "regfoil_" also
        # starts with "reg".
        reg_pane = next(
            (p for p in wrap.css("div.tab-pane")
             if (p.attributes.get("id") or "").startswith("reg_")),
            None,
        )
        if not reg_pane:
            continue  # No regular printing listed at all.
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
        price_usd = to_usd(price_jpy, fx_jpy_per_usd)

        href = (detail_link_el.attributes.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

        set_name = set_el.text(deep=True, strip=True)
        records.append({
            "shop": "TokyoMTG",
            "card": card,
            "set": set_name,
            "number": None,
            "variant": OTHER if _VARIANT_SET_RE.search(set_name) else None,
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
    LOGGER_NAME = "mtgcompare.scrapers.tokyomtg"
    SESSION_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    PAGE_PARAM = "b"
    _PAGE_SIZE = 20

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)

    def search_params(self, card_name: str) -> dict:
        return {"query": card_name, "p": "q", "cx": "jpy"}

    def page_value(self, page: int) -> str:
        return str((page - 1) * self._PAGE_SIZE)
