"""Nukenin (カードショップ抜忍, nukeninmtg.com) MTG scraper.

An EC-CUBE 4 shop. Search is ``/products/list?name=<card>``; it lists only
in-stock products unless told otherwise (Sol Ring, 2026-11-18: 58 of 280),
and ``disp_number=60`` (the largest page size offered) keeps a staple on
one page. Pages are ``pageno``.

Each listing title carries everything but the condition:

    【ENG】《意志の力/Force of Will》[SOA]
    【ENG】【Foil】■ボーダーレス■《太陽の指輪/Sol Ring》[SOC]
    【JPN】《太陽の指輪/Sol Ring》(211)[MSC]
    【ENG】《火+氷/Fire+Ice》[MH2]

Language and foil are ``【…】`` brackets, the treatment ``■…■`` markers
(which ``variants`` reads), the collector number an optional ``(…)`` and the
set code the trailing ``[…]``. The List reprints put their original set in
the parentheses instead (``(e01)[PWシンボル付き再版]``), so only a value that
starts with a digit is taken as the number. A handful of promos carry a word
instead of a set code (``[ショーダウン]``, ``[アリーナ]``); that label is kept
as the set for want of a better one. Etched and surge listings are foil
finishes even when the title says so only in the marker, so those are
dropped too.

Condition, price and stock are per "class": the condition is the
``classcategory_id1`` select's option label (NM, SP), and the page script
defines ``eccube.productsClassCategories`` — ``{product id: {class id:
{"#": {price02_inc_tax, stock, stock_find, …}}}}`` — which this parser
reads as JSON rather than the "---" placeholders the HTML shows.

Split cards are listed as ``Fire+Ice`` and double-faced ones by their front
face, as at BIGWEB (``SPLIT_JOINER``).

The shop is slow: 7–9 s before the first byte for an uncached search, and
the ~1 MB page (a full category menu, uncompressed) adds ~5 s from the
cluster's Helsinki egress, so a request alone takes ~14 s there and 8
concurrent ones took ~15 s each from Japan. Requests are capped at 4 in
flight so a decklist doesn't pile onto a small shop; cards that miss the
per-shop timeout still land in the cache for the next search.

``parse_search_html`` is pure and is what the tests exercise.
"""
import re

import orjson
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .html_base import HtmlSearchScrapper, node_text_ws, to_usd
from .names import matched_name
from .variants import encode, normalize_number, tags_from_markers

SHOP_NAME = "Nukenin"
BASE_URL = "https://nukeninmtg.com"
SEARCH_URL = f"{BASE_URL}/products/list"

_TITLE_RE = re.compile(
    r"^(?P<head>(?:【[^】]*】|■[^■]*■|\s)*)"
    r"《(?P<name>[^》]+)》\s*"
    r"(?:\((?P<num>[^)]*)\)\s*)?"
    r"\[(?P<set>[^\]]+)\]\s*$"
)
_BRACKET_RE = re.compile(r"【([^】]*)】")
_MARKER_RE = re.compile(r"■([^■]+)■")
# Foil finishes some titles flag only in their ■…■ marker.
_FOIL_MARKER_RE = re.compile(r"エッチング|サージ|foil", re.IGNORECASE)
_CLASSES_RE = re.compile(rb"eccube\.productsClassCategories\s*=\s*(\{.*?\});", re.S)
_DETAIL_ID_RE = re.compile(r"/products/detail/(\d+)")
_PRICE_RE = re.compile(r"[\d,]+")


def _class_data(html: bytes) -> dict:
    m = _CLASSES_RE.search(html)
    if m is None:
        return {}
    try:
        return orjson.loads(m.group(1))
    except orjson.JSONDecodeError:
        return {}


def _price(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    return float(m.group(0).replace(",", "")) if m else None


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """In-stock English non-foil NM records for ``card_name`` from one page."""
    raw = html.encode() if isinstance(html, str) else html
    classes = _class_data(raw)
    tree = HTMLParser(raw)
    records: list[dict] = []

    for title_el in tree.css(".item_list .item_name a"):
        href = (title_el.attributes.get("href") or "").strip()
        pid = _DETAIL_ID_RE.search(href)
        m = _TITLE_RE.match(node_text_ws(title_el))
        if pid is None or m is None:
            continue
        brackets = _BRACKET_RE.findall(m.group("head"))
        markers = _MARKER_RE.findall(m.group("head"))
        if "ENG" not in brackets:
            continue
        if any(_FOIL_MARKER_RE.search(b) for b in brackets + markers):
            continue
        card = matched_name(m.group("name"), card_name)
        if card is None:
            continue

        # The List reprints put the original set in the parentheses
        # ("(e01)[PWシンボル付き再版]"): only a leading digit is a number.
        num = (m.group("num") or "").strip()
        number = normalize_number(num) if num[:1].isdigit() else None
        product_id = pid.group(1)
        select = tree.css_first(f"select#classcategory_id1{product_id}")
        if select is None:
            continue
        product_classes = classes.get(product_id) or {}
        mtg_set = m.group("set").strip()

        for option in select.css("option"):
            if option.text(strip=True) != "NM":
                continue
            data = (product_classes.get(option.attributes.get("value") or "") or {}).get("#") or {}
            try:
                stock = int(data.get("stock") or 0)
            except ValueError:
                stock = 0
            price_jpy = _price(str(data.get("price02_inc_tax") or ""))
            if not data.get("stock_find") or stock <= 0 or not price_jpy:
                continue
            records.append({
                "shop": SHOP_NAME,
                "card": card,
                "set": mtg_set,
                "number": number,
                "variant": encode(tags_from_markers(markers)),
                "price_jpy": price_jpy,
                "price_usd": to_usd(price_jpy, fx_jpy_per_usd),
                "stock": stock,
                "condition": "NM",
                "link": href if href.startswith("http") else f"{BASE_URL}{href}",
            })
    return records


class NukeninScrapper(HtmlSearchScrapper):
    SHOP_NAME = SHOP_NAME
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.nukenin"
    SEARCH_PARAM_NAME = "name"
    PAGE_PARAM = "pageno"
    MAX_PAGES = 2
    SPLIT_JOINER = "+"
    REQUEST_TIMEOUT_S = 40.0
    MAX_CONCURRENT = 4

    def search_params(self, card_name: str) -> dict:
        return {"name": card_name, "disp_number": 60}

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
