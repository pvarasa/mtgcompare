"""BIGWEB (bigweb.co.jp, the BIG MAGIC stores' web shop) MTG scraper.

The storefront at ``mtg.bigweb.co.jp`` sits behind a Cloudflare JS
challenge (403 "Just a moment…" to any script, from Japan and from the
cluster's Helsinki egress alike), but the Angular app at
``www.bigweb.co.jp`` reads a plain JSON API that has no such gate:

    GET https://api.bigweb.co.jp/products
        ?game_id=1&name=<card>&lang=[1]&condition=[1]

``game_id=1`` is MTG (9 is Yu-Gi-Oh). ``lang`` and ``condition`` are
JSON arrays of ids from ``/SearchFilters/?game_id=1``: language 1 is
English, condition 1 is non-foil ニアミント (foil NM is 4, Etched FOIL NM
661). Filtering server-side keeps even Sol Ring (86 English NM printings,
2026-11-18) on one 100-row page; ``pagenate.nextPage`` says when there is
another, and a page past the end answers 404 rather than an empty list.
There is no in-stock filter, so sold-out rows (``is_sold_out``) come back
and are dropped here.

One item is one (printing × language × condition), already priced and
stocked. The awkward parts are names and numbers:

- ``name`` is the English name, sometimes with the collector number glued
  on unclosed (``Sol Ring(2672`` — Secret Lair), and sometimes led by
  ``【…】`` markers: ``【旧枠】`` (old frame), ``【拡張】`` (extended art —
  ``variants`` only knows the longer 拡張アート / 拡張枠 spellings, and
  widening it to bare 拡張 would also catch 拡張パック, so it is
  translated here).
- Secret Lair printings that have their own name are listed under it alone
  (``Vivi's Thunder Magic``, ``Hadoken``), with a Japanese name of their
  own too, so nothing ties them to the card; they are dropped, as at the
  other shops whose titles don't name the card.
- Lots (``Swamp(10枚セット）``) are priced per bundle and art cards
  (``(Art)(箔押し)Kaito Shizuki``) aren't the card; both are skipped.
- ``cardset.slip`` is the set code with a suffix for a sub-sheet:
  ``MH2_BF`` (ブースターファン — the showcase/borderless treatments),
  ``CMR_CMD`` (commander decks), ``2XM_VIP``, plus stray spaces and dots
  (`` 2X2_BF``, ``FDN.``) and the odd set name (``The List``, which the
  enrichment maps by name). ``_BF`` marks a variant printing; other
  suffixes say nothing about the frame, so those rows' variant is unknown.
- ``comment`` usually starts with the collector number, but also carries
  "on display at the Namba store" notes, internal stock numbers
  (管理番号…), drop numbers (``#0405 2539``) and Secret Lair drop titles
  after a ``<br>``. Only a leading bare number is taken; ``C18 222`` (a
  The List card's original set + number) yields none.

Split cards are named ``Fire+Ice``; ``Fire // Ice`` and ``Fire Ice`` find
nothing, and the front face alone (``Fire``) matches 700+ products, so the
``+`` spelling is tried before falling back to the front face, which is
how double-faced and adventure cards are listed.

Links go to the app's ``cardViewer`` page for the item: it opens on that
printing next to a table of every printing in stock, and doesn't hit the
Cloudflare challenge.

The ``parse_search_json`` function is pure and is what the tests exercise.
"""
import re

import orjson

from .html_base import HtmlSearchScrapper, ScraperFetchError, to_usd
from .names import matched_name
from .variants import OTHER, encode, normalize_number, tags_from_markers

SHOP_NAME = "BIGWEB"
API_URL = "https://api.bigweb.co.jp/products"
ITEM_URL = "https://www.bigweb.co.jp/ja/products/mtg/cardViewer/{id}"

MTG_GAME_ID = 1
ENGLISH = 1
NEAR_MINT = 1  # non-foil ニアミント

_LEADING_MARKERS_RE = re.compile(r"^((?:【[^】]*】)+)")
_MARKER_RE = re.compile(r"【([^】]*)】")
# "Sol Ring(2672", "Hallowed Fountain(392)", "Elspeth, Knight-Errant（701".
_NAME_NUMBER_RE = re.compile(r"\s*[(（]([A-Za-z]?\d+[a-z]?)[)）]?\s*$")
_SKIP_NAME_RE = re.compile(r"枚セット|\(Art\)|^EMBLEM", re.IGNORECASE)
_MARKER_ALIASES = {"拡張": "拡張アート"}

_STORE_NOTE_RE = re.compile(r"\S*店で展示中")
_STOCK_NO_RE = re.compile(r"管理(?:番号|コード)[:：]?\S*")
_DROP_NO_RE = re.compile(r"#\d+")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_COMMENT_NUMBER_RE = re.compile(r"(\d+[a-z]?)(?![\w.])")

