"""MINT MALL (mint-mall.net) MTG scraper.

Multi-tenant marketplace running ec-cube. Listing titles encode set,
language, and variant inline::

    【<SET>】【<LANG>】[【<Foil…>】]〈<num-rarity>〉《<JP>/<EN>》[<variant suffix>]

Stock counts and per-spec prices are not in the listing HTML — they live
in a JS const ``specificationTreeSearchProductsTree`` at the top of the
page, keyed by ``<option value=...>`` of the per-product spec ``<select>``::

    {"<spec_id>": ["<stock>", <reserve>, <publish>, "<price_x100_tax_excl>"], ...}

We map each list card to the stock-map entry through the spec-id and
filter to English NM non-foil rows with stock > 0. A variant suffix
(``ボーダーレス版``, ``旧枠版``, ...) tags the row (see ``variants``)
rather than dropping it; no suffix is the regular printing. Per-spec price is recovered from the JSON (× 1.1 = tax-incl
price displayed on the page, rounded to whole yen as the page shows it).

Results come 100 to a page (Sol Ring: 152 items), followed via ``pageno=``
up to ``MAX_PAGES``.
"""
import json
import re

from selectolax.parser import HTMLParser

from .html_base import HtmlSearchScrapper, node_text_ws, to_usd
from .names import matched_name
from .variants import encode, normalize_number, tags_from_markers

BASE_URL = "https://www.mint-mall.net"
SEARCH_URL = f"{BASE_URL}/products/list.php"

# JS const containing the per-spec stock + price.
_STOCK_JSON_RE = re.compile(
    r"specificationTreeSearchProductsTree\s*=\s*(\{.*?\});",
    re.DOTALL,
)

# Title pattern. The first 【…】 is the set, the second is the language,
# optional 【Foil…】 between them is rejected upstream. The 〈…〉 card-number
# bracket is optional. After 》 may be a variant suffix (ショーケース版 etc.).
_TITLE_RE = re.compile(
    r"^"
    r"【(?P<set>[A-Z0-9]+)】"
    r"【(?P<lang>[^】]+)】"
    r"(?:〈(?P<num>[^〉-]+)[^〉]*〉)?"
    r"《(?P<jp>[^/]+?)/(?P<en>[^》]+?)》"
    r"(?P<suffix>.*)$"
)
# MINT MALL applies 10% consumption tax on top of the JSON's base price.
_TAX_MULTIPLIER = 1.10


def _stock_map(html: str | bytes) -> dict[str, dict]:
    """Public-ish wrapper: parse once and extract the stock map.

    Retained as a top-level helper because the test suite imports it.
    """
    return _stock_map_from_tree(HTMLParser(html))


def _stock_map_from_tree(tree: HTMLParser) -> dict[str, dict]:
    """Return spec_id → {stock, price_jpy} from the page's inline JS const.

    Finds the script tag containing ``specificationTreeSearchProductsTree``
    and regexes its text content — avoids a regex-on-full-HTML pass.
    """
    for script in tree.css("script"):
        body = script.text(deep=False)
        if "specificationTreeSearchProductsTree" not in body:
            continue
        m = _STOCK_JSON_RE.search(body)
        if not m:
            continue
        try:
            raw = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}
        out: dict[str, dict] = {}
        for spec_id, vals in raw.items():
            if not isinstance(vals, list) or len(vals) < 4:
                continue
            try:
                stock = int(vals[0])
                base_price = float(vals[3])
            except (TypeError, ValueError):
                continue
            out[str(spec_id)] = {
                "stock": stock,
                # The page shows whole yen (379.5 base x 1.1 -> "380円").
                "price_jpy": float(round(base_price * _TAX_MULTIPLIER)),
            }
        return out
    return {}


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English non-foil in-stock rows for ``card_name``."""
    tree = HTMLParser(html)
    stock = _stock_map_from_tree(tree)
    records: list[dict] = []

    for area in tree.css("div.list_area"):
        title_el = area.css_first("h4.recommend-title")
        link_el = area.css_first("a.thumbnail")
        select_el = area.css_first('select[name="specification"]')
        if not (title_el and select_el):
            continue

        title = node_text_ws(title_el)

        # Skip foils and special foils.
        if "【Foil】" in title or "Foil】" in title.split("》", 1)[0] + "》":
            continue

        m = _TITLE_RE.match(title)
        if not m:
            continue
        if m.group("lang") != "ENG":
            continue
        card = matched_name(m.group("en").strip(), card_name)
        if card is None:
            continue

        variant = encode(tags_from_markers([m.group("suffix")]))
        number = normalize_number(m.group("num"))

        # Each in-stock NM option contributes a record.
        for option in select_el.css("option"):
            spec_id = (option.attributes.get("value") or "").strip()
            cond_text = option.text(deep=True, separator=" ", strip=True)
            if not spec_id:
                continue
            # Accept "NM" and "NM〜NM-" (the shop's near-mint range bucket).
            if cond_text not in ("NM", "NM〜NM-"):
                continue
            entry = stock.get(spec_id)
            if entry is None or entry["stock"] <= 0:
                continue

            href = ((link_el.attributes.get("href") if link_el else "") or "").strip()
            link = href if href.startswith("http") else f"{BASE_URL}{href}"

            records.append({
                "shop": "MINT MALL",
                "card": card,
                "set": m.group("set"),
                "number": number,
                "variant": variant,
                "price_jpy": float(entry["price_jpy"]),
                "price_usd": to_usd(entry["price_jpy"], fx_jpy_per_usd),
                "stock": entry["stock"],
                "condition": "NM",
                "link": link,
            })
    return records


class MintMallScrapper(HtmlSearchScrapper):
    SHOP_NAME = "MINT MALL"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.mintmall"
    SEARCH_PARAM_NAME = "name"
    PAGE_PARAM = "pageno"

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
