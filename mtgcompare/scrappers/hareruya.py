"""Hareruya scraper.

Uses Hareruya's internal JSON search API + lazy render endpoint directly —
no browser, no Selenium. Two HTTP calls per search, so this scraper uses
its own class structure rather than the shared ``HtmlSearchScrapper``
base, but it still pulls ``USER_AGENT`` and ``make_session`` from
``_base`` for consistency.

The `parse_lazy_html` function is pure and is what the tests exercise.
"""
import logging
import re
from time import monotonic
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx
from ._base import RateLimitedError, ScraperFetchError
from ._base import make_session as _make_session

BASE_URL = "https://www.hareruyamtg.com"
UNISEARCH_API = f"{BASE_URL}/en/products/search/unisearch_api"
UNISEARCH_LAZY = f"{BASE_URL}/en/products/search/unisearch/lazy"

_NAME_SET_RE = re.compile(r"《(.+?)》.*?\[(.+?)]")
_STOCK_RE = re.compile(r"【(.+?) Stock:(\d+)】")
_PRICE_RE = re.compile(r"(\d[\d,]*)")


def make_session() -> requests.Session:
    return _make_session({"X-Requested-With": "XMLHttpRequest"})


def parse_lazy_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from the HTML returned by /unisearch/lazy.

    fx_jpy_per_usd: JPY per 1 USD (yfinance "JPY=X" previousClose).
    """
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    for item_data in soup.find_all("div", class_="itemData"):
        name_el = item_data.find(class_="itemName")
        price_el = item_data.find(class_="itemDetail__price")
        stock_el = item_data.find(class_="itemDetail__stock")
        if not (name_el and price_el and stock_el):
            continue

        name_match = _NAME_SET_RE.search(name_el.get_text())
        stock_match = _STOCK_RE.search(stock_el.get_text())
        price_match = _PRICE_RE.search(price_el.get_text().replace("¥", ""))
        if not (name_match and stock_match and price_match):
            continue

        card, mtg_set = name_match.group(1), name_match.group(2)
        if card.lower() != target:
            continue

        condition = stock_match.group(1)
        stock = int(stock_match.group(2))
        if stock <= 0:
            continue

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (name_el.get("href") or "").strip()
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
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("mtgcompare.scrappers.hareruya")

    def get_prices(self, card_name: str) -> list[dict]:
        # Both fetch helpers raise ScraperFetchError on transport failure.
        # We let it propagate so the cache layer doesn't poison the entry.
        t0 = monotonic()
        docs = self._fetch_docs(card_name)
        if not docs:
            self.logger.info(
                "event=shop_query shop='Hareruya' card=%r rows=0 duration_ms=%d",
                card_name, int((monotonic() - t0) * 1000),
            )
            return []
        html = self._fetch_lazy_html(docs)
        records = parse_lazy_html(html, card_name, self.fx)
        self.logger.info(
            "event=shop_query shop='Hareruya' card=%r rows=%d duration_ms=%d",
            card_name, len(records), int((monotonic() - t0) * 1000),
        )
        return records

    def _fetch_docs(self, card_name: str) -> list[dict]:
        params = {
            "kw": card_name,
            "fq.price": "1~*",
            "fq.foil_flg": "0",
            "fq.language": "2",
            "fq.stock": "1~*",
            "rows": "60",
            "page": "1",
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
            return resp.json().get("response", {}).get("docs", []) or []
        except ValueError as e:
            raise ScraperFetchError(f"Hareruya unisearch_api JSON decode failed: {e}") from e

    def _fetch_lazy_html(self, docs: list[dict]) -> str:
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
        return resp.text
