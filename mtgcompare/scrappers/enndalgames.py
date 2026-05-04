"""ENNDAL GAMES (enndalgames.com) MTG scraper.

Custom server-rendered shop. Each product card lives in a
``div.product_detail_wrapper`` and exposes:

- ``a.product_name`` whose text follows the format
  ``(<SET>-<RARITY>)<EN>/<JP>[【No.<num>】]`` for plain printings, or
  prepends one or more ``【...】`` brackets for variants
  (``【Foil】``, ``【日本画】``, ``【旧枠】``, ``【PSA10】``, …).
- ``table.item_stock_table`` with one row per (language, condition):
  ``<tr><th>English NM</th><td><span class="price">15,999 yen</span>
  <span class="quantity">(3)</span></td></tr>``.

We accept only the plain English-NM rows (no leading ``【...】`` prefix,
no multi-segment set codes like ``2XM-Box_Topper-MU``) so the records
match the unembellished printings from other shops.
"""
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://www.enndalgames.com"
SEARCH_URL = f"{BASE_URL}/products/list.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Title pattern. Set is the chunk before the rarity suffix; we deliberately
# *don't* match titles whose set code has additional dashes (those are box
# toppers / sub-variants and shouldn't be conflated with the regular printing).
_TITLE_RE = re.compile(
    r"^\((?P<set>[A-Z0-9]+)-[A-Z]+\)"
    r"(?P<en>[^/]+?)"
    r"/"
    r"(?P<jp>[^【]+?)"
    r"(?:【No\.[\w\-]+】)?"
    r"\s*$"
)
_PRICE_RE = re.compile(r"([\d,]+)\s*yen")
_QTY_RE = re.compile(r"\((\d+)\)")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract English-NM in-stock rows for ``card_name`` from an ENNDAL page."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    for wrapper in soup.select("div.product_detail_wrapper"):
        name_el = wrapper.select_one("a.product_name")
        if name_el is None:
            continue

        title = name_el.get_text(strip=True)
        # Variant prefixes (Foil, 日本画, 旧枠, PSA10, ...) — skip; we only want
        # the canonical printing.
        if title.startswith("【"):
            continue

        m = _TITLE_RE.match(title)
        if not m:
            continue

        en = m.group("en").strip()
        if en.lower() != target:
            continue

        link = (name_el.get("href") or "").strip()
        if link and not link.startswith("http"):
            link = f"{BASE_URL}{link}"

        table = wrapper.select_one("table.item_stock_table")
        if table is None:
            continue

        for row in table.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if not (th and td):
                continue
            if th.get_text(strip=True) != "English NM":
                continue

            price_el = td.select_one("span.price")
            qty_el = td.select_one("span.quantity")
            if not (price_el and qty_el):
                continue

            qty_match = _QTY_RE.search(qty_el.get_text())
            if not qty_match:
                continue
            stock = int(qty_match.group(1))
            if stock <= 0:
                continue

            price_match = _PRICE_RE.search(price_el.get_text())
            if not price_match:
                continue
            price_jpy = float(price_match.group(1).replace(",", ""))
            if price_jpy <= 0:
                continue

            records.append({
                "shop": "ENNDAL GAMES",
                "card": en,
                "set": m.group("set"),
                "price_jpy": price_jpy,
                "price_usd": round(price_jpy / fx_jpy_per_usd, 2),
                "stock": stock,
                "condition": "NM",
                "link": link,
            })
    return records


class EnndalGamesScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("enndalgames")

    def get_prices(self, card_name: str) -> list[dict]:
        html = self._fetch_search_html(card_name)
        if not html:
            return []
        records = parse_search_html(html, card_name, self.fx)
        if not records:
            self.logger.info(f"No ENNDAL GAMES results for {card_name!r}")
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
                params={"mode": "search", "name": card_name},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"ENNDAL GAMES search failed: {e}")
            return ""
