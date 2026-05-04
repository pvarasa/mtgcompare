"""MINT MALL (mint-mall.net) MTG scraper.

Multi-tenant marketplace running ec-cube. Listing titles encode set,
language, and variant inline::

    【<SET>】【<LANG>】[【<Foil…>】]〈<num-rarity>〉《<JP>/<EN>》[<variant suffix>]

Stock counts and per-spec prices are not in the listing HTML — they live
in a JS const ``specificationTreeSearchProductsTree`` at the top of the
page, keyed by ``<option value=...>`` of the per-product spec ``<select>``::

    {"<spec_id>": ["<stock>", <reserve>, <publish>, "<price_x100_tax_excl>"], ...}

We map each list card to the stock-map entry through the spec-id and
filter to plain English NM rows with stock > 0 (no foil, no variant
suffix). Per-spec price is recovered from the JSON (× 1.1 = tax-incl
price displayed on the page).
"""
import json
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..scrapper import MtgScrapper
from ..utils import get_fx

BASE_URL = "https://www.mint-mall.net"
SEARCH_URL = f"{BASE_URL}/products/list.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

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
    r"(?:〈[^〉]+〉)?"
    r"《(?P<jp>[^/]+?)/(?P<en>[^》]+?)》"
    r"(?P<suffix>.*)$"
)
# Variant suffixes that mean it's not the canonical printing.
_VARIANT_SUFFIXES = (
    "ショーケース版",
    "ボーダーレス版",
    "日本画版",
    "拡張枠版",
    "フレームレス版",
    "旧枠版",
)
# MINT MALL applies 10% consumption tax on top of the JSON's base price.
_TAX_MULTIPLIER = 1.10


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _stock_map(html: str) -> dict[str, dict]:
    """Return spec_id → {stock, price_jpy} from the page's JS const."""
    m = _STOCK_JSON_RE.search(html)
    if not m:
        return {}
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
            "price_jpy": round(base_price * _TAX_MULTIPLIER, 2),
        }
    return out


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English non-foil non-variant in-stock rows for ``card_name``."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    stock = _stock_map(html)
    records: list[dict] = []

    for area in soup.select("div.list_area"):
        title_el = area.select_one("h4.recommend-title")
        link_el = area.select_one("a.thumbnail")
        select_el = area.select_one('select[name="specification"]')
        if not (title_el and select_el):
            continue

        title = re.sub(r"\s+", " ", title_el.get_text(" ", strip=True)).strip()

        # Skip foils and special foils.
        if "【Foil】" in title or "Foil】" in title.split("》", 1)[0] + "》":
            continue

        m = _TITLE_RE.match(title)
        if not m:
            continue
        if m.group("lang") != "ENG":
            continue
        if m.group("en").strip().lower() != target:
            continue

        suffix = m.group("suffix").strip()
        if any(v in suffix for v in _VARIANT_SUFFIXES):
            continue

        # Each in-stock NM option contributes a record.
        for option in select_el.select("option"):
            spec_id = (option.get("value") or "").strip()
            cond_text = option.get_text(" ", strip=True)
            if not spec_id:
                continue
            # Accept "NM" and "NM〜NM-" (the shop's near-mint range bucket).
            if cond_text not in ("NM", "NM〜NM-"):
                continue
            entry = stock.get(spec_id)
            if entry is None or entry["stock"] <= 0:
                continue

            href = (link_el.get("href") if link_el else "") or ""
            href = href.strip()
            link = href if href.startswith("http") else f"{BASE_URL}{href}"

            records.append({
                "shop": "MINT MALL",
                "card": m.group("en").strip(),
                "set": m.group("set"),
                "price_jpy": float(entry["price_jpy"]),
                "price_usd": round(entry["price_jpy"] / fx_jpy_per_usd, 2),
                "stock": entry["stock"],
                "condition": "NM",
                "link": link,
            })
    return records


class MintMallScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("mintmall")

    def get_prices(self, card_name: str) -> list[dict]:
        html = self._fetch_search_html(card_name)
        if not html:
            return []
        records = parse_search_html(html, card_name, self.fx)
        if not records:
            self.logger.info(f"No MINT MALL results for {card_name!r}")
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
                params={"name": card_name},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            self.logger.error(f"MINT MALL search failed: {e}")
            return ""
