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
  【シルバースクロールFOIL】…  — special foil variant

Variant printings are kept and tagged from their trailing bracket —
``[ボーダーレス]``, ``[旧枠]``, ``[拡張アート版]`` (see ``variants``);
a listing without one is the regular printing.

Collector numbers ride in their own bracket before the rarity —
``Sol Ring[No.2807][無色P]【SLD】`` — so the name regex allows any number of
``[...]`` groups there; it used to allow one, which silently dropped every
in-stock Secret Lair-style English printing.

Search results come 48 to a page (Sol Ring: 4 pages), followed via
``page=`` up to ``MAX_PAGES``.

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import re

import requests
from selectolax.parser import HTMLParser

from .html_base import HtmlSearchScrapper, to_usd
from .names import matched_name
from .variants import encode, normalize_number, tags_from_markers

BASE_URL = "https://blackfrog.jp"
SEARCH_URL = f"{BASE_URL}/shop/shopbrand.html"

_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# The set bracket is the 【XYZ】 right after the rarity bracket; pin to an
# ASCII shape (letters/digits, e.g. SLD or MagicFest) so we don't accidentally
# match a Japanese label like 【FOIL】 or 【シルバースクロールFOIL】.
_NAME_RE = re.compile(
    r"【(?P<lang>[^】]+)】"
    r"(?P<jp>[^/]+?)"
    r"/"
    r"(?P<en>[^\[【]+?)"
    r"(?:\[[^\]]+\])*"          # [No.2807], rarity like [青MR], ...
    r"\s*【(?P<set>[A-Za-z0-9]+)】"
)
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_NUMBER_RE = re.compile(r"\[No\.\s*([^\]]+)\]")


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English non-foil rows for ``card_name`` from a BLACK FROG page."""
    tree = HTMLParser(html)
    records: list[dict] = []

    # `> li` restricts to direct children — the listing is one level deep,
    # nested ``<li>`` (e.g. inside a side-cart) shouldn't be picked up.
    for li in tree.css("ul.innerList > li"):
        name_el = li.css_first("p.name a")
        price_el = li.css_first("p.price")
        if not (name_el and price_el):
            continue

        # In stock = a "basket.html" link is rendered. Out-of-stock items
        # render only the detail link with no add-to-cart button.
        if li.css_first('a[href*="basket.html"]') is None:
            continue

        name = re.sub(
            r"\s+", " ",
            name_el.text(deep=True, separator=" ", strip=True),
        ).strip()

        # Filter NM only — the shop encodes condition as 状態XX in a leading
        # ★...★ flag. NM listings have no such flag.
        if "状態" in name:
            continue

        # Foil and special-foil variants
        if "【FOIL】" in name or "FOIL】" in name.split("】", 1)[0] + "】":
            continue

        m = _NAME_RE.search(name)
        if not m:
            continue
        if m.group("lang") != "英":
            continue

        en = matched_name(m.group("en").strip(), card_name)
        if en is None:
            continue

        price_match = _PRICE_RE.search(price_el.text(deep=True, separator=" ", strip=True))
        if not price_match:
            continue
        price_jpy = float(price_match.group(1).replace(",", ""))
        if price_jpy <= 0:
            continue

        href = (name_el.attributes.get("href") or "").strip()
        link = href if href.startswith("http") else f"{BASE_URL}{href}"

        number = _NUMBER_RE.search(name)
        records.append({
            "shop": "BLACK FROG",
            "card": en,
            "set": m.group("set"),
            "number": normalize_number(number.group(1)) if number else None,
            "variant": encode(tags_from_markers(_BRACKET_RE.findall(name))),
            "price_jpy": price_jpy,
            "price_usd": to_usd(price_jpy, fx_jpy_per_usd),
            "stock": None,  # BLACK FROG list view doesn't expose stock counts
            "condition": "NM",
            "link": link,
        })
    return records


class BlackFrogScrapper(HtmlSearchScrapper):
    SHOP_NAME = "BLACK FROG"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.blackfrog"
    SEARCH_PARAM_NAME = "search"
    PAGE_PARAM = "page"

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)

    def decode_response(self, resp: requests.Response) -> str:
        # The page is EUC-JP; the HTTP Content-Type often omits the charset
        # so requests would otherwise default to ISO-8859-1.
        return resp.content.decode("euc-jp", errors="replace")
