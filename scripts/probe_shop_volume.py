#!/usr/bin/env python
"""Listing-count probe — rank candidate shops by MTG inventory volume.

The shop-integration plan (``docs/shop_integration_plan.md``) ranks the
unintegrated shops by *e-commerce platform and scraping effort*, not by
*measured card volume*. Only the already-integrated shops carry inventory
figures (Serra ~1.19M, ENNDAL ~493k, …). This script fills that gap: it
hits each shop's real search endpoint for a small basket of bellwether
staples and counts how many listings come back, so the candidates can be
ranked the same way the integrated shops were — and read relative to them.

Methodology
-----------
For each (shop × bellwether card) we issue the shop's own search GET and
derive two breadth signals from the response:

* ``nodes`` — count of product-card nodes via a platform CSS selector
  (the same selectors the live scrapers use). This is the primary signal
  wherever a selector matches.
* ``set_tokens`` — regex count of set-code brackets (【SOA】, [2XM-…],
  (MH3-…)) in the page text. Platform-agnostic fallback for shops whose
  theme we don't have a selector for yet (BASE, Shopify, unknown ec-cube
  themes).

Per shop we aggregate over the basket: hit rate, median and sum of the
per-card listing counts, and the largest "N件" total the platform
reported. Shops are ranked by **median listings/card** (robust to one
staple being unusually deep), with the integrated shops included as
**calibration anchors** so a candidate's number can be read against a
shop whose true catalog size is known.

Caveats
-------
* This measures *search breadth for popular staples*, a proxy for catalog
  size — not a true inventory count, and not English-NM-in-stock depth
  (that needs the per-shop parser). A shop strong on JP-only stock can
  score high here yet be thin on the records we actually ingest; verify
  with a real scraper before committing. This is exactly the Toretoku
  trap the plan warns about.
* Politeness: small basket, one host per worker, a delay between a shop's
  own requests, a real UA, no retries. Don't crank ``--cards`` high.

Usage
-----
    uv run python scripts/probe_shop_volume.py
    uv run python scripts/probe_shop_volume.py --candidates-only --json out.json
    uv run python scripts/probe_shop_volume.py --only cardmax,suzunone --delay 1.0
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from selectolax.parser import HTMLParser

# Reuse the project UA / session so the probe looks like the real scrapers.
try:
    from mtgcompare.scrapers.html_base import USER_AGENT, make_session
except Exception:  # pragma: no cover - lets the script run standalone
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    def make_session(extra_headers=None):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        if extra_headers:
            s.headers.update(extra_headers)
        return s


# Bellwether basket: staples reprinted across many sets, so any shop with
# real MTG inventory surfaces a deep result list. Mix of formats/price
# tiers so a shop that only stocks cheap commons or only expensive
# Reserved-List cards still registers.
BELLWETHERS = [
    "Force of Will",
    "Brainstorm",
    "Lightning Bolt",
    "Counterspell",
    "Sol Ring",
    "Swords to Plowshares",
    "Birds of Paradise",
    "Llanowar Elves",
]

# Set-code bracket tokens: 【SOA】 / [2XM-青MR] / (MH3-R). Require a set-code
# shape (2-6 uppercase alnum) and drop obvious non-set words so 【FOIL】 etc.
# don't inflate the count.
_SET_TOKEN_RE = re.compile(r"[【\[(]([A-Z0-9]{2,6})(?:[-】\])])")
_SET_STOPWORDS = {"FOIL", "NM", "EX", "GD", "PLD", "PSA", "JP", "EN", "MR", "SR", "PR"}
# Platform "total results" counters: "全 1234 件" / "1234件中" / "1,234 件".
_TOTAL_RE = re.compile(r"([\d,]{1,9})\s*件")


@dataclass
class Shop:
    name: str
    platform: str
    # URL templates tried in order; first one returning HTTP 200 wins.
    # ``{q}`` is replaced with the URL-encoded card name.
    urls: list[str]
    # CSS selectors for product-card nodes, tried in order.
    selectors: list[str] = field(default_factory=list)
    encoding: str | None = None  # force-decode (e.g. "euc-jp" for ColorMe legacy)
    anchor: str | None = None    # known inventory, for integrated calibration shops
    note: str = ""


# Integrated shops (anchor) + plan candidates. Selectors mirror the live
# scrapers where one exists; same-platform candidates inherit the platform
# selector and fall back to the set-token regex when their theme differs.
# Selectors target the *search-result* product card on each platform's
# theme(s), verified live. ec-cube ships at least three themes: Serra's
# custom (``product-list__item``), the EC-CUBE 4 default shelf
# (``ec-shelfGrid__item``), and MINT MALL's legacy (``list_product``).
# ColorMe likewise: BLACK FROG's legacy ``innerList`` vs the modern
# ``productlist_list``. Deliberately NOT generic ``li.list`` / ``.item_name``
# — those match the sidebar/footer "recommended/new items" rails that every
# JP storefront renders, which over-counts the true result set by 10-45×.
_OCNK = ["li.list_item_cell", "div.list_item_cell"]
_ECCUBE = ["div.product-list__item", "li.ec-shelfGrid__item", ".list_product"]
_COLORME = ["ul.innerList > li", ".productlist_list", ".item_box"]

SHOPS: list[Shop] = [
    # --- integrated: calibration anchors -------------------------------
    Shop("Cardshop Serra", "ec-cube", ["https://cardshop-serra.com/mtg/products/list?name={q}"],
         _ECCUBE, anchor="~1.19M"),
    Shop("ENNDAL GAMES", "custom", ["https://www.enndalgames.com/products/list.php?mode=search&name={q}"],
         ["div.product_detail_wrapper"], anchor="~493k"),
    Shop("BLACK FROG", "colorme", ["https://blackfrog.jp/shop/shopbrand.html?search={q}"],
         _COLORME, encoding="euc-jp", anchor="~119k"),
    Shop("MINT MALL", "ec-cube", ["https://www.mint-mall.net/products/list.php?name={q}"],
         _ECCUBE, anchor="~90k"),
    Shop("Card Rush", "ocnk", ["https://www.cardrush-mtg.jp/product-list?keyword={q}"],
         _OCNK, anchor="(integrated)"),
    Shop("SingleStar", "ocnk", ["https://www.singlestar.jp/product-list?keyword={q}"],
         _OCNK, anchor="(integrated)"),
    # --- Tier 1 candidates: ocnk.net -----------------------------------
    Shop("F-conclave", "ocnk", ["https://www.f-conclave.net/product-list?keyword={q}"], _OCNK),
    Shop("Genki302", "ocnk", ["https://www.genki302.com/product-list?keyword={q}"], _OCNK),
    Shop("Kurowaku", "ocnk", ["https://www.kurowaku.com/product-list?keyword={q}"], _OCNK),
    Shop("Todo", "ocnk", ["https://todo.ocnk.net/product-list?keyword={q}"], _OCNK),
    # --- Tier 1 candidates: ec-cube ------------------------------------
    Shop("Manzokuya", "ec-cube", ["https://shopmanzokuya.com/products/list?name={q}"], _ECCUBE),
    Shop("Nukenin", "ec-cube", ["https://nukeninmtg.com/products/list?name={q}"], _ECCUBE),
    # --- Tier 1 candidates: ColorMe (try modern then legacy URL) -------
    Shop("CARDMAX", "colorme",
         ["https://www.cardmax.jp/?mode=srh&keyword={q}",
          "https://www.cardmax.jp/shop/shopbrand.html?search={q}"],
         _COLORME, encoding="euc-jp"),
    Shop("Gemutlich", "colorme",
         ["https://www.mtg-gemutlich.shop/?mode=srh&keyword={q}",
          "https://www.mtg-gemutlich.shop/shop/shopbrand.html?search={q}"],
         _COLORME, encoding="euc-jp"),
    Shop("Suzunone", "colorme", ["https://tcgshop-suzunone.com/?mode=srh&keyword={q}"],
         _COLORME, encoding="euc-jp"),
    Shop("Hamaya", "colorme", ["https://www.cardshophamaya.com/?mode=srh&keyword={q}"],
         _COLORME, encoding="euc-jp"),
    # --- Tier 1 candidates: other platforms ----------------------------
    Shop("Takaoka (BASE)", "base", ["https://shop.takaoka-sc.com/items?q={q}"],
         [".item", "li.p-item", ".items-grid_item"]),
    Shop("GOODGAME (Shopify)", "shopify", ["https://goodgame.co.jp/search?q={q}"],
         ["li.grid__item", ".product-card", ".card-wrapper", ".grid-product"]),
]


@dataclass
class CardResult:
    card: str
    http: int | None = None
    nodes: int = 0          # result-card nodes the selector matched
    relevant: int = 0       # of those, how many actually name this card
    set_tokens: int = 0
    reported_total: int | None = None
    selector: str | None = None
    url: str | None = None
    error: str | None = None

    @property
    def listings(self) -> int:
        """Breadth estimate — *selector node-count only*.

        The set-token regex is kept (``set_tokens``) as a diagnostic but is
        deliberately NOT used here: it counts every 【SET】 in the page,
        including the sidebar/footer recommendation rails, so it over-counts
        the real result set by 10-45× and isn't comparable across shops. A
        shop whose theme we have no selector for scores 0 — the safe
        direction (don't overclaim); high ``set_tokens`` with 0 ``nodes``
        flags "page rendered but no result-card match" for follow-up.

        Counts only *relevant* cards — result nodes whose title actually
        names the queried card. JP shops title cards bilingually
        (《渦まく知識/Brainstorm(SET)》英語), so a substring match weeds out
        shops whose search is fuzzy/category-wide and returns unrelated MTG
        cards (e.g. BASE's ``/items?q=`` returns a whole MTG grid)."""
        return self.relevant


def _count_tokens(text: str) -> int:
    return sum(
        1 for m in _SET_TOKEN_RE.finditer(text) if m.group(1) not in _SET_STOPWORDS
    )


def _reported_total(text: str) -> int | None:
    best = None
    for m in _TOTAL_RE.finditer(text):
        n = int(m.group(1).replace(",", ""))
        if best is None or n > best:
            best = n
    return best


def probe_card(session: requests.Session, shop: Shop, card: str, timeout: float) -> CardResult:
    res = CardResult(card=card)
    last_exc = None
    for tmpl in shop.urls:
        url = tmpl.replace("{q}", requests.utils.quote(card))
        res.url = url
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            last_exc = str(e)
            continue
        res.http = resp.status_code
        if resp.status_code != 200:
            continue
        html = resp.content.decode(shop.encoding, errors="replace") if shop.encoding else resp.content
        tree = HTMLParser(html)
        text = tree.body.text(deep=True, separator=" ", strip=True) if tree.body else ""
        # Whitespace-insensitive needle: ocnk search wraps the matched
        # keyword in <span class="result_emphasis">, so a naive spaced
        # render splits "Brainstorm" into "Brain storm". Strip all
        # whitespace from both sides before the substring test.
        needle = re.sub(r"\s+", "", card.lower())
        for sel in shop.selectors:
            matched = tree.css(sel)
            if matched:
                res.nodes, res.selector = len(matched), sel
                res.relevant = sum(
                    1 for node in matched
                    if needle in re.sub(r"\s+", "", node.text(deep=True, strip=True).lower())
                )
                break
        res.set_tokens = _count_tokens(text)
        res.reported_total = _reported_total(text)
        return res
    res.error = last_exc or f"all URLs non-200 (last http={res.http})"
    return res


@dataclass
class ShopReport:
    shop: Shop
    cards: list[CardResult]

    @property
    def hits(self) -> int:
        return sum(1 for c in self.cards if c.listings > 0)

    @property
    def hit_rate(self) -> float:
        return self.hits / len(self.cards) if self.cards else 0.0

    @property
    def median_listings(self) -> float:
        vals = [c.listings for c in self.cards]
        return statistics.median(vals) if vals else 0.0

    @property
    def sum_listings(self) -> int:
        return sum(c.listings for c in self.cards)

    @property
    def max_reported_total(self) -> int | None:
        totals = [c.reported_total for c in self.cards if c.reported_total]
        return max(totals) if totals else None

    @property
    def errors(self) -> list[str]:
        return sorted({c.error for c in self.cards if c.error})

    @property
    def flag(self) -> str:
        """Short human note explaining a low/zero score."""
        if self.errors:
            return self.errors[0][:34]
        if self.median_listings > 0:
            return ""
        # Zero *relevant* results. Two distinct causes:
        med_nodes = statistics.median([c.nodes for c in self.cards]) if self.cards else 0
        med_tok = statistics.median([c.set_tokens for c in self.cards]) if self.cards else 0
        if med_nodes > 0:
            # Cards matched a selector but none named the query → fuzzy /
            # category-wide search (e.g. BASE). Real depth unmeasured here.
            return f"fuzzy search: {med_nodes:.0f} cards/page, 0 named the query"
        if med_tok > 0:
            # Page rendered with set-token content but no result-card match →
            # English search returns no parseable products (needs JP-name table).
            return f"no card-node match (tok~{med_tok:.0f}); JP-name search?"
        return ""


def probe_shop(shop: Shop, cards: Sequence[str], timeout: float, delay: float) -> ShopReport:
    session = make_session({"User-Agent": USER_AGENT})
    results = []
    for i, card in enumerate(cards):
        if i and delay:
            time.sleep(delay)
        results.append(probe_card(session, shop, card, timeout))
    return ShopReport(shop=shop, cards=results)


def render_table(reports: list[ShopReport]) -> str:
    # Rank candidates by median listings/card; anchors sort in their own
    # group at the top so the candidate numbers can be read against them.
    anchors = [r for r in reports if r.shop.anchor]
    cands = [r for r in reports if not r.shop.anchor]
    anchors.sort(key=lambda r: r.median_listings, reverse=True)
    cands.sort(key=lambda r: (r.median_listings, r.sum_listings), reverse=True)

    hdr = f"{'shop':<20} {'platform':<8} {'hit':>4} {'med':>6} {'sum':>6} {'maxN件':>8} {'known':>9}  notes"
    lines = [hdr, "-" * len(hdr)]

    def row(r: ShopReport) -> str:
        note = r.flag
        total = str(r.max_reported_total) if r.max_reported_total is not None else "-"
        return (
            f"{r.shop.name:<20} {r.shop.platform:<8} "
            f"{r.hits}/{len(r.cards):<2} {r.median_listings:>6.0f} {r.sum_listings:>6} "
            f"{total:>8} {(r.shop.anchor or ''):>9}  {note}"
        )

    lines.append("# integrated (calibration anchors)")
    lines += [row(r) for r in anchors]
    lines.append("# candidates (ranked by median listings/card)")
    lines += [row(r) for r in cands]
    return "\n".join(lines)


def to_json(reports: list[ShopReport]) -> list[dict]:
    return [
        {
            "shop": r.shop.name,
            "platform": r.shop.platform,
            "anchor_known_inventory": r.shop.anchor,
            "hit_rate": round(r.hit_rate, 2),
            "median_listings": r.median_listings,
            "sum_listings": r.sum_listings,
            "max_reported_total": r.max_reported_total,
            "errors": r.errors,
            "cards": [
                {
                    "card": c.card,
                    "http": c.http,
                    "listings": c.listings,
                    "nodes": c.nodes,
                    "relevant": c.relevant,
                    "set_tokens": c.set_tokens,
                    "reported_total": c.reported_total,
                    "selector": c.selector,
                    "url": c.url,
                    "error": c.error,
                }
                for c in r.cards
            ],
        }
        for r in reports
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cards", type=int, default=len(BELLWETHERS),
                    help="how many bellwether staples to probe (default: all)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between a shop's own requests (default: 0.5)")
    ap.add_argument("--timeout", type=float, default=20.0, help="per-request timeout seconds (default: 20)")
    ap.add_argument("--workers", type=int, default=8, help="concurrent shops (one host each) (default: 8)")
    ap.add_argument("--only", default="", help="comma-separated shop name substrings to include")
    ap.add_argument("--candidates-only", action="store_true", help="skip the integrated calibration anchors")
    ap.add_argument("--json", dest="json_out", default="", help="write full per-card results to this JSON path")
    args = ap.parse_args(argv)

    cards = BELLWETHERS[: args.cards]
    shops = SHOPS
    if args.candidates_only:
        shops = [s for s in shops if not s.anchor]
    if args.only:
        wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        shops = [s for s in shops if any(w in s.name.lower() for w in wanted)]
    if not shops:
        print("no shops matched --only filter", file=sys.stderr)
        return 2

    print(f"probing {len(shops)} shops × {len(cards)} cards "
          f"(delay={args.delay}s, timeout={args.timeout}s)…", file=sys.stderr)
    reports: list[ShopReport] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe_shop, s, cards, args.timeout, args.delay): s for s in shops}
        for fut in as_completed(futs):
            shop = futs[fut]
            try:
                reports.append(fut.result())
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"  {shop.name}: probe crashed: {e}", file=sys.stderr)

    print()
    print(render_table(reports))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(to_json(reports), f, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
