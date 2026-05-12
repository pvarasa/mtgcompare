"""Scryfall scraper.

Uses Scryfall's public REST API (https://scryfall.com/docs/api) to look up
per-printing USD prices. Scryfall's `prices.usd` reflects the TCGPlayer
market price — the de-facto US "what does this card cost" reference.

No auth. Respects Scryfall's rate-limit guidance (50-100 ms between calls).

The `parse_page` / `parse_search_response` functions are pure and are
what tests exercise. ``parse_page`` is the per-page primitive so the
scrapper can stream-process and drop each page without accumulating
the full pagination history in memory — popular cards with many
printings produce multi-MB JSON bodies and were the largest single
contributor to /decklist's peak RSS.
"""
import logging
import time
from collections.abc import Iterator
from time import monotonic

import orjson
import requests
from requests.adapters import HTTPAdapter

from ..scrapper import MtgScrapper
from ..utils import get_fx
from ._base import RateLimitedError, ScraperFetchError

SEARCH_URL = "https://api.scryfall.com/cards/search"

# Scryfall asks clients to identify themselves.
USER_AGENT = "mtgcompare/0.1 (+https://github.com/pvarasa/mtgcompare)"

_SLEEP_BETWEEN_PAGES_S = 0.1


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    # Each scraper makes 1–3 requests per card; the default pool of 10
    # idle connections is wasteful when many scrapers spin up per /decklist.
    adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def parse_page(
    page: dict,
    target_lower: str,
    fx_jpy_per_usd: float,
) -> list[dict]:
    """Extract matching records from a single Scryfall /cards/search page."""
    records: list[dict] = []
    for card in page.get("data") or ():
        if (card.get("name") or "").lower() != target_lower:
            continue
        prices = card.get("prices") or {}
        usd_raw = prices.get("usd")
        if not usd_raw:
            continue
        try:
            price_usd = float(usd_raw)
        except (TypeError, ValueError):
            continue

        purchase_uris = card.get("purchase_uris") or {}
        link = purchase_uris.get("tcgplayer") or card.get("scryfall_uri") or ""

        records.append({
            "shop": "TCGPlayer (Scryfall)",
            "card": card["name"],
            "set": (card.get("set") or "").upper(),
            "price_jpy": round(price_usd * fx_jpy_per_usd, 2),
            "price_usd": price_usd,
            "stock": None,
            "condition": "NM",
            "link": link,
        })
    return records


def parse_search_response(
    pages: list[dict],
    card_name: str,
    fx_jpy_per_usd: float,
) -> list[dict]:
    """Concat parse_page across multiple pages — retained for tests and
    callers that already materialise the full pagination list. Production
    callers should iterate with ``ScryfallScrapper.get_prices``, which
    streams page-by-page."""
    target = card_name.strip().lower()
    records: list[dict] = []
    for page in pages:
        records.extend(parse_page(page, target, fx_jpy_per_usd))
    return records


class ScryfallScrapper(MtgScrapper):
    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("mtgcompare.scrappers.scryfall")

    def get_prices(self, card_name: str) -> list[dict]:
        t0 = monotonic()
        target = card_name.strip().lower()
        records: list[dict] = []
        # Stream pages: parse each into records, then let the page go out
        # of scope before the next fetch. Avoids the multi-MB accumulation
        # that the old _fetch_all_pages → list incurred for popular cards.
        for page in self._iter_pages(card_name):
            records.extend(parse_page(page, target, self.fx))
        self.logger.info(
            "event=shop_query shop='Scryfall' card=%r rows=%d duration_ms=%d",
            card_name, len(records), int((monotonic() - t0) * 1000),
        )
        return records

    def _iter_pages(self, card_name: str) -> Iterator[dict]:
        url = SEARCH_URL
        params: dict | None = {
            "q": f'!"{card_name}"',
            "unique": "prints",
        }
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as e:
                raise ScraperFetchError(f"Scryfall fetch failed: {e}") from e

            if resp.status_code == 404:
                # No cards match — Scryfall returns 404, not empty data.
                # This is the legitimate "no such card" path; don't raise.
                return
            if resp.status_code == 429:
                raise RateLimitedError("Scryfall returned 429")
            if resp.status_code >= 400:
                raise ScraperFetchError(f"Scryfall HTTP {resp.status_code}")

            try:
                # orjson is ~2-3× faster than stdlib json and feeds bytes
                # directly (no Python str copy of the body).
                data = orjson.loads(resp.content)
            except orjson.JSONDecodeError as e:
                raise ScraperFetchError(f"Scryfall JSON decode failed: {e}") from e

            yield data
            if not data.get("has_more"):
                return
            url = data["next_page"]
            params = None
            time.sleep(_SLEEP_BETWEEN_PAGES_S)
