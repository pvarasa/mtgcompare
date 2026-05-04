"""BLACK FROG (blackfrog.jp) MTG scraper.

ColorMe Shop installation served as EUC-JP. Search lives at the legacy
``/shop/shopbrand.html?search=…`` URL.

Listing-name format::

    [<sale-or-condition prefix>][<【FOIL】>]【<lang>】<JP>/<EN>[<rarity>]【<SET>】[<variant…>]

Example NM English Force of Will::

    【英】意志の力/Force of Will[青MR]【SOA】

Examples we filter out:

  ★特価品　状態EX★【英】…   — non-NM (condition stamped in 状態XX prefix)
  【FOIL】【英】…              — foil printing
  【日】…                       — Japanese
  …[ボーダーレス]              — borderless variant
  …[旧枠]                       — old-frame variant
  …[日本画]                     — Japanese-art variant
  【シルバースクロールFOIL】…  — special foil variant

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://blackfrog.jp"
SEARCH_URL = f"{BASE_URL}/shop/shopbrand.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# The set bracket is the 【XYZ】 right after the rarity bracket; pin to the
# set-code shape (uppercase ASCII letters/digits) so we don't accidentally
# match a Japanese label like 【FOIL】 or 【シルバースクロールFOIL】.
_NAME_RE = re.compile(
    r"【(?P<lang>[^】]+)】"
    r"(?P<jp>[^/]+?)"
    r"/"
    r"(?P<en>[^\[【]+?)"
    r"(?:\[[^\]]+\])?"          # optional rarity bracket like [青MR]
    r"\s*【(?P<set>[A-Z0-9]+)】"
)
_VARIANT_BRACKETS = ("[ボーダーレス]", "[旧枠]", "[日本画]", "[拡張枠]", "[フレームレス]")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English non-foil non-variant rows for ``card_name`` from a BLACK FROG page."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    items_root = soup.select_one("ul.innerList")
    if items_root is None:
        return records

    for li in items_root.find_all("li", recursive=False):
        name_el = li.select_one("p.name a")
        price_el = li.select_one("p.price")
        if not (name_el and price_el):
            continue

        # In stock = a "basket.html" link is rendered. Out-of-stock items
        # render only the detail link with no add-to-cart button.
        in_stock = li.find("a", href=lambda h: bool(h) and "basket.html" in h) is not None
        if not in_stock:
            continue

        name = re.sub(r"\s+", " ", name_el.get_text(" ", strip=True)).strip()

        # Filter NM only — the shop encodes condition as 状態XX in a leading
        # ★...★ flag. NM listings have no such flag.
        if "状態" in name:
            continue

        # Foil and special-foil variants
        if "【FOIL】" in name or "FOIL】" in name.split("】", 1)[0] + "】":
            continue
        if any(v in name for v in _VARIANT_BRACKETS):
            continue

        m = _NAME_RE.search(name)
        if not m:
            continue
        if m.group("lang") != "英":
            continue

        en = m.group("en").strip()
        if en.lower() != target:
            continue

        price_match = _PRICE_RE.search(price_el.get_text())
        if not price_match:
            continue
        price_jpy = float(price_match.group(1).replace(",", ""))
        if price_jpy <= 0:
            continue

        href = (name_el.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}{href}"

        records.append({
            "shop": "BLACK FROG",
            "card": en,
            "set": m.group("set"),
            "price_jpy": price_jpy,
            "price_usd": round(price_jpy / fx_jpy_per_usd, 2),
            "stock": None,  # BLACK FROG list view doesn't expose stock counts
            "condition": "NM",
            "link": link,
        })
    return records


class BlackFrogScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("blackfrog")

    def get_prices(self, card_name: str) -> list[dict]:
        html = self._fetch_search_html(card_name)
        if not html:
            return []
        records = parse_search_html(html, card_name, self.fx)
        if not records:
            self.logger.info(f"No BLACK FROG results for {card_name!r}")
        for r in records:
            self.logger.info(
                f"Found {r['card']} [{r['set']}] ¥{r['price_jpy']:.0f} "
                f"(${r['price_usd']:.2f})"
            )
        return records

    def _fetch_search_html(self, card_name: str) -> str:
        try:
            resp = self.session.get(
                SEARCH_URL,
                params={"search": card_name},
                timeout=20,
            )
            resp.raise_for_status()
            # The page is EUC-JP; let requests autodetect from the response,
            # but coerce explicitly because a few servers misreport it.
            resp.encoding = resp.encoding or "EUC-JP"
            if (resp.encoding or "").lower().replace("-", "") in ("euc_jp", "eucjp", "iso88591"):
                return resp.content.decode("euc-jp", errors="replace")
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"BLACK FROG search failed: {e}")
            return ""
