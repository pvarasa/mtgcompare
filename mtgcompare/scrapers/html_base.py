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
- ``PAGE_PARAM`` / ``MAX_PAGES`` (follow result pages; see below)
- ``MAX_CONCURRENT`` / ``MIN_INTERVAL_S`` (pace a shop that rate-limits)

Two behaviours are shared rather than left to each shop:

- **Pagination.** Popular cards overflow the first result page at most
  shops (Sol Ring: 15 pages at SingleStar, 5 at Card Rush, 4 at BLACK FROG),
  and the in-stock English copies are not reliably on page 1. With
  ``PAGE_PARAM`` set, pages are fetched while the page links to the next
  one, up to ``MAX_PAGES`` — a cap, because every page is another request
  inside the decklist fan-out.
- **Multi-face names.** A ``Front // Back`` name that finds nothing is
  retried with the front face alone (``names.query_variants``); parsers
  match listing names with ``names.matched_name``.
- **Rate limits.** A 429 puts the shop in back-off for its ``Retry-After``
  (default 60 s, capped at 10 min): calls inside that window raise
  ``RateLimitedError`` without a request, rather than hammering a shop
  that is already refusing us and extending the ban. Shops that 429 a
  burst (Cardshop Serra) also set ``MAX_CONCURRENT`` / ``MIN_INTERVAL_S``
  so the decklist fan-out — up to MTGCOMPARE_DECKLIST_FAN_OUT_WORKERS
  cards at once (18 in prod), each querying every shop — reaches them at
  a paced rate instead of all at once. Both are per process: one search
  runs in one gunicorn worker, so its own burst is paced, but searches
  in different workers (prod: 2 pods × 2 workers) are not coordinated.

