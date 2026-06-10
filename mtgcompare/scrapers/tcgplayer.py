"""TCGPlayer ships-to-Japan scraper.

Answers "what would this card actually cost me, landed in Japan, from
TCGPlayer?" — a different number from the TCGPlayer *market price* the
Scryfall scraper reports. Market price is a US-centric sales aggregate;
only a small subset of sellers ship internationally, so the cheapest
JP-shippable offer routinely sits several dollars above it.

There is no documented source for this number. TCGPlayer closed its
official API to new developers after the eBay acquisition, and every
aggregate feed (Scryfall, MTGJSON, TCGCSV, paid third parties) carries
product-level prices only — no per-seller listings, no shipping
destination. The one source is the listings endpoint that powers the
product page itself:

    POST https://mp-search-api.tcgplayer.com/v1/product/{id}/listings

It is undocumented and 403s without a browser-credible Origin/Referer,
so treat it like any other shop scraper: canary-tested for drift,
wrapped in CachedScrapper, failing loudly via ScraperFetchError.

Flow per card:

  1. Resolve printings → TCGPlayer product ids via the same memoized
     Scryfall lookup the market scraper uses
     (``scryfall.fetch_card_summaries`` — one pagination per search,
     shared between the two shops).
  2. Cap to the cheapest few printings by market price.
  3. POST the listings endpoint per product, in parallel:
     shippingCountry=JP, Normal finish, English, NM/LP — matching the
     other shops' "plain English NM-ish" convention. Individual product
     failures are tolerated; the card only fails outright when no
     product produced rows and at least one fetch errored.
  4. Emit up to two records per product: the cheapest offer by item
     price and the cheapest by landed total (item + that seller's
     shipping) — different listings whenever cheap items hide behind
     expensive shipping. ``price_jpy`` is the item price, like every
     other shop; the offer's real shipping rides along as ``ship_jpy``,
     which the include-shipping sort uses instead of a flat per-shop
     estimate (the registry's marketplace flag pins that estimate to ¥0
     and keeps it out of the override UI).
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from urllib.parse import quote_plus

import requests

from ..utils import get_fx
from .base import MtgScrapper
from .html_base import (
    RateLimitedError,
    ScraperFetchError,
    decode_json_response,
    raise_for_response,
)
from .html_base import make_session as _make_session
from .scryfall import TCGPLAYER_PRODUCT_URL, fetch_card_summaries

SHOP_NAME = "TCGPlayer → JP"

LISTINGS_URL = "https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"

SHIP_COUNTRY = "JP"
_CONDITIONS = ("Near Mint", "Lightly Played")
_CONDITION_ABBR = {"Near Mint": "NM", "Lightly Played": "LP"}

# Popular cards have dozens of printings; one listings POST per printing
# per search would be slow and look like crawling. The cheapest few
# printings by market price are where the cheapest purchasable listing
# lives in practice.
_MAX_PRODUCTS_PER_CARD = 5
_PAGE_SIZE = 50


def make_session() -> requests.Session:
    # The endpoint rejects requests without a browser-credible
    # cross-origin context (403, no body); the base session's browser UA
    # plus these headers satisfies it.
    return _make_session({
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.tcgplayer.com",
        "Referer": "https://www.tcgplayer.com/",
    })


_SHARED_SESSION = make_session()


def build_listings_payload() -> dict:
    """Request body for the listings endpoint, filtered server-side."""
    return {
        "filters": {
            "term": {
                "sellerStatus": "Live",
                "channelId": 0,
                "condition": list(_CONDITIONS),
                "printing": ["Normal"],
                "language": ["English"],
            },
            "range": {"quantity": {"gte": 1}},
            "exclude": {"channelExclusion": 0},
        },
        "from": 0,
        "size": _PAGE_SIZE,
        "sort": {"field": "price+shipping", "order": "asc"},
        "context": {"shippingCountry": SHIP_COUNTRY, "cart": {}},
    }


def pick_best_listings(page: dict) -> list[dict]:
    """Best offers on a listings-response page: cheapest by item price
    and cheapest by landed total (item + that seller's shipping).

    Often the same listing (then one entry is returned), but cheap items
    routinely hide behind expensive international shipping, so each sort
    mode (with / without the include-shipping toggle) needs its own
    winner. The endpoint claims to sort by price+shipping, but the
    comparison is cheap and trusting the order is one more way drift
    could bite, so the minima are recomputed here. Empty when nothing
    ships.
    """
    results = page.get("results") or []
    listings = (results[0].get("results") or []) if results else []
    valid: list[tuple[float, float, dict]] = []
    for entry in listings:
        price = entry.get("price")
        ship = entry.get("shippingPrice") or 0.0
        if not isinstance(price, int | float) or not isinstance(ship, int | float):
            continue
        valid.append((float(price), float(price) + float(ship), entry))
    if not valid:
        return []
    by_item = min(valid, key=lambda v: (v[0], v[1]))[2]
    by_total = min(valid, key=lambda v: (v[1], v[0]))[2]
    return [by_item] if by_item is by_total else [by_item, by_total]


def build_record(
    listing: dict,
    *,
    card_name: str,
    set_code: str,
    product_id: int,
    fx_jpy_per_usd: float,
) -> dict:
    """Project one chosen listing into the shared record shape.

    ``price_jpy``/``price_usd`` is the item price, like every other
    shop; the seller's own shipping travels separately as ``ship_jpy``
    so the include-shipping sort can use the offer's real landed cost.
    """
    price_usd = float(listing["price"])
    ship_usd = float(listing.get("shippingPrice") or 0.0)
    qty = listing.get("quantity")
    condition = listing.get("condition") or ""
    # The product page hosts both finishes under one id and defaults to
    # an unfiltered listing mix (cheap foils float to the top), so
    # pre-filter the link to what this row actually is.
    link = (
        TCGPLAYER_PRODUCT_URL.format(product_id=product_id)
        + "?Language=English&Printing=Normal"
    )
    if condition:
        link += f"&Condition={quote_plus(condition)}"
    return {
        "shop": SHOP_NAME,
        "card": card_name,
        "set": set_code,
        "price_jpy": round(price_usd * fx_jpy_per_usd, 2),
        "price_usd": round(price_usd, 2),
        "ship_jpy": round(ship_usd * fx_jpy_per_usd, 2),
        "stock": int(qty) if isinstance(qty, int | float) else None,
        "condition": _CONDITION_ABBR.get(condition, condition),
        "link": link,
    }


class TcgPlayerJpScrapper(MtgScrapper):
    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session if session is not None else _SHARED_SESSION
        self.logger = logging.getLogger("mtgcompare.scrapers.tcgplayer")

    def get_prices(self, card_name: str) -> list[dict]:
        t0 = monotonic()
        printings = fetch_card_summaries(card_name)
        candidates = [p for p in printings if p.get("tcgplayer_id")]
        candidates.sort(key=lambda p: (p["usd"] is None, p["usd"] or 0.0))
        if len(candidates) > _MAX_PRODUCTS_PER_CARD:
            self.logger.info(
                "event=printings_capped shop='TCGPlayerJP' card=%r kept=%d dropped=%d",
                card_name, _MAX_PRODUCTS_PER_CARD,
                len(candidates) - _MAX_PRODUCTS_PER_CARD,
            )
            candidates = candidates[:_MAX_PRODUCTS_PER_CARD]

        records: list[dict] = []
        failures = 0
        last_error: ScraperFetchError | None = None
        if candidates:
            # The POSTs hit distinct product ids and are independent;
            # running them in parallel keeps the worst case at one
            # request's latency instead of the sum — the whole chain has
            # to fit the registry's per-shop wall-clock cap.
            with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
                futures = {
                    ex.submit(self._fetch_listings, p["tcgplayer_id"]): p
                    for p in candidates
                }
                for future, p in futures.items():
                    try:
                        page = future.result()
                    except RateLimitedError:
                        # Don't keep hammering a rate-limited endpoint
                        # for partial rows — surface it for backoff.
                        for f in futures:
                            f.cancel()
                        raise
                    except ScraperFetchError as exc:
                        failures += 1
                        last_error = exc
                        self.logger.warning(
                            "event=product_fetch_failed shop='TCGPlayerJP' "
                            "card=%r product_id=%s detail=%s",
                            card_name, p["tcgplayer_id"], exc,
                        )
                        continue
                    records.extend(build_record(
                        listing,
                        card_name=p["name"],
                        set_code=p["set"],
                        product_id=p["tcgplayer_id"],
                        fx_jpy_per_usd=self.fx,
                    ) for listing in pick_best_listings(page))

        # Partial product failures are tolerated — some rows beat none,
        # and the cache stores them. But zero rows with at least one
        # failure must propagate: caching "no listings" off a failed
        # fetch would mask purchasable stock for the whole cache TTL.
        if not records and last_error is not None:
            raise last_error

        self.logger.info(
            "event=shop_query shop='TCGPlayerJP' card=%r products=%d failed=%d "
            "rows=%d duration_ms=%d",
            card_name, len(candidates), failures, len(records),
            int((monotonic() - t0) * 1000),
        )
        return records

    def _fetch_listings(self, product_id: int) -> dict:
        url = LISTINGS_URL.format(product_id=product_id)
        try:
            resp = self.session.post(url, json=build_listings_payload(), timeout=20)
        except requests.RequestException as e:
            raise ScraperFetchError(f"TCGPlayer listings fetch failed: {e}") from e

        if resp.status_code == 403:
            raise ScraperFetchError(
                "TCGPlayer returned 403 — bot protection may have tightened"
            )
        raise_for_response(resp, "TCGPlayer")
        return decode_json_response(resp, "TCGPlayer")
