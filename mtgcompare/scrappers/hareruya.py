"""Hareruya scraper.

Uses Hareruya's internal JSON search API + lazy render endpoint directly —
no browser, no Selenium. Two HTTP calls per search.

The `parse_lazy_html` function is pure and is what the tests exercise.
"""
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://www.hareruyamtg.com"
UNISEARCH_API = f"{BASE_URL}/en/products/search/unisearch_api"
UNISEARCH_LAZY = f"{BASE_URL}/en/products/search/unisearch/lazy"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_NAME_SET_RE = re.compile(r"《(.+?)》.*?\[(.+?)]")
_STOCK_RE = re.compile(r"【(.+?) Stock:(\d+)】")
_PRICE_RE = re.compile(r"(\d[\d,]*)")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


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
        self.logger = logging.getLogger("hareruya")

    def get_prices(self, card_name: str) -> list[dict]:
        docs = self._fetch_docs(card_name)
        if not docs:
            self.logger.info(f"No Hareruya results for {card_name!r}")
            return []
        html = self._fetch_lazy_html(docs)
        if not html:
            return []

        records = parse_lazy_html(html, card_name, self.fx)
        for r in records:
            self.logger.info(
                f"Found {r['card']} [{r['set']}] ¥{r['price_jpy']:.0f} "
                f"(${r['price_usd']:.2f}) cond={r['condition']} stock={r['stock']}"
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
            resp.raise_for_status()
            return resp.json().get("response", {}).get("docs", []) or []
        except (requests.RequestException, ValueError) as e:
            self.logger.error(f"unisearch_api failed: {e}")
            return []

    def _fetch_lazy_html(self, docs: list[dict]) -> str:
        payload: list[tuple[str, str]] = [("css", "itemList")]
        for i, d in enumerate(docs):
            for key, val in d.items():
                payload.append((f"docs[{i}][{key}]", str(val)))
        try:
            resp = self.session.post(UNISEARCH_LAZY, data=payload, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"unisearch/lazy failed: {e}")
            return ""
