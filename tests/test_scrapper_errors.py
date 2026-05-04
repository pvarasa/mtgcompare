"""Each scraper path must raise ScraperFetchError on transport failure
(rather than silently returning empty results) so the cache layer can
distinguish "shop has no listings" from "we couldn't reach the shop".

This file covers all three fetch paths:
- HtmlSearchScrapper (the base class shared by 7 shops)
- HareruyaScrapper (custom 2-step API)
- ScryfallScrapper (custom JSON API with 404=legit-empty)
"""
import pytest
import requests

from mtgcompare.scrappers._base import (
    HtmlSearchScrapper,
    RateLimitedError,
    ScraperFetchError,
)
from mtgcompare.scrappers.hareruya import HareruyaScrapper
from mtgcompare.scrappers.scryfall import ScryfallScrapper

# ---------------------------------------------------------------------------
# Helpers — fake requests.Session that returns whatever we want
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", json_payload=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self._json = json_payload

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
