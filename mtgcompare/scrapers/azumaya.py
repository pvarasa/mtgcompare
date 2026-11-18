"""Azumaya (カードショップあずまや, azumayamtg.com) MTG scraper.

An EC-CUBE 3 shop. Search is ``/products/list?name=<card>&disp_number=128``
(the largest page size offered; pages are ``pageno``), and it lists only
in-stock products. Each product is one (printing × language × finish), with
a small table of its conditions — NM, NM-, SP, MP — each with its own price
and stock::

    [ENG]意志の力/Force of Will            NM 19990円 在庫：1
    [Foil][ENG]砕骨の巨人/Bonecrusher Giant
    [ENG]秘密を掘り下げる者/Delver of Secrets // 昆虫の逸脱者/Insectile Aberration
    [プロモ][ENG]対抗呪文/Counterspell

The title says nothing about the set. That is only on the product's own
page, as the product code (``DMR-0050E``: set, collector number, language,
an ``F`` for foil) and its categories. Some codes use Azumaya's own
abbreviations (``Al``, ``RV``, ``MPSAKH``) but the set's category then names
the real one — ``アライアンス(Alliances/AL/ALL)`` — or is an English set
name (``Amonkhet Invocations``) that the Scryfall enrichment maps; promos
and The List have no code at all (``The List(MB,The List)``).

So each English non-foil NM listing that matches the card costs one more
request (~0.5 s from Japan, ~1.8 s from the cluster). They run 4 at a time,
and a product's set never changes, so it is remembered per process: a
staple's second search fetches nothing but the list page. A product page
that fails fails the search (``ScraperFetchError``) rather than caching
rows without a set for a day.

Split cards are listed as ``Fire+Ice`` (``SPLIT_JOINER``).

``parse_search_html`` and ``parse_detail_html`` are pure and are what the
tests exercise.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .html_base import (
    HtmlSearchScrapper,
    ScraperFetchError,
    node_text_ws,
    raise_for_response,
    retry_after_s,
    to_usd,
)
from .names import matched_name
from .variants import normalize_number

SHOP_NAME = "Azumaya"
BASE_URL = "https://azumayamtg.com"
SEARCH_URL = f"{BASE_URL}/products/list"
DETAIL_URL = f"{BASE_URL}/products/detail/{{id}}"

_HEAD_RE = re.compile(r"^((?:\[[^\[\]]*\]|[^\[\]\s/]*\])+)")
_TAG_RE = re.compile(r"\[?([^\[\]]+)\]")
_FOIL_TAG_RE = re.compile(r"foil|エッチング|サージ", re.IGNORECASE)
_DETAIL_ID_RE = re.compile(r"/products/detail/(\d+)")
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
_STOCK_RE = re.compile(r"在庫[:：]\s*(\d+)")
_CODE_RE = re.compile(r"^([A-Za-z0-9]+)-(\d+)")
_SET_LIKE_RE = re.compile(r"[A-Z0-9]{3,4}")
# "アライアンス(Alliances/AL/ALL)", "The List(MB,The List)".
_CATEGORY_ALIASES_RE = re.compile(r"[(（]([^()（）]+)[)）]\s*$")

_DETAIL_WORKERS = 4


class Listing(NamedTuple):
    product_id: str
    card: str
    price_jpy: float
    stock: int
    link: str


class Printing(NamedTuple):
    set: str | None
    number: str | None


def _title_tags(title: str) -> tuple[list[str], str]:
    """``[Foil][ENG]意志の力/Force of Will`` → (["Foil", "ENG"], name).
    Tolerates the odd unbalanced bracket (``[サージ][Foil]JPN]``)."""
    head = _HEAD_RE.match(title)
    if head is None:
        return [], title
    return [t.strip() for t in _TAG_RE.findall(head.group(1))], title[head.end():]


def parse_search_html(html: str | bytes, card_name: str) -> list[Listing]:
    """English non-foil NM in-stock listings of ``card_name`` on one page."""
    tree = HTMLParser(html)
    listings: list[Listing] = []
    for item in tree.css("div.product_item"):
        link_el = item.css_first("a")
        name_el = item.css_first(".item_name")
        if link_el is None or name_el is None:
            continue
        href = (link_el.attributes.get("href") or "").strip()
        pid = _DETAIL_ID_RE.search(href)
        if pid is None:
            continue
        tags, name = _title_tags(node_text_ws(name_el))
        if "ENG" not in tags:
            continue
        if any(_FOIL_TAG_RE.search(t) and t.lower() != "non-foil" for t in tags):
            continue
        card = matched_name(name, card_name)
        if card is None:
            continue
        for row in item.css("tbody tr"):
            cells = row.css("td")
            if len(cells) < 3 or cells[0].text(strip=True) != "NM":
                continue
            price = _PRICE_RE.search(node_text_ws(cells[1]))
            stock = _STOCK_RE.search(node_text_ws(cells[2]))
            if price is None or stock is None or int(stock.group(1)) <= 0:
                continue
            listings.append(Listing(
                product_id=pid.group(1),
                card=card,
                price_jpy=float(price.group(1).replace(",", "")),
                stock=int(stock.group(1)),
                link=href if href.startswith("http") else f"{BASE_URL}{href}",
            ))
    return listings


def parse_detail_html(html: str | bytes) -> Printing:
    """Set and collector number from a product page."""
    tree = HTMLParser(html)
    code_el = tree.css_first("#item_code_default")
    code = _CODE_RE.match(code_el.text(strip=True) if code_el is not None else "")
    categories = [a.text(strip=True) for a in tree.css("#relative_category_box li a")]

    aliased = None
    english_name = None
    for name in categories:
        m = _CATEGORY_ALIASES_RE.search(name)
        if m:
            aliased = re.split(r"[/,]", m.group(1))[-1].strip()
        elif name.isascii() and not english_name:
            english_name = name

    prefix = code.group(1).upper() if code else None
    if aliased:
        mtg_set = aliased
    elif prefix and _SET_LIKE_RE.fullmatch(prefix):
        mtg_set = prefix
    else:
        mtg_set = english_name or prefix
    return Printing(set=mtg_set, number=normalize_number(code.group(2)) if code else None)


# product id → Printing. A product's printing never changes; bounded only so
# a long-lived worker can't grow it without limit.
_PRINTINGS: dict[str, Printing] = {}
_PRINTINGS_LOCK = threading.Lock()
_PRINTINGS_MAX = 50_000


class AzumayaScrapper(HtmlSearchScrapper):
    SHOP_NAME = SHOP_NAME
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.azumaya"
    SEARCH_PARAM_NAME = "name"
    PAGE_PARAM = "pageno"
    MAX_PAGES = 2
    SPLIT_JOINER = "+"
    MAX_CONCURRENT = 4

    def search_params(self, card_name: str) -> dict:
        return {"name": card_name, "disp_number": 128}

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        listings = parse_search_html(html, card_name)
        printings = self._printings({listing.product_id for listing in listings})
        records = []
        for listing in listings:
            printing = printings[listing.product_id]
            if not printing.set:
                continue
            records.append({
                "shop": SHOP_NAME,
                "card": listing.card,
                "set": printing.set,
                "number": printing.number,
                # The page names no treatment; enrichment settles it from
                # the collector number.
                "variant": None,
                "price_jpy": listing.price_jpy,
                "price_usd": to_usd(listing.price_jpy, self.fx),
                "stock": listing.stock,
                "condition": "NM",
                "link": listing.link,
            })
        return records

    def _printings(self, product_ids: set[str]) -> dict[str, Printing]:
        with _PRINTINGS_LOCK:
            known = {pid: _PRINTINGS[pid] for pid in product_ids if pid in _PRINTINGS}
        missing = sorted(product_ids - known.keys())
        if missing:
            with ThreadPoolExecutor(max_workers=min(_DETAIL_WORKERS, len(missing))) as ex:
                fetched = dict(zip(missing, ex.map(self._fetch_printing, missing), strict=True))
            with _PRINTINGS_LOCK:
                if len(_PRINTINGS) + len(fetched) > _PRINTINGS_MAX:
                    _PRINTINGS.clear()
                _PRINTINGS.update(fetched)
            known.update(fetched)
        return known

    def _fetch_printing(self, product_id: str) -> Printing:
        with self._THROTTLE.slot():
            try:
                resp = self.session.get(
                    DETAIL_URL.format(id=product_id), timeout=self.REQUEST_TIMEOUT_S,
                )
            except requests.RequestException as e:
                raise ScraperFetchError(f"{SHOP_NAME} product page fetch failed: {e}") from e
        if resp.status_code == 429:
            self._THROTTLE.back_off(retry_after_s(resp))
        raise_for_response(resp, SHOP_NAME)
        return parse_detail_html(resp.content)
