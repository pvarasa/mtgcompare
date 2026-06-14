"""Scryfall scraper.

Uses Scryfall's public REST API (https://scryfall.com/docs/api) to look up
per-printing USD prices. Scryfall's `prices.usd` reflects the TCGPlayer
market price — the de-facto US "what does this card cost" reference.

No auth. Respects Scryfall's rate-limit guidance (50-100 ms between calls).

Pages are stream-processed: each page is reduced to compact per-printing
summaries and dropped before the next fetch — popular cards with many
printings produce multi-MB JSON bodies and were the largest single
contributor to /decklist's peak RSS.

Two shops resolve cards through this module — ``ScryfallScrapper``
("TCGPlayer market") and the TCGPlayer ships-to-JP scraper — and the
single-card fan-out runs them concurrently. ``fetch_card_summaries``
memoizes the summary list briefly (with a per-card lock so concurrent
callers coalesce) so one search costs one Scryfall pagination, not two.
"""
import logging
import threading
import time
from collections.abc import Iterator
from time import monotonic

import requests

from ..utils import get_fx
from .base import MtgScrapper
from .html_base import (
    ScraperFetchError,
    decode_json_response,
    raise_for_response,
    to_jpy,
)
from .html_base import make_session as _make_session

SEARCH_URL = "https://api.scryfall.com/cards/search"

# TCGPlayer product page, shared with the ships-to-JP scraper. The bare
# page mixes finishes (cheap foils float to the top), so links built
# from it should append Printing/Language/Condition filters.
TCGPLAYER_PRODUCT_URL = "https://www.tcgplayer.com/product/{product_id}"

# Scryfall asks clients to identify themselves.
USER_AGENT = "mtgcompare/0.1 (+https://github.com/pvarasa/mtgcompare)"

_SLEEP_BETWEEN_PAGES_S = 0.1


def make_session() -> requests.Session:
    return _make_session({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })


# Module-level Session shared across all ScryfallScrapper instances.
_SHARED_SESSION = make_session()


def summarize_page(page: dict, target_lower: str) -> list[dict]:
    """Compact per-printing summaries for exact-name matches on one page.

    One dict per printing: ``name`` (canonical casing), ``set``, ``usd``
    (TCGPlayer market price, float | None), ``tcgplayer_id`` (int | None)
    and ``link``. Everything both TCGPlayer shops need, nothing else —
    summaries for a whole walk stay small enough to memoize while the raw
    pages are dropped one at a time.
    """
    summaries: list[dict] = []
    for card in page.get("data") or ():
        if (card.get("name") or "").lower() != target_lower:
            continue
        usd_raw = (card.get("prices") or {}).get("usd")
        try:
            usd = float(usd_raw) if usd_raw else None
        except (TypeError, ValueError):
            usd = None
        purchase_uris = card.get("purchase_uris") or {}
        summaries.append({
            "name": card["name"],
            "set": (card.get("set") or "").upper(),
            "usd": usd,
            "tcgplayer_id": card.get("tcgplayer_id"),
            "link": purchase_uris.get("tcgplayer") or card.get("scryfall_uri") or "",
        })
    return summaries


def record_from_summary(summary: dict, fx_jpy_per_usd: float) -> dict | None:
    """Project one summary into the shared record shape; None without a price."""
    usd = summary["usd"]
    if usd is None:
        return None
    # prices.usd is the non-foil market price, so link to the matching
    # listing view: Scryfall's affiliate URL opens the unfiltered page,
    # where cheap foils at the top read as a price mismatch.
    if summary["tcgplayer_id"]:
        link = (
            TCGPLAYER_PRODUCT_URL.format(product_id=summary["tcgplayer_id"])
            + "?Language=English&Printing=Normal"
        )
    else:
        link = summary["link"]
    return {
        "shop": "TCGPlayer market",
        "card": summary["name"],
        "set": summary["set"],
        "price_jpy": to_jpy(usd, fx_jpy_per_usd),
        "price_usd": usd,
        "stock": None,
        "condition": "NM",
        "link": link,
    }


