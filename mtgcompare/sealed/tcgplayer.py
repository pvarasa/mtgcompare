"""TCGPlayer market prices for sealed products.

Two undocumented endpoints the tcgplayer.com product page itself calls
(the same family as ``scrapers/tcgplayer.py``'s listings endpoint):

- ``mpapi.tcgplayer.com/v2/product/{id}/pricepoints`` — current market
  price (recent-sales based) and listed median, per printing.
- ``infinite-api.tcgplayer.com/price/history/{id}/detailed?range=…`` —
  market-price buckets: ``month`` is daily (30 points), ``annual`` is
  weekly (52 points). Used once per product to backfill a year of history.

Parsers are pure; the client only does I/O.
"""
from __future__ import annotations

from datetime import date

import requests

from ..scrapers.html_base import ScraperFetchError
from ..scrapers.tcgplayer import make_session

PRICEPOINTS_URL = "https://mpapi.tcgplayer.com/v2/product/{product_id}/pricepoints"
HISTORY_URL = "https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed"
PRODUCT_URL = "https://www.tcgplayer.com/product/{product_id}"

SOURCE = "tcgplayer"
# Ranges fetched for a backfill, coarse first so the daily buckets of the
# finer range overwrite the weekly ones on shared dates.
BACKFILL_RANGES = ("annual", "month")


def _positive(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_pricepoints(payload) -> float | None:
    """Market price of the Normal printing (sealed has no foil), else None."""
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, dict) and entry.get("printingType") == "Normal":
            return _positive(entry.get("marketPrice"))
    return None


def parse_history(payload) -> dict[date, float]:
    """``{bucket_start_date: market_price}`` from a price-history response.

    Takes the English/Unopened SKU when several are present; buckets
    without a positive market price are dropped.
    """
    results = (payload or {}).get("result") if isinstance(payload, dict) else None
    if not results:
        return {}
    sku = next(
        (r for r in results if r.get("language", "English") == "English"),
        results[0],
    )
    out: dict[date, float] = {}
    for bucket in sku.get("buckets") or []:
        price = _positive(bucket.get("marketPrice"))
        stamp = bucket.get("bucketStartDate")
        if price is None or not stamp:
            continue
        try:
            out[date.fromisoformat(stamp[:10])] = price
        except ValueError:
            continue
    return out


class TcgSealedClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 15):
        self.session = session or make_session()
        self.timeout = timeout

    def _get_json(self, url: str, params: dict | None = None):
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ScraperFetchError(f"TCGPlayer sealed fetch failed: {e}") from e
        if resp.status_code >= 400:
            raise ScraperFetchError(f"TCGPlayer sealed HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except ValueError as e:
            raise ScraperFetchError(f"TCGPlayer sealed JSON decode failed: {e}") from e

    def market_price(self, product_id: int) -> float | None:
        return parse_pricepoints(self._get_json(PRICEPOINTS_URL.format(product_id=product_id)))

    def history(self, product_id: int) -> dict[date, float]:
        merged: dict[date, float] = {}
        for rng in BACKFILL_RANGES:
            merged.update(parse_history(self._get_json(
                HISTORY_URL.format(product_id=product_id), {"range": rng},
            )))
        return merged
