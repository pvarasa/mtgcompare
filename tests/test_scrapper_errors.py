"""Each scraper path must raise ScraperFetchError on transport failure
(rather than silently returning empty results) so the cache layer can
distinguish "shop has no listings" from "we couldn't reach the shop".

This file covers all three fetch paths:
- HtmlSearchScrapper (the base class shared by 7 shops)
- HareruyaScrapper (custom 2-step API)
- ScryfallScrapper (custom JSON API with 404=legit-empty)
"""
import threading
import time

import pytest
import requests

from mtgcompare.scrapers.hareruya import HareruyaScrapper
from mtgcompare.scrapers.html_base import (
    HtmlSearchScrapper,
    RateLimitedError,
    ScraperFetchError,
    retry_after_s,
)
from mtgcompare.scrapers.scryfall import ScryfallScrapper
from mtgcompare.scrapers.serra import CardshopSerraScrapper

# ---------------------------------------------------------------------------
# Helpers — fake requests.Session that returns whatever we want
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", json_payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self._json = json_payload
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception

    def get(self, *_args, **_kwargs):
        if self.exception:
            raise self.exception
        return self.response

    def post(self, *_args, **_kwargs):
        return self.get()


class _DummyHtmlScrapper(HtmlSearchScrapper):
    SHOP_NAME = "DummyShop"
    SEARCH_URL = "https://example.test/search"
    LOGGER_NAME = "test.dummy"

    def parse_html(self, html, card_name):
        return []  # parse never sees error path; transport raises before this


# ---------------------------------------------------------------------------
# HtmlSearchScrapper (covers singlestar / cardrush / serra / blackfrog /
#                     mintmall / enndalgames / tokyomtg)
# ---------------------------------------------------------------------------

class TestHtmlSearchScrapper:
    def _make(self, response=None, exception=None):
        s = _DummyHtmlScrapper(fx=150.0, session=_FakeSession(response, exception))
        return s

    def test_429_raises_rate_limited(self):
        s = self._make(_FakeResponse(429, ""))
        with pytest.raises(RateLimitedError):
            s.get_prices("Mountain")

    def test_500_raises_fetch_error(self):
        s = self._make(_FakeResponse(500, "internal error"))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_403_raises_fetch_error(self):
        s = self._make(_FakeResponse(403, "forbidden"))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_connection_error_raises_fetch_error(self):
        s = self._make(exception=requests.ConnectionError("DNS failed"))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_timeout_raises_fetch_error(self):
        s = self._make(exception=requests.Timeout("timed out"))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_200_with_empty_parse_returns_empty_does_not_raise(self):
        """A 200 response that the parser turns into 0 records is a
        legitimate negative result, not a fetch error."""
        s = self._make(_FakeResponse(200, "<html><body>nothing here</body></html>"))
        assert s.get_prices("Mountain") == []


# ---------------------------------------------------------------------------
# Rate-limit back-off and pacing (HtmlSearchScrapper._THROTTLE)
# ---------------------------------------------------------------------------

class _CountingSession:
    """Returns ``responses`` in order (the last one repeats) and records
    how many GETs ran and the most that were in flight at once."""

    def __init__(self, *responses, delay_s: float = 0.0):
        self.responses = list(responses)
        self.delay_s = delay_s
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def get(self, *_args, **_kwargs):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            resp = self.responses[min(self.calls, len(self.responses)) - 1]
        time.sleep(self.delay_s)
        with self._lock:
            self.in_flight -= 1
        return resp


class _PacedHtmlScrapper(HtmlSearchScrapper):
    SHOP_NAME = "PacedShop"
    SEARCH_URL = "https://example.test/search"
    LOGGER_NAME = "test.paced"
    MAX_CONCURRENT = 1
    MIN_INTERVAL_S = 0.05

    def parse_html(self, html, card_name):
        return []


