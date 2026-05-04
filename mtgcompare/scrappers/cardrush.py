"""Card Rush (カードラッシュ) MTG scraper.

Card Rush runs on the ocnk.net e-commerce template (the same one SingleStar
uses), so the listing markup is structurally similar — but Card Rush exposes
graded conditions (NM/EX/PLD/HPLD/POOR), so name parsing has to filter
explicitly to NM stock.

Listing names follow the pattern:

    [<COND>]<JP-name>/<EN-name>《<LANG>》【<SET>】

where the condition bracket is omitted for NM, the leading ``(...)`` group
holds variant flavor tags like ``(旧枠仕様)`` (old frame), and the search
form may inject ``<span class="result_emphasis">`` highlights inside the
goods_name span. The parser strips those before matching.

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import re

from bs4 import BeautifulSoup

from ._base import HtmlSearchScrapper

BASE_URL = "https://www.cardrush-mtg.jp"
SEARCH_URL = f"{BASE_URL}/product-list"

# Card Rush only exposes prices tax-included ((税込) badge alongside the figure).
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# Stock counter uses 枚 (mai); SingleStar's template uses 点 — same idea.
_STOCK_RE = re.compile(r"在庫数\s*(\d+)")

# Listing name structure. Group meanings:
#   cond  : NM if absent; otherwise the bracketed grade (EX, PLD, HPLD, POOR, NM-, ...)
#   jp    : Japanese name (kept only to confirm the slash-separated bilingual format)
#   en    : English name (or the JP name again for Japanese-only listings)
#   lang  : 《英語》/《日本語》/...
#   set   : 3-char set code or longer set label (e.g. "Judge Promos")
_LISTING_RE = re.compile(
    r"^"
    r"(?:\[(?P<cond>[A-Z+\-]+)\])?"
    r"(?:\([^)]*\))?"          # optional flavor bracket like (旧枠仕様)
    r"(?P<jp>[^/]+?)"
    r"/"
    r"(?P<en>.+?)"
    r"《(?P<lang>[^》]+)》"
    r"【(?P<set>[^】]+)】"
    r"\s*$"
)

# Conditions Card Rush considers "near mint": absent prefix is NM by convention,
# [NM] is explicit NM, [NM-] is "near-mint with light handling" — the shop
# itself groups it with NM in their grading guide.
_NM_CONDITIONS = {None, "NM", "NM-"}


def _goods_name_text(goods_name_el) -> str:
    """Concatenate text content of the goods_name span.

    BeautifulSoup's get_text() handles the nested ``<b>`` / ``<wbr/>`` /
    ``<span class="result_emphasis">`` tags automatically; this helper just
    centralises whitespace-collapsing for predictable matching.
    """
    return re.sub(r"\s+", " ", goods_name_el.get_text(" ", strip=True)).strip()


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English price records for ``card_name`` from a Card Rush page."""
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

        text = _goods_name_text(name_el)
        m = _LISTING_RE.match(text)
        if not m:
            continue

        if m.group("cond") not in _NM_CONDITIONS:
            continue
        if m.group("lang") != "英語":
            continue

        en_name = m.group("en").strip()
        if en_name.lower() != target:
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

        price_match = _PRICE_RE.search(price_el.get_text())
        if not price_match:
            continue
        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (link_el.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}{href}"

        records.append({
            "shop": "Card Rush",
            "card": en_name,
            "set": m.group("set").strip(),
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "stock": stock,
            "condition": "NM",
            "link": link,
        })
    return records


class CardRushScrapper(HtmlSearchScrapper):
    SHOP_NAME = "Card Rush"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "cardrush"

    def parse_html(self, html: str, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