# Sub-sheets that are a set of their own on Scryfall.
_SLIP_ALIASES = {"TSR_TSB": "TSB"}
_VARIANT_SUFFIXES = frozenset({"BF"})


def _split_name(raw: str) -> tuple[str, list[str], str | None]:
    """``【旧枠】Sol Ring(2672`` → (``Sol Ring``, [``旧枠``], ``2672``)."""
    name = raw.strip()
    markers: list[str] = []
    lead = _LEADING_MARKERS_RE.match(name)
    if lead:
        markers = _MARKER_RE.findall(lead.group(1))
        name = name[lead.end():]
    number = None
    tail = _NAME_NUMBER_RE.search(name)
    if tail:
        number = tail.group(1)
        name = name[:tail.start()]
    return name.strip(), markers, number


def _comment_number(comment: str | None) -> str | None:
    """The collector number a ``comment`` leads with, if it leads with one."""
    text = _STORE_NOTE_RE.sub(" ", comment or "")
    text = _DROP_NO_RE.sub(" ", _STOCK_NO_RE.sub(" ", text))
    for segment in _BR_RE.split(text):
        segment = segment.strip()
        if segment:
            m = _COMMENT_NUMBER_RE.match(segment)
            return m.group(1) if m else None
    return None


def _set_code(slip: str) -> tuple[str, str]:
    """(set code, sub-sheet suffix) from a ``cardset.slip``."""
    slip = slip.strip()
    if slip in _SLIP_ALIASES:
        return _SLIP_ALIASES[slip], ""
    code, _, suffix = slip.partition("_")
    code = code.strip().rstrip(".")
    # A set name ("The List") keeps its spelling for the enrichment's
    # name lookup; codes are upper-cased.
    return (code if " " in code else code.upper()), suffix.strip()


def parse_search_json(body: bytes | dict, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """In-stock English non-foil NM records for ``card_name`` from one
    ``/products`` response (raw bytes or the decoded dict)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            data = orjson.loads(body)
        except orjson.JSONDecodeError as e:
            raise ScraperFetchError(f"{SHOP_NAME} JSON decode failed: {e}") from e
    else:
        data = body
    records: list[dict] = []
    for item in data.get("items") or []:
        if item.get("is_sold_out") or item.get("is_hidden_price"):
            continue
        if item.get("is_box") or item.get("is_supply") or item.get("is_preorder_item"):
            continue
        if (item.get("language") or {}).get("id") != ENGLISH:
            continue
        if (item.get("condition") or {}).get("id") != NEAR_MINT:
            continue
        stock = int(item.get("stock_count") or 0)
        price_jpy = float(item.get("price") or 0)
        if stock <= 0 or price_jpy <= 0:
            continue

        raw_name = item.get("name") or ""
        if _SKIP_NAME_RE.search(raw_name):
            continue
        name, markers, number = _split_name(raw_name)
        card = matched_name(name, card_name)
        if card is None:
            continue

        mtg_set, suffix = _set_code((item.get("cardset") or {}).get("slip") or "")
        if not mtg_set:
            continue
        tags = tags_from_markers(_MARKER_ALIASES.get(m, m) for m in markers)
        if suffix in _VARIANT_SUFFIXES and not tags:
            tags = {OTHER}
        # An unrecognised sub-sheet (commander deck, VIP, Holiday) says
        # nothing about the frame: unknown, for enrichment to settle.
        variant = None if suffix and suffix not in _VARIANT_SUFFIXES and not tags else encode(tags)

        records.append({
            "shop": SHOP_NAME,
            "card": card,
            "set": mtg_set,
            "number": normalize_number(number or _comment_number(item.get("comment"))),
            "variant": variant,
            "price_jpy": price_jpy,
            "price_usd": to_usd(price_jpy, fx_jpy_per_usd),
            "stock": stock,
            "condition": "NM",
            "link": ITEM_URL.format(id=item["id"]),
        })
    return records


_NEXT_PAGE_RE = re.compile(rb'"nextPage"\s*:\s*true')


class BigwebScrapper(HtmlSearchScrapper):
    SHOP_NAME = SHOP_NAME
    SEARCH_URL = API_URL
    LOGGER_NAME = "mtgcompare.scrapers.bigweb"
    SESSION_HEADERS = {"Accept": "application/json"}
    PAGE_PARAM = "page"
    MAX_PAGES = 3
    SPLIT_JOINER = "+"

    def search_params(self, card_name: str) -> dict:
        return {
            "game_id": MTG_GAME_ID,
            "name": card_name,
            "lang": f"[{ENGLISH}]",
            "condition": f"[{NEAR_MINT}]",
        }

    def _has_next_page(self, body: str | bytes, page: int) -> bool:
        raw = body.encode() if isinstance(body, str) else body
        return _NEXT_PAGE_RE.search(raw) is not None

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        body = html.encode() if isinstance(html, str) else html
        return parse_search_json(body, card_name, self.fx)
