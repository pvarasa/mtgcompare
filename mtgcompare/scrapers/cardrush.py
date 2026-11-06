"""Card Rush (カードラッシュ) MTG scraper.

Card Rush runs on the ocnk.net e-commerce template (the same one SingleStar
uses), so the listing markup is structurally similar — but Card Rush exposes
graded conditions (NM/EX/PLD/HPLD/POOR), so name parsing has to filter
explicitly to NM stock.

Listing names follow the pattern:

    [<COND>]<JP-name>/<EN-name>《<LANG>》【<SET>】

where the condition bracket is omitted for NM, an optional ``☆SALE☆`` flag
may follow it (it used to swallow the tags after it into the JP name, so
``☆SALE☆(旧枠仕様)`` read as the regular printing), the leading ``(...)`` groups
hold finish and variant tags like ``(FOIL)``, ``(サージFOIL)``, ``(旧枠仕様)``
(old frame) or a collector number ``(1604)`` — several can stack — and the
search form may inject ``<span class="result_emphasis">`` highlights inside
the goods_name span. The parser strips those before matching.

Foils are rejected by any ``FOIL`` in those leading groups. The regex used
to allow a single optional group and never looked inside it, so
``(FOIL)Sol Ring《英語》`` parsed as a plain English listing: 10 of 32
records across four staples on 2026-10-02 were foil prices reported as
non-foil.

``available=1`` drops sold-out listings server-side (Sol Ring: 5 pages to
1), and pages are followed up to ``MAX_PAGES``.

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import re

from . import ocnk
from .html_base import HtmlSearchScrapper, node_text_ws, to_usd
from .names import matched_name
from .variants import encode, normalize_number, tags_from_markers

BASE_URL = "https://www.cardrush-mtg.jp"
SEARCH_URL = f"{BASE_URL}/product-list/0/0/photo"

# Listing name structure. Group meanings:
#   cond  : NM if absent; otherwise the bracketed grade (EX, PLD, HPLD, POOR, NM-, ...)
#   jp    : Japanese name (kept only to confirm the slash-separated bilingual format)
#   en    : English name (or the JP name again for Japanese-only listings)
#   lang  : 《英語》/《日本語》/...
#   set   : 3-char set code or longer set label (e.g. "Judge Promos")
_LISTING_RE = re.compile(
    r"^"
    r"(?:\[(?P<cond>[A-Z+\-]+)\])?"
    r"(?:[☆★][^☆★]*[☆★])?"       # sale flag, e.g. "☆SALE☆" — before the tags
    r"(?P<tags>(?:\([^)]*\))*)"   # finish/flavor groups: (FOIL)(1604)(旧枠仕様)
    r"(?P<jp>[^/]+?)"
    r"/"
    r"(?P<en>.+?)"
    r"《(?P<lang>[^》]+)》"
    r"【(?P<set>[^】]+)】"
    r"(?:\s*#(?P<num>\S+))?"      # trailing collector number, e.g. "#292"
    r"\s*$"
)

# Conditions Card Rush considers "near mint": absent prefix is NM by convention,
# [NM] is explicit NM, [NM-] is "near-mint with light handling" — the shop
# itself groups it with NM in their grading guide.
_NM_CONDITIONS = {None, "NM", "NM-"}

_TAG_RE = re.compile(r"\(([^)]*)\)")


def parse_search_html(html: str | bytes, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English price records for ``card_name`` from a Card Rush page."""
    records: list[dict] = []

    for cell in ocnk.iter_cells(html, BASE_URL):
        if cell.price_jpy is None or cell.stock <= 0:
            continue
        m = _LISTING_RE.match(node_text_ws(cell.name))
        if not m:
            continue

        if m.group("cond") not in _NM_CONDITIONS:
            continue
        if "FOIL" in m.group("tags").upper():
            continue
        if m.group("lang") != "英語":
            continue

        en_name = matched_name(m.group("en").strip(), card_name)
        if en_name is None:
            continue

        tags = _TAG_RE.findall(m.group("tags"))
        # A bare-digits tag is the collector number: "(1604)".
        number = m.group("num") or next((t for t in tags if t.isdigit()), None)
        records.append({
            "shop": "Card Rush",
            "card": en_name,
            "set": m.group("set").strip(),
            "number": normalize_number(number),
            "variant": encode(tags_from_markers(tags)),
            "price_jpy": cell.price_jpy,
            "price_usd": to_usd(cell.price_jpy, fx_jpy_per_usd),
            "stock": cell.stock,
            "condition": "NM",
            "link": cell.link,
        })
    return records


class CardRushScrapper(HtmlSearchScrapper):
    SHOP_NAME = "Card Rush"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrapers.cardrush"
    PAGE_PARAM = "page"

    def search_params(self, card_name: str) -> dict:
        return {"keyword": card_name, "available": "1", "num": "100"}

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