def iter_search_pages(
    card_name: str,
    session: requests.Session | None = None,
) -> Iterator[dict]:
    """Yield Scryfall ``/cards/search`` pages for an exact-name query."""
    if session is None:
        session = _SHARED_SESSION
    url = SEARCH_URL
    params: dict | None = {
        "q": f'!"{card_name}"',
        "unique": "prints",
    }
    while True:
        try:
            resp = session.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            raise ScraperFetchError(f"Scryfall fetch failed: {e}") from e

        if resp.status_code == 404:
            # No cards match — Scryfall returns 404, not empty data.
            # This is the legitimate "no such card" path; don't raise.
            return
        raise_for_response(resp, "Scryfall")
        data = decode_json_response(resp, "Scryfall")

        yield data
        if not data.get("has_more"):
            return
        url = data["next_page"]
        params = None
        time.sleep(_SLEEP_BETWEEN_PAGES_S)


def _walk_summaries(card_name: str, session: requests.Session) -> list[dict]:
    target = card_name.strip().lower()
    summaries: list[dict] = []
    for page in iter_search_pages(card_name, session):
        summaries.extend(summarize_page(page, target))
    return summaries


# Memo for fetch_card_summaries. TTL is short — it only needs to span one
# fan-out so the two TCGPlayer shops share a single pagination. The
# per-card lock doubles as a singleflight: the second shop's thread blocks
# on the first walk and then reads the fresh memo entry. Errors are never
# cached. Custom sessions (tests) bypass the memo entirely.
_SUMMARIES_TTL_S = 60.0
_SUMMARIES_MAX = 64
_summaries_cache: dict[str, tuple[float, list[dict]]] = {}
_summaries_locks: dict[str, threading.Lock] = {}
_summaries_guard = threading.Lock()


def _evict_summaries_locked() -> None:
    """Bound both memo dicts; caller holds _summaries_guard.

    Dropping an unheld lock can race a thread that already grabbed the
    lock object — the worst case is two threads walking the same card
    once, which is benign.
    """
    now = monotonic()
    for key in [k for k, (t, _) in _summaries_cache.items()
                if now - t >= _SUMMARIES_TTL_S]:
        del _summaries_cache[key]
    while len(_summaries_cache) > _SUMMARIES_MAX:
        oldest = min(_summaries_cache, key=lambda k: _summaries_cache[k][0])
        del _summaries_cache[oldest]
    for key in [k for k, lock in _summaries_locks.items()
                if k not in _summaries_cache and not lock.locked()]:
        del _summaries_locks[key]


def fetch_card_summaries(
    card_name: str,
    session: requests.Session | None = None,
) -> list[dict]:
    """Per-printing summaries for an exact card name, briefly memoized.

    Consumed by both ``ScryfallScrapper`` (market-price records) and the
    TCGPlayer ships-to-JP scraper (product-id resolution).
    """
    if session is not None and session is not _SHARED_SESSION:
        return _walk_summaries(card_name, session)

    key = " ".join(card_name.strip().lower().split())
    with _summaries_guard:
        lock = _summaries_locks.setdefault(key, threading.Lock())
    with lock:
        cached = _summaries_cache.get(key)
        if cached is not None and monotonic() - cached[0] < _SUMMARIES_TTL_S:
            return cached[1]
        summaries = _walk_summaries(card_name, _SHARED_SESSION)
        with _summaries_guard:
            _summaries_cache[key] = (monotonic(), summaries)
            _evict_summaries_locked()
        return summaries


class ScryfallScrapper(MtgScrapper):
    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session if session is not None else _SHARED_SESSION
        self.logger = logging.getLogger("mtgcompare.scrapers.scryfall")

    def get_prices(self, card_name: str) -> list[dict]:
        t0 = monotonic()
        records = [
            record
            for summary in fetch_card_summaries(card_name, self.session)
            if (record := record_from_summary(summary, self.fx)) is not None
        ]
        self.logger.info(
            "event=shop_query shop='Scryfall' card=%r rows=%d duration_ms=%d",
            card_name, len(records), int((monotonic() - t0) * 1000),
        )
        return records
