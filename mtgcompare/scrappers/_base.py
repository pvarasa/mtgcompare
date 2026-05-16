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
import re
from time import monotonic
from typing import ClassVar

import requests
from requests.adapters import HTTPAdapter

from ..scrapper import MtgScrapper
from ..utils import get_fx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_WHITESPACE_RE = re.compile(r"\s+")


def node_text_ws(node) -> str:
    # selectolax's separator=" " keeps a single space at child boundaries,
    # but nested <wbr/>/<b>/<span class="result_emphasis"> children can still
    # leak runs of whitespace; collapse them so downstream regexes anchor cleanly.
    return _WHITESPACE_RE.sub(" ", node.text(deep=True, separator=" ", strip=True)).strip()


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


def make_session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if extra_headers:
        s.headers.update(extra_headers)
    # Module-level sessions are shared across the per-decklist fan-out, so
    # the pool must accommodate concurrent in-flight requests per shop.
    # Sized to match MTGCOMPARE_DECKLIST_FAN_OUT_WORKERS (default 12).
    adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
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
    _SHARED_SESSION: ClassVar[requests.Session]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # One Session per subclass, allocated at class-definition time and
        # reused across every instance. Keeps HTTP keep-alive warm across
        # the per-decklist fan-out instead of forcing a fresh TLS handshake
        # for each (card, shop) pair. requests.Session is thread-safe for
        # concurrent GETs.
        cls._SHARED_SESSION = make_session(cls.SESSION_HEADERS or None)

    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.session = session if session is not None else self._SHARED_SESSION
        self.logger = logging.getLogger(self.LOGGER_NAME)

    # --- subclasses customise these ---

    def parse_html(self, html: str | bytes, card_name: str) -> list[dict]:
        """Return parsed records. Typically delegates to a module-level
        ``parse_search_html(html, card_name, fx)`` pure function. The
        parser (selectolax) accepts bytes directly and reads the HTML
        meta-charset, so subclasses don't need to decode unless the
        encoding can't be sniffed (BLACK FROG / EUC-JP)."""
        raise NotImplementedError

    def search_params(self, card_name: str) -> dict:
        return {self.SEARCH_PARAM_NAME: card_name}

    def decode_response(self, resp: requests.Response) -> str | bytes:
        """Default: return the raw response bytes, skipping the
        ``resp.text`` decode (which would allocate a second copy of the
        body as a Python str). Subclasses can override to return ``str``
        when the encoding has to be forced (e.g. BLACK FROG / EUC-JP).
        Either is fine for the selectolax parser."""
        return resp.content

    # --- shared scaffolding ---

    def get_prices(self, card_name: str) -> list[dict]:
        # _fetch_search_html raises on transport failure — that propagates
        # so the cache layer can distinguish "shop has no listings"
        # (cacheable) from "we couldn't reach the shop" (don't cache).
        t0 = monotonic()
        body = self._fetch_search_html(card_name)
        records = self.parse_html(body, card_name)
        self.logger.info(
            "event=shop_query shop=%r card=%r rows=%d duration_ms=%d",
            self.SHOP_NAME, card_name, len(records),
            int((monotonic() - t0) * 1000),
        )
        return records

    def _fetch_search_html(self, card_name: str) -> str | bytes:
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
