"""Shared scaffolding for shop scrapers.

Most JP MTG shops follow the same shape:

  GET <SEARCH_URL>?<param>=<card name>
  parse the HTML for product cards
  emit one record per (card × set × condition × language × foil) match

``HtmlSearchScrapper`` captures that shape so each shop file only has
to declare:

- ``SHOP_NAME``     (display name used in records and logs)
- ``SEARCH_URL``    (the GET URL)
- ``LOGGER_NAME``   (logger namespace)
- ``parse_html(self, html, card_name)`` returning records (typically a
  one-line delegation to a module-level pure ``parse_search_html``)

Optional overrides:

- ``SEARCH_PARAM_NAME``  (defaults to ``"keyword"``)
- ``SESSION_HEADERS``    (extra headers merged into the default UA)
- ``search_params(card_name)``   (for endpoints with multiple params)
- ``decode_response(resp)``      (for non-UTF-8 endpoints, e.g. EUC-JP)

Hareruya and Scryfall don't fit this shape (multi-step API call,
JSON-only) so they keep their own classes; they share ``USER_AGENT``
and ``make_session`` but skip the base class.
"""
import logging
from time import monotonic
from typing import ClassVar, Optional

import requests

from ..scrapper import MtgScrapper
from ..utils import get_fx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class ScraperFetchError(Exception):
    """Transport-layer failure: timeout, DNS error, 5xx, decode error.

    Raised by scrapers' fetch helpers when the page itself can't be obtained.
    Callers (CachedScrapper) treat this differently from "fetch succeeded
    but parser found 0 records" — the former must NOT poison the cache,
    the latter is a legitimate negative result.
    """


class RateLimitedError(ScraperFetchError):
    """Specifically a 429 / explicit rate-limit response.

    Subclass of ``ScraperFetchError`` so callers that just need to know
    "the fetch failed, don't cache" can ignore the distinction, while
    rate-limiter logic can pattern-match on the type.
    """


def make_session(extra_headers: Optional[dict] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if extra_headers:
        s.headers.update(extra_headers)
    return s


class HtmlSearchScrapper(MtgScrapper):
    """Convention-based base for one-GET HTML-search shops.

    Subclasses declare class-level constants and override ``parse_html``;
    the base provides ``__init__``, ``get_prices``, fetch + decode +
    log scaffolding.
    """

    SHOP_NAME: ClassVar[str]
    SEARCH_URL: ClassVar[str]
    LOGGER_NAME: ClassVar[str]
    SEARCH_PARAM_NAME: ClassVar[str] = "keyword"
    SESSION_HEADERS: ClassVar[dict] = {}
    REQUEST_TIMEOUT_S: ClassVar[float] = 20.0

    def __init__(
        self,
        fx: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session or make_session(self.SESSION_HEADERS or None)
        self.logger = logging.getLogger(self.LOGGER_NAME)

    # --- subclasses customise these ---

    def parse_html(self, html: str, card_name: str) -> list[dict]:
        """Return parsed records. Typically delegates to a module-level
        ``parse_search_html(html, card_name, fx)`` pure function."""
        raise NotImplementedError

    def search_params(self, card_name: str) -> dict:
        return {self.SEARCH_PARAM_NAME: card_name}

    def decode_response(self, resp: requests.Response) -> str:
        return resp.text

    # --- shared scaffolding ---

    def get_prices(self, card_name: str) -> list[dict]:
        # _fetch_search_html raises on transport failure — that propagates
        # so the cache layer can distinguish "shop has no listings"
        # (cacheable) from "we couldn't reach the shop" (don't cache).
        t0 = monotonic()
        html = self._fetch_search_html(card_name)
        records = self.parse_html(html, card_name)
        self.logger.info(
            "event=shop_query shop=%r card=%r rows=%d duration_ms=%d",
            self.SHOP_NAME, card_name, len(records),
            int((monotonic() - t0) * 1000),
        )
        return records

    def _fetch_search_html(self, card_name: str) -> str:
        try:
            resp = self.session.get(
                self.SEARCH_URL,
                params=self.search_params(card_name),
                timeout=self.REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise ScraperFetchError(f"{self.SHOP_NAME} fetch failed: {e}") from e

        if resp.status_code == 429:
            raise RateLimitedError(
                f"{self.SHOP_NAME} returned 429 — being rate-limited"
            )
        if resp.status_code >= 400:
            raise ScraperFetchError(
                f"{self.SHOP_NAME} HTTP {resp.status_code}"
            )
        return self.decode_response(resp)