Hareruya and Scryfall don't fit this shape (multi-step API call,
JSON-only) so they keep their own classes; they share ``USER_AGENT``
and ``make_session`` but skip the base class.
"""
import logging
import os
import re
import threading
from contextlib import contextmanager
from time import monotonic, sleep
from typing import ClassVar

import orjson
import requests
from requests.adapters import HTTPAdapter

from ..utils import get_fx
from .base import MtgScrapper
from .names import query_variants

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


_DEFAULT_RETRY_AFTER_S = 60.0
_MAX_RETRY_AFTER_S = 600.0


def retry_after_s(resp: requests.Response) -> float:
    """Seconds a 429 asks us to wait: ``Retry-After`` in delta-seconds
    form, else the default. The HTTP-date form isn't used by any shop we
    scrape and falls back to the default too."""
    headers = getattr(resp, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After", ""))
    except ValueError:
        return _DEFAULT_RETRY_AFTER_S
    return min(max(value, 0.0), _MAX_RETRY_AFTER_S)


class ShopThrottle:
    """Process-wide request pacing and 429 back-off for one shop.

    ``max_concurrent`` caps in-flight requests, ``min_interval_s`` spaces
    request starts; ``None`` / ``0`` leave that dimension unlimited.
    """

    def __init__(self, shop: str, max_concurrent: int | None = None, min_interval_s: float = 0.0):
        self.shop = shop
        self._slots = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None
        self._min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._blocked_until = 0.0

    def reset(self) -> None:
        with self._lock:
            self._next_start = 0.0
            self._blocked_until = 0.0

    def back_off(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, monotonic() + seconds)

    def _check_back_off(self) -> None:
        remaining = self._blocked_until - monotonic()
        if remaining > 0:
            raise RateLimitedError(
                f"{self.shop} rate-limited — backing off for {remaining:.0f}s more"
            )

    @contextmanager
    def slot(self):
        """Hold one request slot for the duration of the ``with`` block."""
        self._check_back_off()
        if self._slots is not None:
            self._slots.acquire()
        try:
            if self._min_interval_s:
                with self._lock:
                    now = monotonic()
                    start = max(now, self._next_start)
                    self._next_start = start + self._min_interval_s
                if start > now:
                    sleep(start - now)
            # A 429 may have landed on another thread while this one queued.
            self._check_back_off()
            yield
        finally:
            if self._slots is not None:
                self._slots.release()


def raise_for_response(resp: requests.Response, shop: str) -> None:
    """Shared transport-status policy: 429 → RateLimitedError, any other
    4xx/5xx → ScraperFetchError. Shop-specific statuses (Scryfall's
    404-means-no-match, TCGPlayer's 403 bot wall) are handled by callers
    before delegating here."""
    if resp.status_code == 429:
        raise RateLimitedError(f"{shop} returned 429 — being rate-limited")
    if resp.status_code >= 400:
        raise ScraperFetchError(f"{shop} HTTP {resp.status_code}")


def decode_json_response(resp: requests.Response, shop: str) -> dict:
    """Decode a JSON response body or raise ScraperFetchError."""
    try:
        # orjson is ~2-3× faster than stdlib json and feeds bytes
        # directly (no Python str copy of the body).
        return orjson.loads(resp.content)
    except orjson.JSONDecodeError as e:
        raise ScraperFetchError(f"{shop} JSON decode failed: {e}") from e


def make_session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if extra_headers:
        s.headers.update(extra_headers)
    # Module-level sessions are shared across the per-decklist fan-out, so
    # the pool must accommodate concurrent in-flight requests per shop:
    # one per fan-out worker. Read from the same env var as
    # decklist.DECKLIST_FAN_OUT_WORKERS — prod runs 18, and a fixed 12 meant
    # the extra connections were discarded after every request and each
    # paid a fresh TLS handshake. (Importing decklist here would be circular.)
    pool = max(12, int(os.environ.get("MTGCOMPARE_DECKLIST_FAN_OUT_WORKERS", "12")))
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def to_usd(price_jpy: float, fx_jpy_per_usd: float) -> float:
    """Convert a JPY price to USD at the given JPY-per-USD rate, rounded to cents.

    The single place the per-shop record-building conversion + rounding
    convention lives, shared by every JPY-sourced scraper.
    """
    return round(price_jpy / fx_jpy_per_usd, 2)


def to_jpy(price_usd: float, fx_jpy_per_usd: float) -> float:
    """Convert a USD price to JPY at the given JPY-per-USD rate, rounded to cents.

    The inverse of ``to_usd``, for the USD-sourced shops (TCGPlayer / Scryfall).
    """
    return round(price_usd * fx_jpy_per_usd, 2)


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
    # Query parameter carrying the 1-based page number; None = single page.
    PAGE_PARAM: ClassVar[str | None] = None
    MAX_PAGES: ClassVar[int] = 3
    # Pacing for shops that 429 bursts; None / 0.0 = unlimited.
    MAX_CONCURRENT: ClassVar[int | None] = None
    MIN_INTERVAL_S: ClassVar[float] = 0.0
    _SHARED_SESSION: ClassVar[requests.Session]
    _THROTTLE: ClassVar[ShopThrottle]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # One Session per subclass, allocated at class-definition time and
        # reused across every instance. Keeps HTTP keep-alive warm across
        # the per-decklist fan-out instead of forcing a fresh TLS handshake
        # for each (card, shop) pair. requests.Session is thread-safe for
        # concurrent GETs.
        cls._SHARED_SESSION = make_session(cls.SESSION_HEADERS or None)
        # Likewise one throttle per subclass, so pacing and back-off span
        # every thread of the fan-out.
        cls._THROTTLE = ShopThrottle(
            getattr(cls, "SHOP_NAME", cls.__name__), cls.MAX_CONCURRENT, cls.MIN_INTERVAL_S,
        )

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
        records: list[dict] = []
        pages = 0
        for query in query_variants(card_name):
            records, n = self._collect_pages(query, card_name)
            pages += n
            if records:
                break
        self.logger.info(
            "event=shop_query shop=%r card=%r rows=%d pages=%d duration_ms=%d",
            self.SHOP_NAME, card_name, len(records), pages,
            int((monotonic() - t0) * 1000),
        )
        return records

    def _collect_pages(self, query: str, card_name: str) -> tuple[list[dict], int]:
        """Search ``query`` and parse every page against ``card_name``."""
        records: list[dict] = []
        seen: set[tuple] = set()
        page = 1
        while True:
            body = self._fetch_search_html(query, page=page)
            for record in self.parse_html(body, card_name):
                # A listing can straddle two pages if the shop's ordering
                # shifts between requests; keep the first copy.
                key = tuple(sorted(record.items()))
                if key not in seen:
                    seen.add(key)
                    records.append(record)
            if page >= self.MAX_PAGES or not self._has_next_page(body, page):
                return records, page
            page += 1

    def page_value(self, page: int) -> str:
        """``PAGE_PARAM``'s value for a 1-based page. Override for shops
        that page by offset rather than page number (TokyoMTG)."""
        return str(page)

    def _has_next_page(self, body: str | bytes, page: int) -> bool:
        if self.PAGE_PARAM is None:
            return False
        # Pager links carry the next page's value; hrefs in HTML are often
        # entity-escaped, hence the optional "amp;".
        value = re.escape(self.page_value(page + 1))
        pattern = rf"[?&](?:amp;)?{re.escape(self.PAGE_PARAM)}={value}(?!\d)"
        if isinstance(body, bytes):
            return re.search(pattern.encode(), body) is not None
        return re.search(pattern, body) is not None

    def _fetch_search_html(self, card_name: str, page: int = 1) -> str | bytes:
        params = self.search_params(card_name)
        if page > 1 and self.PAGE_PARAM is not None:
            params = {**params, self.PAGE_PARAM: self.page_value(page)}
        with self._THROTTLE.slot():
            try:
                resp = self.session.get(
                    self.SEARCH_URL,
                    params=params,
                    timeout=self.REQUEST_TIMEOUT_S,
                )
            except requests.RequestException as e:
                raise ScraperFetchError(f"{self.SHOP_NAME} fetch failed: {e}") from e

        if resp.status_code == 429:
            self._THROTTLE.back_off(retry_after_s(resp))
        raise_for_response(resp, self.SHOP_NAME)
        return self.decode_response(resp)
