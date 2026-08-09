"""Cardshop Serra (cardshop-serra.com) MTG scraper.

Shopify storefront. The shop migrated off ec-cube during 2026: the old
``/mtg/products/list?name=<name>`` path now 301s to ``/collections/all``
and drops the query string, so it answered 200 with the full catalogue
and the parser found nothing — a silent zero-rows failure rather than an
error. Search now lives at ``/search?q=<name>``.

One ``div.product_item`` per (language × printing); conditions (NM, SP,
MP) are ``div.condition_area`` sub-rows inside it, each with its own
price and stock count.

Title format (unchanged across the migration, apart from the foil
marker now being explicit):

    (<lang_jp>)[【…Foil】]<JP> / <EN>[ <flavor>] 【<SET>】[ No.<num>]

For example::

    (英)意志の力 / Force of Will【2XM】 No.051
    (英)意志の力 / Force of Will ★拡張枠★ 【DMR】 No.418
    (英)【Foil】意志の力 / Force of Will【DMR】 No.050
    (日)【シルバースクロールFoil】意志の力 / Force of Will ★日本画★ 【SOA】 No.149

We filter to ``(英)`` (English), non-foil, NM-condition, in-stock rows.
The foil filter matters because the marker sits on the *Japanese* side of
the ``/``, so it lands in the discarded ``jp`` group — without an explicit
check an English foil parses as a plain English listing and reports a
foil price as the non-foil one.

Only the first result page is fetched, matching the single-GET shape of
``HtmlSearchScrapper``. Serra sorts by relevance, so in-stock English
printings land on page 1; page 2+ is long-tail zero-stock variants.

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import re

from selectolax.parser import HTMLParser

from .html_base import HtmlSearchScrapper, node_text_ws, to_usd

BASE_URL = "https://cardshop-serra.com"
SEARCH_URL = f"{BASE_URL}/search"

_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# Title parser. Greedy on the JP side, non-greedy on the EN side so optional
# flavor markers like ★拡張枠★ stay outside the captured EN name.
_TITLE_RE = re.compile(
    r"^\((?P<lang>[^)]+)\)"
    r"(?P<jp>[^/]+?)"
    r"\s*/\s*"
    r"(?P<en>.+?)"
    r"\s*【(?P<set>[^】]+)】"
    r"(?:\s*No\.[\w\-]+)?"
    r"\s*$"
)
# Decorations sometimes appended to the EN name to flag printing variants.
_FLAVOR_RE = re.compile(r"\s*[★●■▼◆☆][^★●■▼◆☆]*[★●■▼◆☆]\s*$")
# Foil marker bracket: plain 【Foil】 and decorated variants like
# 【シルバースクロールFoil】. Set brackets are uppercase ASCII so they can't
# collide with this.
_FOIL_RE = re.compile(r"【[^】]*foil[^】]*】", re.IGNORECASE)


def _row_price_jpy(condition_area) -> float | None:
    price_el = condition_area.css_first(".price_wrap")
    if price_el is None:
        return None
    m = _PRICE_RE.search(node_text_ws(price_el))
    return float(m.group(1).replace(",", "")) if m else None


def _row_stock(condition_area) -> int:
    """Stock is the "/ N" figure next to the quantity input; 0 when sold out."""
    count_el = condition_area.css_first(".count .parameter")
    if count_el is None:
        return 0
    text = count_el.text(deep=True, strip=True)
    return int(text) if text.isdigit() else 0


def _row_condition(condition_area) -> str:
    """The grade label, rendered twice (desktop ``.pc`` + mobile ``.sp``)."""
    el = condition_area.css_first(".condition_name.pc") or condition_area.css_first(".condition_name")
    return el.text(deep=True, strip=True) if el is not None else ""


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English non-foil in-stock rows for ``card_name`` from a Serra page."""
    tree = HTMLParser(html)
    target = card_name.strip().lower()
    records: list[dict] = []

    for item in tree.css("div.product_item"):
        title_el = item.css_first(".card_title a")
        if title_el is None:
            continue

        title = node_text_ws(title_el)
        if _FOIL_RE.search(title):
            continue

        m = _TITLE_RE.match(title)
        if not m:
            continue

        if m.group("lang") != "英":
            continue

        en = _FLAVOR_RE.sub("", m.group("en")).strip()
        if en.lower() != target:
            continue

        href = (title_el.attributes.get("href") or "").strip()
        # Shopify appends per-search tracking params (?_pos=&_sid=&_ss=) that
        # are noise in a stored/cached link.
        href = href.split("?", 1)[0]
        link = href if href.startswith("http") else f"{BASE_URL}{href}"

        # One condition_area per grade; Serra grades SP/MP below NM and we
        # match the Card Rush convention of exposing only NM.
        for area in item.css("div.condition_area"):
            if _row_condition(area) != "NM":
                continue

            stock = _row_stock(area)
            if stock <= 0:
                continue

            price_jpy = _row_price_jpy(area)
            if price_jpy is None or price_jpy <= 0:
                continue

            records.append({
                "shop": "Cardshop Serra",
                "card": en,
                "set": m.group("set").strip(),
                "price_jpy": price_jpy,
                "price_usd": to_usd(price_jpy, fx_jpy_per_usd),
                "stock": stock,
                "condition": "NM",
                "link": link,
            })
    return records


class CardshopSerraScrapper(HtmlSearchScrapper):
    SHOP_NAME = "Cardshop Serra"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.serra"
    SEARCH_PARAM_NAME = "q"

    def search_params(self, card_name: str) -> dict:
        # `type=product` keeps blog/page hits out of the result set.
        return {"q": card_name, "type": "product"}

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
