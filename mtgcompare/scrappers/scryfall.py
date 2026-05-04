"""Scryfall scraper.

Uses Scryfall's public REST API (https://scryfall.com/docs/api) to look up
per-printing USD prices. Scryfall's `prices.usd` reflects the TCGPlayer
market price — the de-facto US "what does this card cost" reference.

No auth. Respects Scryfall's rate-limit guidance (50-100 ms between calls).

The `parse_search_response` function is pure and is what tests exercise.
"""
import logging
import time
from typing import Optional

import requests

from ..scrapper import MtgScrapper
from ..utils import get_fx
from ._base import RateLimitedError, ScraperFetchError

SEARCH_URL = "https://api.scryfall.com/cards/search"

# Scryfall asks clients to identify themselves.
USER_AGENT = "mtgcompare/0.1 (+https://github.com/pablovarasa/mtgcompare)"

_SLEEP_BETWEEN_PAGES_S = 0.1


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    return s


def parse_search_response(
    pages: list[dict],
    card_name: str,
    fx_jpy_per_usd: float,
) -> list[dict]:
    """Turn Scryfall /cards/search pages into our price record schema.

    Only non-foil prices are returned (for parity with HareruyaScrapper,
    which filters to non-foil English printings).
    """
    target = card_name.strip().lower()
    records: list[dict] = []
    for page in pages:
        for card in page.get("data", []):
            if (card.get("name") or "").lower() != target:
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


class ScryfallScrapper(MtgScrapper):
    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session()
        self.logger = logging.getLogger("scryfall")

    def get_prices(self, card_name: str) -> list[dict]:
        pages = self._fetch_all_pages(card_name)
        if not pages:
            self.logger.info(f"No Scryfall results for {card_name!r}")
            return []
        records = parse_search_response(pages, card_name, self.fx)
        for r in records:
            self.logger.info(
                f"Found {r['card']} [{r['set']}] ${r['price_usd']:.2f} "
                f"(¥{r['price_jpy']:.0f})"
            )
        return records

    def _fetch_all_pages(self, card_name: str) -> list[dict]:
        pages: list[dict] = []
        url = SEARCH_URL
        params: Optional[dict] = {
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
                return pages
            if resp.status_code == 429:
                raise RateLimitedError("Scryfall returned 429")
            if resp.status_code >= 400:
                raise ScraperFetchError(f"Scryfall HTTP {resp.status_code}")

            try:
                data = resp.json()
            except ValueError as e:
                raise ScraperFetchError(f"Scryfall JSON decode failed: {e}") from e

            pages.append(data)
            if not data.get("has_more"):
                return pages
            url = data.get("next_page")
            params = None
            time.sleep(_SLEEP_BETWEEN_PAGES_S)
