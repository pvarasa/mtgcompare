"""TokyoMTG (tokyomtg.com) scraper.

TokyoMTG has no public API — site is bespoke PHP with server-rendered
HTML at `/cardpage.html?query=<name>&p=q`. Each printing is wrapped in a
`div.pwrapper` with Bootstrap nav-tabs for Regular/Played/Foil/PlayedFoil.
We only pull the Regular (NM) tab of English-version entries that have
stock.

The default `User-Agent` gets a 429 — callers must use `make_session()`
which sets a full browser header fingerprint.

The `parse_search_html` function is pure and is what tests exercise.
"""
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://tokyomtg.com"
SEARCH_URL = f"{BASE_URL}/cardpage.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# "¥14,990" — BS4 decodes the `&yen;` entity to the `¥` character.
_PRICE_RE = re.compile(r"¥\s*([\d,]+)")
# "Stock: 3".
_STOCK_RE = re.compile(r"Stock:\s*(\d+)")

ENGLISH_BADGE = "English Version"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from a TokyoMTG /cardpage.html response."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    for wrap in soup.select("div.pwrapper"):
        badge = wrap.select_one("span.lang-badge")
        if not badge or badge.get_text(strip=True) != ENGLISH_BADGE:
            continue

        info = wrap.select_one("div.col.mx-2")
        if not info:
            continue

        name_el = info.select_one("a > h3")
        set_el = info.select_one("h3 > a > b")
        detail_link_el = info.select_one("a[href*='carddetails.html']")
        if not (name_el and set_el and detail_link_el):
            continue

        card = name_el.get_text(strip=True)
        if card.lower() != target:
            continue

        # The first (active) tab-pane is always Regular/NM non-foil.
        reg_pane = wrap.select_one("div.tab-pane.show.active")
        if not reg_pane:
            continue
        price_el = reg_pane.select_one("h3.price-text")
        if not price_el:
            continue  # Out of stock — no price-text node.

        pane_text = price_el.get_text(" ", strip=True)
        price_match = _PRICE_RE.search(pane_text)
        stock_match = _STOCK_RE.search(pane_text)
        if not (price_match and stock_match):
            continue

        stock = int(stock_match.group(1))
        if stock <= 0:
            continue

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (detail_link_el.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

        records.append({
            "shop": "TokyoMTG",
            "card": card,
            "set": set_el.get_text(strip=True),
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "stock": stock,
            "condition": "NM",
            "link": link,
        })
    return records


class TokyoMtgScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("tokyomtg")

    def get_prices(self, card_name: str) -> list[dict]:
        html = self._fetch_search_html(card_name)
        if not html:
            return []
        records = parse_search_html(html, card_name, self.fx)
        if not records:
            self.logger.info(f"No TokyoMTG results for {card_name!r}")
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
                params={"query": card_name, "p": "q"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"TokyoMTG search failed: {e}")
            return ""
