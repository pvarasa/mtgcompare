"""SingleStar (シングルスター) scraper.

SingleStar has no public API. The search page at
`/product-list?keyword=<name>` returns all matches server-rendered on a
single page (no pagination for typical card queries).

The `parse_search_html` function is pure and is what tests exercise.
"""
import re

from selectolax.parser import HTMLParser

from ._base import HtmlSearchScrapper

BASE_URL = "https://www.singlestar.jp"
SEARCH_URL = f"{BASE_URL}/product-list"

# Set code + color/rarity bracket at the end of goods_name, e.g. "[SOA-青MR]".
_SET_RE = re.compile(r"\[([A-Z0-9]+)-[^\]]+\]\s*$")
# Price like "9,930円".
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# Stock count like "在庫数 4点".
_STOCK_RE = re.compile(r"在庫数\s*(\d+)")
# Variant/language/set brackets stripped to obtain the bare English card name.
_STRIP_BRACKETS_RE = re.compile(r"【[^】]*】|\([^)]*\)|\[[^\]]*\]|●")

ENGLISH_TAG = "【英語版】"


def _clean_english_name(goods_text: str) -> str | None:
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


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract price records from a SingleStar /product-list HTML response."""
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

        goods_text = name_el.text(deep=True, separator=" ", strip=True)
        english_name = _clean_english_name(goods_text)
        if not english_name or english_name.lower() != target:
            continue

        set_match = _SET_RE.search(goods_text)
        price_match = _PRICE_RE.search(price_el.text(deep=True, separator=" ", strip=True))
        if not (set_match and price_match):
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

        price_jpy = float(price_match.group(1).replace(",", ""))
        price_usd = round(price_jpy / fx_jpy_per_usd, 2)

        href = (link_el.attributes.get("href") or "").strip()
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


class SingleStarScrapper(HtmlSearchScrapper):
    SHOP_NAME = "SingleStar"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrappers.singlestar"

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
