"""Tests for the cross-shop fan-out in collect_prices."""
import logging
import time

from mtgcompare import shops
from mtgcompare.scrapper import MtgScrapper


class _SleepyScraper(MtgScrapper):
    """Returns a single canned record after sleeping for `duration` seconds."""

    def __init__(self, shop_name: str, duration: float):
        super().__init__()
        self.shop_name = shop_name
        self.duration = duration

    def get_prices(self, card_name):
        time.sleep(self.duration)
        return [{
            "shop": self.shop_name,
            "card": card_name,
            "set": "TST",
            "price_jpy": 100.0,
            "price_usd": 0.67,
            "stock": 1,
            "condition": "NM",
            "link": f"https://example.com/{self.shop_name}",
        }]


class _FailingScraper(MtgScrapper):
    def get_prices(self, card_name):
        raise RuntimeError("simulated shop outage")


def test_collect_prices_runs_shops_in_parallel(monkeypatch):
    """5 scrapers each sleeping 200 ms must complete in well under 1 s."""
    fake = [_SleepyScraper(f"Shop{i}", 0.2) for i in range(5)]
    monkeypatch.setattr(shops, "build_scrapers", lambda fx: fake)

    t0 = time.perf_counter()
    results = shops.collect_prices("Lightning Bolt", fx=150.0)
    elapsed = time.perf_counter() - t0

    assert len(results) == 5
    assert {r["shop"] for r in results} == {f"Shop{i}" for i in range(5)}
    # Sequential would be 5 × 0.2 = 1.0 s. Parallel should be ~0.2 s.
    # Allow generous slack for CI scheduler jitter.
    assert elapsed < 0.6, f"expected parallel fan-out (~0.2 s), got {elapsed:.2f} s"


def test_collect_prices_isolates_per_scraper_failures(monkeypatch, caplog):
    """One scraper raising should not drop the others' results."""
    fake = [
        _SleepyScraper("OK1", 0.0),
        _FailingScraper(),
        _SleepyScraper("OK2", 0.0),
    ]
    monkeypatch.setattr(shops, "build_scrapers", lambda fx: fake)

    logger = logging.getLogger("test_shops")
    with caplog.at_level(logging.ERROR, logger="test_shops"):
        results = shops.collect_prices("Lightning Bolt", fx=150.0, logger=logger)

    shops_returned = {r["shop"] for r in results}
    assert shops_returned == {"OK1", "OK2"}
    assert any("simulated shop outage" in rec.message for rec in caplog.records)
    assert any("_FailingScraper" in rec.message for rec in caplog.records)


def test_collect_prices_returns_empty_when_all_shops_empty(monkeypatch):
    """Aggregated empty results stay empty (no spurious rows)."""
    class _EmptyScraper(MtgScrapper):
        def get_prices(self, card_name):
            return []

    monkeypatch.setattr(shops, "build_scrapers", lambda fx: [_EmptyScraper(), _EmptyScraper()])
    assert shops.collect_prices("No Such Card", fx=150.0) == []
