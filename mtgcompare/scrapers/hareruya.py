"""Hareruya scraper.

Uses Hareruya's internal JSON search API + lazy render endpoint directly —
no browser, no Selenium. Two HTTP calls per search, so this scraper uses
its own class structure rather than the shared ``HtmlSearchScrapper``
base, but it still pulls ``USER_AGENT`` and ``make_session`` from
``_base`` for consistency.

The search API returns at most ``rows`` docs per call, so popular cards
are paged (Sol Ring: 172 matching docs on 2026-10-02, and only the first
60 were ever fetched) up to ``_MAX_PAGES``.

Item names are ``《Face》`` per face — ``《Delver of Secrets》/《Insectile
Aberration》``, ``《Fire+Ice》``, or a Secret Lair alternate name like
``《Vivi's Thunder Magic》//《Lightning Bolt》`` — so every bracketed face is
collected and matched with ``names.matched_name``; taking only the first
bracket made all of those miss.

The `parse_lazy_html` function is pure and is what the tests exercise.
"""
import logging
import re
from time import monotonic

import requests
from selectolax.parser import HTMLParser

from ..utils import get_fx
from .base import MtgScrapper
from .html_base import RateLimitedError, ScraperFetchError, to_usd
from .html_base import make_session as _make_session
from .names import matched_name, query_variants

BASE_URL = "https://www.hareruyamtg.com"
UNISEARCH_API = f"{BASE_URL}/en/products/search/unisearch_api"
UNISEARCH_LAZY = f"{BASE_URL}/en/products/search/unisearch/lazy"

_NAME_SET_RE = re.compile(r"《(.+?)》.*?\[(.+?)]")
_FACE_RE = re.compile(r"《(.+?)》")
_ROWS_PER_PAGE = 60
_MAX_PAGES = 3
_STOCK_RE = re.compile(r"【(.+?) Stock:(\d+)】")
_PRICE_RE = re.compile(r"(\d[\d,]*)")


def make_session() -> requests.Session:
    return _make_session({"X-Requested-With": "XMLHttpRequest"})


# Module-level Session shared across all HareruyaScrapper instances.
# Hareruya doesn't use HtmlSearchScrapper (it does two HTTP calls per
# search), so we wire the shared session manually rather than via the
# base class's __init_subclass__ hook.
_SHARED_SESSION = make_session()


def parse_lazy_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from the HTML returned by /unisearch/lazy.

    fx_jpy_per_usd: JPY per 1 USD (ECB daily reference rate via utils.get_fx).
    """
    tree = HTMLParser(html)
    records: list[dict] = []

    for item_data in tree.css("div.itemData"):
        name_el = item_data.css_first(".itemName")
        price_el = item_data.css_first(".itemDetail__price")
        stock_el = item_data.css_first(".itemDetail__stock")
        if not (name_el and price_el and stock_el):
            continue

        name_text = name_el.text(deep=True, separator=" ", strip=True)
        name_match = _NAME_SET_RE.search(name_text)
        stock_match = _STOCK_RE.search(stock_el.text(deep=True, separator=" ", strip=True))
        price_match = _PRICE_RE.search(
            price_el.text(deep=True, separator=" ", strip=True).replace("¥", ""),
        )
        if not (name_match and stock_match and price_match):
            continue

        # Every 《face》 before the set bracket, rejoined Scryfall-style.
        faces = _FACE_RE.findall(name_text.split("[", 1)[0])
        card = matched_name(" // ".join(faces), card_name)
        if card is None:
            continue
        mtg_set = name_match.group(2)

        condition = stock_match.group(1)
        stock = int(stock_match.group(2))
        if stock <= 0:
            continue

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = to_usd(price_jpy, fx_jpy_per_usd)

        href = (name_el.attributes.get("href") or "").strip()
        link = f"{BASE_URL}{href}" if href.startswith("/") else href

        records.append({
            "shop": "Hareruya",
            "card": card,
            "set": mtg_set,
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "stock": stock,
            "condition": condition,
            "link": link,
        })
    return records


class HareruyaScrapper(MtgScrapper):
    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session if session is not None else _SHARED_SESSION
        self.logger = logging.getLogger("mtgcompare.scrapers.hareruya")

    def get_prices(self, card_name: str) -> list[dict]:
        # Both fetch helpers raise ScraperFetchError on transport failure.
        # We let it propagate so the cache layer doesn't poison the entry.
        t0 = monotonic()
        records: list[dict] = []
        pages = 0
        for query in query_variants(card_name):
            records, n = self._collect(query, card_name)
            pages += n
            if records:
                break
        self.logger.info(
            "event=shop_query shop='Hareruya' card=%r rows=%d pages=%d duration_ms=%d",
            card_name, len(records), pages, int((monotonic() - t0) * 1000),
        )
        return records

    def _collect(self, query: str, card_name: str) -> tuple[list[dict], int]:
        """Page the search API for ``query``; one lazy render per page."""
        records: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            docs, num_found = self._fetch_docs(query, page)
            if docs:
                records.extend(parse_lazy_html(self._fetch_lazy_html(docs), card_name, self.fx))
            if not docs or page * _ROWS_PER_PAGE >= num_found:
                return records, page
        return records, _MAX_PAGES

    def _fetch_docs(self, card_name: str, page: int = 1) -> tuple[list[dict], int]:
        """One page of search docs, plus the total match count."""
        params = {
            "kw": card_name,
            "fq.price": "1~*",
            "fq.foil_flg": "0",
            "fq.language": "2",
            "fq.stock": "1~*",
            "rows": str(_ROWS_PER_PAGE),
            "page": str(page),
        }
        try:
            resp = self.session.get(UNISEARCH_API, params=params, timeout=20)
        except requests.RequestException as e:
            raise ScraperFetchError(f"Hareruya unisearch_api fetch failed: {e}") from e
        if resp.status_code == 429:
            raise RateLimitedError("Hareruya unisearch_api returned 429")
        if resp.status_code >= 400:
            raise ScraperFetchError(f"Hareruya unisearch_api HTTP {resp.status_code}")
        try:
            body = resp.json().get("response", {})
            docs = body.get("docs", []) or []
            return docs, int(body.get("numFound") or len(docs))
        except ValueError as e:
            raise ScraperFetchError(f"Hareruya unisearch_api JSON decode failed: {e}") from e

    def _fetch_lazy_html(self, docs: list[dict]) -> bytes:
        payload: list[tuple[str, str]] = [("css", "itemList")]
        for i, d in enumerate(docs):
            for key, val in d.items():
                payload.append((f"docs[{i}][{key}]", str(val)))
        try:
            resp = self.session.post(UNISEARCH_LAZY, data=payload, timeout=20)
        except requests.RequestException as e:
            raise ScraperFetchError(f"Hareruya unisearch/lazy fetch failed: {e}") from e
        if resp.status_code == 429:
            raise RateLimitedError("Hareruya unisearch/lazy returned 429")
        if resp.status_code >= 400:
            raise ScraperFetchError(f"Hareruya unisearch/lazy HTTP {resp.status_code}")
        return resp.content
