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
    monkeypatch.setattr(shops, "build_scrapers", lambda fx, enabled=None: fake)

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
    monkeypatch.setattr(shops, "build_scrapers", lambda fx, enabled=None: fake)

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

    monkeypatch.setattr(shops, "build_scrapers", lambda fx, enabled=None: [_EmptyScraper(), _EmptyScraper()])
    assert shops.collect_prices("No Such Card", fx=150.0) == []


# ---------------------------------------------------------------------------
# Shop filter
# ---------------------------------------------------------------------------

def test_collect_prices_filter_enabled_runs_subset(monkeypatch):
    """Only scrapers whose shop is in `enabled` should be invoked."""
    s_one = _SleepyScraper("Hareruya", 0.0)
    s_two = _SleepyScraper("Card Rush", 0.0)
    s_three = _SleepyScraper("MINT MALL", 0.0)
    captured = {}

    def fake_build(fx, enabled=None):
        captured["enabled"] = enabled
        all_ = [("Hareruya", s_one), ("Card Rush", s_two), ("MINT MALL", s_three)]
        if enabled is None:
            return [s for _, s in all_]
        return [s for name, s in all_ if name in enabled]

    monkeypatch.setattr(shops, "build_scrapers", fake_build)

    out = shops.collect_prices("Card", fx=150.0, enabled={"Card Rush", "MINT MALL"})
    shops_in_results = {r["shop"] for r in out}
    assert shops_in_results == {"Card Rush", "MINT MALL"}
    assert captured["enabled"] == {"Card Rush", "MINT MALL"}


def test_collect_prices_filter_empty_returns_empty(monkeypatch):
    """Empty filter set means user deselected everything — return [] without
    attempting any scrapes (and without raising on max_workers=0)."""
    def fake_build(fx, enabled=None):
        return []  # no scrapers match
    monkeypatch.setattr(shops, "build_scrapers", fake_build)
    assert shops.collect_prices("Card", fx=150.0, enabled=set()) == []


def test_collect_prices_filter_none_runs_all(monkeypatch):
    """Default ``enabled=None`` means "all shops" — regression test that
    we didn't accidentally start dropping shops on the unspecified path."""
    s = _SleepyScraper("X", 0.0)
    monkeypatch.setattr(shops, "build_scrapers", lambda fx, enabled=None: [s])
    out = shops.collect_prices("Card", fx=150.0)
    assert len(out) == 1


def test_active_shops_matches_build_scrapers_default():
    """ACTIVE_SHOPS is hand-maintained alongside build_scrapers and drives the
    filter UI. If the two drift, the filter panel will silently miss shops or
    show ghost ones — catch that in CI."""
    default_scrapers = shops.build_scrapers(fx=150.0)
    # CACHE_ENABLED is True in normal config, so each item is a CachedScrapper
    # with a shop_name attribute. Fall back to class name for the disabled path.
    active_from_build = [getattr(s, "shop_name", s.__class__.__name__) for s in default_scrapers]
    assert active_from_build == shops.ACTIVE_SHOPS


def test_build_scrapers_filter_skips_unselected():
    """build_scrapers with an enabled set that doesn't include some real
    shops produces a smaller list and the names match the filter."""
    raw_all = shops.build_scrapers(fx=150.0)
    filtered = shops.build_scrapers(fx=150.0, enabled={"Hareruya"})
    assert len(filtered) == 1
    assert len(raw_all) > len(filtered)
    # Inner CachedScrapper has shop_name; bare scrapers don't, but
    # CACHE_ENABLED defaults to True so we can rely on it.
    assert filtered[0].shop_name == "Hareruya"
