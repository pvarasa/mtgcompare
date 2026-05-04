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
import re

import requests
from bs4 import BeautifulSoup

from ._base import HtmlSearchScrapper

BASE_URL = "https://blackfrog.jp"
SEARCH_URL = f"{BASE_URL}/shop/shopbrand.html"

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


class BlackFrogScrapper(HtmlSearchScrapper):
    SHOP_NAME = "BLACK FROG"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrappers.blackfrog"
    SEARCH_PARAM_NAME = "search"

    def parse_html(self, html: str, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)

    def decode_response(self, resp: requests.Response) -> str:
        # The page is EUC-JP; the HTTP Content-Type often omits the charset
        # so requests would otherwise default to ISO-8859-1.
        return resp.content.decode("euc-jp", errors="replace")