class TestRateLimitBackOff:
    def test_429_backs_off_without_sending_more_requests(self):
        session = _CountingSession(_FakeResponse(429, headers={"Retry-After": "60"}),
                                   _FakeResponse(200, "<html></html>"))
        s = _DummyHtmlScrapper(fx=150.0, session=session)
        with pytest.raises(RateLimitedError):
            s.get_prices("Sol Ring")
        with pytest.raises(RateLimitedError, match="backing off"):
            s.get_prices("Counterspell")
        assert session.calls == 1

    def test_back_off_ends_after_retry_after(self):
        session = _CountingSession(_FakeResponse(429, headers={"Retry-After": "0"}),
                                   _FakeResponse(200, "<html></html>"))
        s = _DummyHtmlScrapper(fx=150.0, session=session)
        with pytest.raises(RateLimitedError):
            s.get_prices("Sol Ring")
        assert s.get_prices("Sol Ring") == []
        assert session.calls == 2

    def test_back_off_is_per_shop(self):
        with pytest.raises(RateLimitedError):
            _DummyHtmlScrapper(fx=150.0, session=_CountingSession(_FakeResponse(429))).get_prices("X")
        other = _PacedHtmlScrapper(fx=150.0, session=_CountingSession(_FakeResponse(200, "")))
        assert other.get_prices("X") == []

    @pytest.mark.parametrize("headers,expected", [
        ({"Retry-After": "120"}, 120.0),
        ({}, 60.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 60.0),
        ({"Retry-After": "99999"}, 600.0),
    ])
    def test_retry_after_parsing(self, headers, expected):
        assert retry_after_s(_FakeResponse(429, headers=headers)) == expected


class TestPacing:
    def test_min_interval_spaces_request_starts(self):
        s = _PacedHtmlScrapper(fx=150.0, session=_CountingSession(_FakeResponse(200, "")))
        t0 = time.monotonic()
        for _ in range(3):
            s.get_prices("X")
        # Three starts, two enforced gaps.
        assert time.monotonic() - t0 >= 0.1

    def test_max_concurrent_caps_parallel_requests(self):
        session = _CountingSession(_FakeResponse(200, ""), delay_s=0.03)
        threads = [
            threading.Thread(target=_PacedHtmlScrapper(fx=150.0, session=session).get_prices, args=("X",))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert session.calls == 4
        assert session.max_in_flight == 1

    def test_serra_is_paced(self):
        """2026-10-24: a 14-card decklist got a 429 on every Serra lookup."""
        assert CardshopSerraScrapper.MAX_CONCURRENT == 2
        assert CardshopSerraScrapper.MIN_INTERVAL_S >= 1.0


# ---------------------------------------------------------------------------
# HareruyaScrapper (custom 2-step API)
# ---------------------------------------------------------------------------

class TestHareruya:
    def test_unisearch_api_429_raises_rate_limited(self):
        s = HareruyaScrapper(fx=150.0, session=_FakeSession(_FakeResponse(429, "")))
        with pytest.raises(RateLimitedError):
            s.get_prices("Mountain")

    def test_unisearch_api_500_raises_fetch_error(self):
        s = HareruyaScrapper(fx=150.0, session=_FakeSession(_FakeResponse(500, "")))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_unisearch_api_bad_json_raises_fetch_error(self):
        # 200 with non-JSON body (e.g. HTML error page) — JSON decode fails
        s = HareruyaScrapper(fx=150.0, session=_FakeSession(
            _FakeResponse(200, "<html>not json</html>")
        ))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_unisearch_api_empty_docs_returns_empty_does_not_raise(self):
        """Hareruya legitimately returns an empty docs list when no card
        matches — that's "no results", not "fetch failed"."""
        s = HareruyaScrapper(fx=150.0, session=_FakeSession(
            _FakeResponse(200, "{}", json_payload={"response": {"docs": []}})
        ))
        assert s.get_prices("Mountain") == []


# ---------------------------------------------------------------------------
# ScryfallScrapper (404 = legit empty, other errors raise)
# ---------------------------------------------------------------------------

class TestScryfall:
    def test_404_returns_empty_does_not_raise(self):
        """Scryfall's 404 means "no card by that name" — not a fetch error."""
        s = ScryfallScrapper(fx=150.0, session=_FakeSession(_FakeResponse(404, "")))
        assert s.get_prices("Nonexistent Card") == []

    def test_429_raises_rate_limited(self):
        s = ScryfallScrapper(fx=150.0, session=_FakeSession(_FakeResponse(429, "")))
        with pytest.raises(RateLimitedError):
            s.get_prices("Mountain")

    def test_500_raises_fetch_error(self):
        s = ScryfallScrapper(fx=150.0, session=_FakeSession(_FakeResponse(500, "")))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")

    def test_connection_error_raises_fetch_error(self):
        s = ScryfallScrapper(fx=150.0, session=_FakeSession(
            exception=requests.ConnectionError("DNS failed")
        ))
        with pytest.raises(ScraperFetchError):
            s.get_prices("Mountain")
