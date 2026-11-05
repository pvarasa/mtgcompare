"""Japanese shop prices for sealed boosters.

Sell prices (what a shop asks): Hareruya, SingleStar, Cardshop Serra.
Buy prices (what a shop pays you): Hareruya's buylist.

Each shop's whole sealed listing is crawled once per refresh instead of
searching per product: Hareruya through two keyword searches of its JSON
search API, SingleStar through its 未開封BOX category pages, Serra through
its Shopify collection feed, and the Hareruya buylist through its booster
BOX / pack categories. Titles are parsed by ``jp_titles``; ``best_offers``
reduces the listings to one price per (catalog product, language): the
cheapest in-stock offer. For the buylist that is the lowest of its bids on
the same product (it bids per printing, e.g. a WAR first print above the
reprint; which one a holder has is unknown, so the conservative bid wins).

Prices are in JPY and are asking / buying prices, not sales. Only in-stock
offers are kept: a sold-out listing's price is not something anyone can pay.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..scrapers.hareruya import UNISEARCH_API
from ..scrapers.hareruya import make_session as hareruya_session
from ..scrapers.html_base import ScraperFetchError, retry_after_s
from ..scrapers.html_base import make_session as html_session
from . import jp_titles as jt

logger = logging.getLogger(__name__)

HARERUYA = "hareruya"
SINGLESTAR = "singlestar"
SERRA = "serra"
HARERUYA_BUY = "hareruya_buy"
SHOP_LABELS = {
    HARERUYA: "Hareruya", SINGLESTAR: "SingleStar", SERRA: "Cardshop Serra",
    HARERUYA_BUY: "Hareruya buylist",
}
# Sources whose price is what the shop pays, not what it asks.
BUY_SOURCES = frozenset({HARERUYA_BUY})

HARERUYA_DETAIL_URL = "https://www.hareruyamtg.com/ja/products/detail/{product}"
# Keyword searches that together cover Hareruya's sealed boosters; their
# results also include some single cards, which the title parser rejects.
HARERUYA_KEYWORDS = ("ブースターBOX", "ブースターパック")
_HARERUYA_ROWS = 60
_HARERUYA_MAX_PAGES = 40

SINGLESTAR_CATEGORY_URL = "https://www.singlestar.jp/product-list/1427/0/photo"  # 未開封BOX
_SINGLESTAR_PAGE_SIZE = 100
_SINGLESTAR_MAX_PAGES = 15
# SingleStar is slow on cold pages (10-50 s observed); give it room.
_SINGLESTAR_TIMEOUT_S = 90

# Serra (Shopify) answers 429 to bursts; its card scraper is paced too.
SERRA_COLLECTION_URL = "https://cardshop-serra.com/collections/sidebar-cat-5/products.json"  # 未開封ボックス&パック
SERRA_PRODUCT_URL = "https://cardshop-serra.com/products/{handle}"
_SERRA_PAGE_SIZE = 250
_SERRA_MAX_PAGES = 20
_SERRA_PACING_S = 1.5
# One wait-and-retry per page on a 429 (Serra sends Retry-After: 60); a
# daily job can afford a minute, a ban-extending retry loop it can't.
_SERRA_MAX_RETRY_WAIT_S = 90.0

HARERUYA_BUY_URL = "https://www.hareruyamtg.com/ja/purchase/search"
HARERUYA_BUY_BASE = "https://www.hareruyamtg.com"
# Buylist categories: 506 ブースターBOX, 507 ブースターパック.
HARERUYA_BUY_CATEGORIES = ("506", "507")
_HARERUYA_BUY_PAGE_SIZE = 60
_HARERUYA_BUY_MAX_PAGES = 30

_PAGE_PACING_S = 0.5
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
_STOCK_RE = re.compile(r"在庫数\s*(\d+)")
# SingleStar image paths are hex-encoded and often carry the set code:
# ".../_/70726f...6a7067..." → "product/FRA/FRApro/MTGpro_FRA_01.jpg".
_IMG_HEX_RE = re.compile(r"/_/([0-9a-f]+)")
_IMG_SET_RE = re.compile(r"product/(?:[A-Z0-9]+/)?([A-Z0-9]{3,5})(?:pro|box)/")


@dataclass(frozen=True)
class ShopListing:
    title: str
    price_jpy: float | None
    # Units in stock; None = available but the shop doesn't say how many
    # (Serra), or not applicable (a buylist bid).
    stock: int | None
    url: str
    hint_set_code: str | None = None  # shop-side guess, used only as a fallback

    @property
    def available(self) -> bool:
        return bool(self.price_jpy) and (self.stock is None or self.stock > 0)


# --- Hareruya ------------------------------------------------------------------

def hareruya_listings_from_docs(docs: list[dict]) -> list[ShopListing]:
    out: list[ShopListing] = []
    seen: set[str] = set()
    for d in docs:
        pid = str(d.get("product") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            price = float(d.get("price") or 0) or None
            stock = int(d.get("stock") or 0)
        except ValueError:
            continue
        out.append(ShopListing(
            title=d.get("product_name") or "",
            price_jpy=price,
            stock=stock,
            url=HARERUYA_DETAIL_URL.format(product=pid),
        ))
    return out


def crawl_hareruya(session: requests.Session | None = None, pacing_s: float = _PAGE_PACING_S) -> list[ShopListing]:
    session = session or hareruya_session()
    docs: list[dict] = []
    for kw in HARERUYA_KEYWORDS:
        for page in range(1, _HARERUYA_MAX_PAGES + 1):
            if pacing_s and (page > 1 or docs):
                time.sleep(pacing_s)
            try:
                resp = session.get(UNISEARCH_API, params={
                    "kw": kw, "rows": str(_HARERUYA_ROWS), "page": str(page),
                }, timeout=30)
                resp.raise_for_status()
                body = resp.json().get("response", {})
            except (requests.RequestException, ValueError) as e:
                raise ScraperFetchError(f"Hareruya sealed crawl failed ({kw} p{page}): {e}") from e
            batch = body.get("docs") or []
            docs.extend(batch)
            if not batch or page * _HARERUYA_ROWS >= int(body.get("numFound") or 0):
                break
    return hareruya_listings_from_docs(docs)


# --- SingleStar ----------------------------------------------------------------

def _image_set_code(src: str) -> str | None:
    m = _IMG_HEX_RE.search(src or "")
    if not m:
        return None
    hexstr = m.group(1)
    try:
        path = bytes.fromhex(hexstr[: len(hexstr) // 2 * 2]).split(b"\x00")[0].decode("latin-1")
    except ValueError:
        return None
    code = _IMG_SET_RE.search(path)
    return code.group(1) if code else None


def parse_singlestar_category_html(html: str | bytes) -> list[ShopListing]:
    out: list[ShopListing] = []
    for cell in HTMLParser(html).css("li.list_item_cell"):
        name_el = cell.css_first("span.goods_name")
        link_el = cell.css_first("a.item_data_link")
        if not (name_el and link_el):
            continue
        price_el = cell.css_first("span.figure")
        price_m = _PRICE_RE.search(price_el.text(strip=True)) if price_el else None
        stock_el = cell.css_first("p.stock")
        stock = 0
        if stock_el and "soldout" not in (stock_el.attributes.get("class") or "").split():
            sm = _STOCK_RE.search(stock_el.text(strip=True))
            stock = int(sm.group(1)) if sm else 0
        img = cell.css_first("img")
        out.append(ShopListing(
            title=name_el.text(strip=True),
            price_jpy=float(price_m.group(1).replace(",", "")) if price_m else None,
            stock=stock,
            url=(link_el.attributes.get("href") or "").strip(),
            hint_set_code=_image_set_code(img.attributes.get("src") or "" if img else ""),
        ))
    return out


def crawl_singlestar(session: requests.Session | None = None, pacing_s: float = _PAGE_PACING_S) -> list[ShopListing]:
    session = session or html_session()
    listings: list[ShopListing] = []
    for page in range(1, _SINGLESTAR_MAX_PAGES + 1):
        if page > 1 and pacing_s:
            time.sleep(pacing_s)
        try:
            resp = session.get(SINGLESTAR_CATEGORY_URL, params={
                "num": str(_SINGLESTAR_PAGE_SIZE), "page": str(page),
            }, timeout=_SINGLESTAR_TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise ScraperFetchError(f"SingleStar sealed crawl failed (p{page}): {e}") from e
        batch = parse_singlestar_category_html(resp.content)
        listings.extend(batch)
        if len(batch) < _SINGLESTAR_PAGE_SIZE:
            break
    return listings


# --- Cardshop Serra (Shopify) ----------------------------------------------------

def serra_listings_from_products(products: list[dict]) -> list[ShopListing]:
    """One listing per product, priced at its NM variant. SP/MP variants are
    damaged boxes and are ignored; a product whose NM variant is sold out
    is kept (stock 0) so it still counts as matched."""
    out: list[ShopListing] = []
    for p in products:
        nm = next((v for v in p.get("variants") or [] if v.get("title") == "NM"), None)
        handle = p.get("handle")
        if not (nm and handle and p.get("title")):
            continue
        try:
            price = float(nm.get("price") or 0) or None
        except (TypeError, ValueError):
            continue
        out.append(ShopListing(
            title=p["title"],
            price_jpy=price,
            stock=None if nm.get("available") else 0,
            url=SERRA_PRODUCT_URL.format(handle=handle),
        ))
    return out


def crawl_serra(
    session: requests.Session | None = None,
    pacing_s: float = _SERRA_PACING_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ShopListing]:
    session = session or html_session()
    products: list[dict] = []
    for page in range(1, _SERRA_MAX_PAGES + 1):
        if page > 1 and pacing_s:
            sleep(pacing_s)
        params = {"limit": str(_SERRA_PAGE_SIZE), "page": str(page)}
        try:
            resp = session.get(SERRA_COLLECTION_URL, params=params, timeout=30)
            if resp.status_code == 429:
                wait = min(retry_after_s(resp), _SERRA_MAX_RETRY_WAIT_S)
                logger.info("event=sealed_serra_rate_limited page=%d wait_s=%.0f", page, wait)
                sleep(wait)
                resp = session.get(SERRA_COLLECTION_URL, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("products") or []
        except (requests.RequestException, ValueError) as e:
            raise ScraperFetchError(f"Serra sealed crawl failed (p{page}): {e}") from e
        products.extend(batch)
        if len(batch) < _SERRA_PAGE_SIZE:
            break
    return serra_listings_from_products(products)


# --- Hareruya buylist --------------------------------------------------------------

def parse_hareruya_buylist_html(html: str | bytes) -> list[ShopListing]:
    out: list[ShopListing] = []
    for cell in HTMLParser(html).css("ul.itemListLine li.itemList"):
        name_el = cell.css_first("a.itemName")
        price_el = cell.css_first("p.itemDetail__price")
        if not (name_el and price_el):
            continue
        digits = re.sub(r"[^\d]", "", price_el.text(strip=True))
        href = (name_el.attributes.get("href") or "").strip()
        out.append(ShopListing(
            title=name_el.text(strip=True),
            price_jpy=float(digits) if digits and int(digits) > 0 else None,
            stock=None,
            url=href if href.startswith("http") else f"{HARERUYA_BUY_BASE}{href}",
        ))
    return out


def crawl_hareruya_buylist(
    session: requests.Session | None = None, pacing_s: float = _PAGE_PACING_S,
) -> list[ShopListing]:
    session = session or html_session()
    listings: list[ShopListing] = []
    for category in HARERUYA_BUY_CATEGORIES:
        for page in range(1, _HARERUYA_BUY_MAX_PAGES + 1):
            if pacing_s and (page > 1 or listings):
                time.sleep(pacing_s)
            try:
                resp = session.get(HARERUYA_BUY_URL, params={
                    "category": category, "page": str(page),
                }, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise ScraperFetchError(f"Hareruya buylist crawl failed ({category} p{page}): {e}") from e
            batch = parse_hareruya_buylist_html(resp.content)
            listings.extend(batch)
            if len(batch) < _HARERUYA_BUY_PAGE_SIZE:
                break
    return listings


# --- matching ------------------------------------------------------------------

def hareruya_set_names(listings: list[ShopListing]) -> dict[str, str]:
    """``set_name_key → set code`` learned from Hareruya titles (shop or
    buylist: same shape)."""
    names: dict[str, str] = {}
    for listing in listings:
        parsed = jt.parse_hareruya_title(listing.title)
        if parsed and parsed.set_code:
            names.setdefault(jt.set_name_key(parsed.set_name), parsed.set_code)
    return names


def _parse(shop: str, title: str) -> jt.JpListing | None:
    if shop in (HARERUYA, HARERUYA_BUY):
        return jt.parse_hareruya_title(title)
    if shop == SERRA:
        return jt.parse_serra_title(title)
    return jt.parse_singlestar_title(title)


def best_offers(
    shop: str,
    listings: list[ShopListing],
    catalog_by_set: dict[str, list[dict]],
    set_names: dict[str, str],
) -> tuple[dict[tuple[str, str], ShopListing], dict[str, int]]:
    """Cheapest available listing per (product uuid, language), plus counts
    of what happened to the listings (for logs)."""
    english_index = jt.index_by_english_name(catalog_by_set) if shop == SERRA else {}
    best: dict[tuple[str, str], ShopListing] = {}
    stats = {"listings": len(listings), "parsed": 0, "matched": 0, "in_stock": 0}
    for listing in listings:
        parsed = _parse(shop, listing.title)
        if not parsed:
            continue
        stats["parsed"] += 1
        product = jt.match_english_name(parsed, english_index) if english_index else None
        if not product:
            code = parsed.set_code or (
                jt.resolve_set_code(parsed.set_name, set_names)
                or jt.english_set_code(parsed.set_name, catalog_by_set)
            )
            if not code and listing.hint_set_code in catalog_by_set:
                code = listing.hint_set_code
            product = jt.match_product(parsed, code, catalog_by_set) if code else None
        if not product:
            continue
        stats["matched"] += 1
        if not listing.available:
            continue
        stats["in_stock"] += 1
        key = (product["uuid"], parsed.language)
        if key not in best or (listing.price_jpy or 0) < (best[key].price_jpy or 0):
            best[key] = listing
    return best, stats
