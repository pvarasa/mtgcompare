"""SingleStar (シングルスター) scraper.

SingleStar has no public API. The search page at
`/product-list?keyword=<name>` returns all matches server-rendered on a
single page (no pagination for typical card queries).

The `parse_search_html` function is pure and is what tests exercise.
"""
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://www.singlestar.jp"
SEARCH_URL = f"{BASE_URL}/product-list"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Set code + color/rarity bracket at the end of goods_name, e.g. "[SOA-青MR]".
_SET_RE = re.compile(r"\[([A-Z0-9]+)-[^\]]+\]\s*$")
# Price like "9,930円".
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# Stock count like "在庫数 4点".
_STOCK_RE = re.compile(r"在庫数\s*(\d+)")
# Variant/language/set brackets stripped to obtain the bare English card name.
_STRIP_BRACKETS_RE = re.compile(r"【[^】]*】|\([^)]*\)|\[[^\]]*\]|●")

ENGLISH_TAG = "【英語版】"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _clean_english_name(goods_text: str) -> Optional[str]:
    """Extract the bare English card name from a goods_name text.

    Returns None if the listing is a non-MTG product (no set bracket),
    a foil, a non-English language, or doesn't have a `JP/EN` name split.
    """
    text = goods_text.strip()
    if text.startswith("[FOIL]"):
        return None
    if ENGLISH_TAG not in text:
        return None
    if not _SET_RE.search(text):
        return None
    if "/" not in text:
        return None

    english_half = text.split("/", 1)[1]
    bare = _STRIP_BRACKETS_RE.sub("", english_half)
    return re.sub(r"\s+", " ", bare).strip()


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from a SingleStar /product-list HTML response."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    for cell in soup.select("li.list_item_cell"):
        name_el = cell.select_one("span.goods_name")
        price_el = cell.select_one("span.figure")
        stock_el = cell.select_one("p.stock")
        link_el = cell.select_one("a.item_data_link")
        if not (name_el and price_el and stock_el and link_el):
            continue

        goods_text = name_el.get_text(" ", strip=True)
        english_name = _clean_english_name(goods_text)
        if not english_name or english_name.lower() != target:
            continue

        set_match = _SET_RE.search(goods_text)
        price_match = _PRICE_RE.search(price_el.get_text())
        if not (set_match and price_match):
            continue

        stock_classes = stock_el.get("class") or []
        if "soldout" in stock_classes:
            continue
        stock_match = _STOCK_RE.search(stock_el.get_text())
        if not stock_match:
            continue
        stock = int(stock_match.group(1))
        if stock <= 0:
            continue

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (link_el.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}{href}"

        records.append({
            "shop": "SingleStar",
            "card": english_name,
            "set": set_match.group(1),
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "stock": stock,
            "condition": "NM",
            "link": link,
        })
    return records


class SingleStarScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("singlestar")

    def get_prices(self, card_name: str) -> list[dict]:
        html = self._fetch_search_html(card_name)
        if not html:
            return []
        records = parse_search_html(html, card_name, self.fx)
        if not records:
            self.logger.info(f"No SingleStar results for {card_name!r}")
        for r in records:
            self.logger.info(
                f"Found {r['card']} [{r['set']}] ¥{r['price_jpy']:.0f} "
                f"(${r['price_usd']:.2f}) stock={r['stock']}"
            )
        return records

    def _fetch_search_html(self, card_name: str) -> str:
        try:
            resp = self.session.get(
                SEARCH_URL,
                params={"keyword": card_name},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"SingleStar search failed: {e}")
            return ""
