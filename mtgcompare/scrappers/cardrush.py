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

from selectolax.parser import HTMLParser

from ._base import HtmlSearchScrapper, node_text_ws

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


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English price records for ``card_name`` from a Card Rush page."""
    tree = HTMLParser(html)
    target = card_name.strip().lower()
    records: list[dict] = []

    for cell in tree.css("li.list_item_cell"):
        name_el = cell.css_first("span.goods_name")
        price_el = cell.css_first("span.figure")
        stock_el = cell.css_first("p.stock")
        link_el = cell.css_first("a.item_data_link")
        if not (name_el and price_el and stock_el and link_el):
            continue

        text = node_text_ws(name_el)
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

        stock_class_attr = stock_el.attributes.get("class") or ""
        if "soldout" in stock_class_attr.split():
            continue
        stock_match = _STOCK_RE.search(stock_el.text(deep=True, separator=" ", strip=True))
        if not stock_match:
            continue
        stock = int(stock_match.group(1))
        if stock <= 0:
            continue

        price_match = _PRICE_RE.search(price_el.text(deep=True, separator=" ", strip=True))
        if not price_match:
            continue
        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (link_el.attributes.get("href") or "").strip()
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
    LOGGER_NAME = "mtgcompare.scrappers.cardrush"

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
