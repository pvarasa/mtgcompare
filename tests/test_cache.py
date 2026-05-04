"""Tests for the lazy/on-demand caching layer."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

import mtgcompare.db as db_module
from mtgcompare import cache as cache_module
from mtgcompare.cache import (
    CachedScrapper,
    _normalize,
    _Singleflight,
    read_listings,
    read_log,
    replace_listings,
    upsert_log,
)
from mtgcompare.scrapper import MtgScrapper

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)
    # Each test gets a fresh DB, so the cache module's "schema ready" guard
    # must be reset to ensure init runs again against the new engine.
    monkeypatch.setattr(cache_module, "_schema_ready", False)
    db_module.init_schema()
    yield db_module


class FakeScrapper(MtgScrapper):
    """Counts calls, returns canned data, optionally raises."""

    def __init__(self, records=None, raises=None):
        super().__init__()
        self._records = records or []
        self._raises = raises
        self.calls = 0
        self.gate = None  # if set, .wait() before returning — for coalescing tests

    def get_prices(self, card_name):
        self.calls += 1
        if self._raises:
            raise self._raises
        if self.gate is not None:
            self.gate.wait()
        return list(self._records)


def _record(card="Lightning Bolt", price_jpy=200.0, set_code="2X2", stock=4):
    return {
        "shop": "FakeShop",
        "card": card,
        "set": set_code,
        "price_jpy": price_jpy,
        "price_usd": price_jpy / 150.0,
        "stock": stock,
        "condition": "NM",
        "link": f"https://example.com/{set_code}",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestNormalize:
    @pytest.mark.parametrize("inp,expected", [
        ("Lightning Bolt", "lightning bolt"),
        ("  LIGHTNING   bolt  ", "lightning bolt"),
        ("Force of Will", "force of will"),
        ("Force\tof  Will", "force of will"),
    ])
    def test_normalize(self, inp, expected):
        assert _normalize(inp) == expected


# ---------------------------------------------------------------------------
# DB read/write helpers
# ---------------------------------------------------------------------------

class TestStorage:
    def test_replace_then_read(self, test_db):
        rows_in = [_record(set_code="2X2"), _record(set_code="MMQ", price_jpy=180.0)]
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt", rows_in)
        with test_db.get_conn() as conn:
            rows_out = read_listings(conn, "FakeShop", "lightning bolt")
        assert {r["set"] for r in rows_out} == {"2X2", "MMQ"}
        prices = {r["set"]: r["price_jpy"] for r in rows_out}
        assert prices == {"2X2": 200.0, "MMQ": 180.0}

    def test_replace_evicts_old_rows(self, test_db):
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt",
                             [_record(set_code="2X2"), _record(set_code="MMQ")])
        # Second scrape only returns 2X2 — MMQ should be gone
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt",
                             [_record(set_code="2X2", price_jpy=250.0)])
        with test_db.get_conn() as conn:
            rows = read_listings(conn, "FakeShop", "lightning bolt")
        assert len(rows) == 1
        assert rows[0]["set"] == "2X2"
        assert rows[0]["price_jpy"] == 250.0

    def test_replace_with_empty_list_clears_rows(self, test_db):
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt", [_record()])
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt", [])
        with test_db.get_conn() as conn:
            rows = read_listings(conn, "FakeShop", "lightning bolt")
        assert rows == []

    def test_replace_scoped_per_shop(self, test_db):
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt", [_record(set_code="2X2")])
            replace_listings(conn, "OtherShop", "lightning bolt", [_record(set_code="MMQ")])
        with test_db.get_conn() as conn:
            replace_listings(conn, "FakeShop", "lightning bolt", [])
            assert read_listings(conn, "OtherShop", "lightning bolt") != []

    def test_log_upsert_and_read(self, test_db):
        with test_db.get_conn() as conn:
            upsert_log(conn, "FakeShop", "lightning bolt", 3)
        with test_db.get_conn() as conn:
            log = read_log(conn, "FakeShop", "lightning bolt")
        assert log is not None
        assert log["result_count"] == 3
        assert log["status"] == "ok"
        assert log["queried_at"].tzinfo is not None

    def test_log_upsert_overwrites(self, test_db):
        with test_db.get_conn() as conn:
            upsert_log(conn, "FakeShop", "lightning bolt", 3)
            upsert_log(conn, "FakeShop", "lightning bolt", 0)
        with test_db.get_conn() as conn:
            log = read_log(conn, "FakeShop", "lightning bolt")
        assert log["result_count"] == 0


# ---------------------------------------------------------------------------
# CachedScrapper behavior
# ---------------------------------------------------------------------------

class TestCachedScrapper:
    def test_first_call_hits_inner_scrapper(self, test_db):
        inner = FakeScrapper([_record()])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        out = cached.get_prices("Lightning Bolt")
        assert inner.calls == 1
        assert len(out) == 1
        assert out[0]["card"] == "Lightning Bolt"

    def test_second_call_within_ttl_uses_cache(self, test_db):
        inner = FakeScrapper([_record()])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        cached.get_prices("Lightning Bolt")
        out2 = cached.get_prices("Lightning Bolt")
        assert inner.calls == 1, "second call should be served from cache"
        assert out2[0]["card"] == "Lightning Bolt"
        assert out2[0]["price_jpy"] == 200.0

    def test_negative_result_is_cached(self, test_db):
        """A shop that returns nothing for this card shouldn't be re-scraped."""
        inner = FakeScrapper([])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        assert cached.get_prices("Obscure Card") == []
        assert cached.get_prices("Obscure Card") == []
        assert inner.calls == 1

    def test_normalization_makes_calls_equivalent(self, test_db):
        inner = FakeScrapper([_record()])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        cached.get_prices("Lightning Bolt")
        cached.get_prices("LIGHTNING BOLT")
        cached.get_prices("  lightning  bolt  ")
        assert inner.calls == 1

    def test_stale_entry_triggers_refresh(self, test_db, monkeypatch):
        inner = FakeScrapper([_record()])
        # Use a TTL shorter than the artificial age below
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(minutes=10))
        cached.get_prices("Lightning Bolt")

        # Backdate the log entry to make it stale
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        with test_db.get_conn() as conn:
            conn.execute(
                text("UPDATE shop_query_log SET queried_at = :t"
                     " WHERE shop = 'FakeShop' AND card_name = 'lightning bolt'"),
                {"t": old},
            )
        cached.get_prices("Lightning Bolt")
        assert inner.calls == 2

    def test_replace_on_fresh_evicts_sold_out(self, test_db):
        inner = FakeScrapper([_record(set_code="2X2"), _record(set_code="MMQ")])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(minutes=10))
        cached.get_prices("Lightning Bolt")

        # Backdate, then change the inner scraper's response so MMQ is gone
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        with test_db.get_conn() as conn:
            conn.execute(
                text("UPDATE shop_query_log SET queried_at = :t"
                     " WHERE shop = 'FakeShop' AND card_name = 'lightning bolt'"),
                {"t": old},
            )
        inner._records = [_record(set_code="2X2", price_jpy=250.0)]
        out = cached.get_prices("Lightning Bolt")
        assert len(out) == 1
        assert out[0]["set"] == "2X2"
        assert out[0]["price_jpy"] == 250.0

    def test_scraper_exception_does_not_cache(self, test_db):
        inner = FakeScrapper(raises=RuntimeError("network down"))
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        with pytest.raises(RuntimeError):
            cached.get_prices("Lightning Bolt")
        # Next call should retry rather than serve a broken negative-cache entry
        with pytest.raises(RuntimeError):
            cached.get_prices("Lightning Bolt")
        assert inner.calls == 2

    def test_rate_limit_does_not_poison_cache(self, test_db):
        """The TokyoMTG-from-prod scenario: 429 must not be memoized as
        'result_count=0, status=ok'. Otherwise every search for the next
        24h returns nothing."""
        from sqlalchemy import text

        from mtgcompare.scrappers._base import RateLimitedError
        inner = FakeScrapper(raises=RateLimitedError("simulated 429"))
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        with pytest.raises(RateLimitedError):
            cached.get_prices("Mountain")
        # Nothing in shop_query_log
        with test_db.get_conn() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM shop_query_log WHERE shop = 'FakeShop'"
            )).scalar()
        assert n == 0, "rate-limit failure must not produce a cache entry"

    def test_fetch_error_does_not_poison_cache(self, test_db):
        """Same check for the generic ScraperFetchError path."""
        from sqlalchemy import text

        from mtgcompare.scrappers._base import ScraperFetchError
        inner = FakeScrapper(raises=ScraperFetchError("DNS resolution failed"))
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        with pytest.raises(ScraperFetchError):
            cached.get_prices("Mountain")
        with test_db.get_conn() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM shop_query_log WHERE shop = 'FakeShop'"
            )).scalar()
        assert n == 0

    def test_legitimate_empty_result_is_still_cached(self, test_db):
        """Don't break the negative-result memoization: a successful fetch
        that returns 0 rows IS a cacheable signal ("this shop doesn't
        carry this card") and shouldn't be re-scraped for 24h."""
        from sqlalchemy import text
        inner = FakeScrapper([])  # no exception, just empty list
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        assert cached.get_prices("Obscure") == []
        with test_db.get_conn() as conn:
            row = conn.execute(text(
                "SELECT result_count, status FROM shop_query_log WHERE shop = 'FakeShop'"
            )).fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "ok"

    def test_cached_listings_have_correct_shop_and_shape(self, test_db):
        inner = FakeScrapper([_record()])
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))
        cached.get_prices("Lightning Bolt")
        out = cached.get_prices("Lightning Bolt")
        r = out[0]
        for field in ("shop", "card", "set", "price_jpy", "price_usd", "stock", "condition", "link"):
            assert field in r
        assert r["shop"] == "FakeShop"
        assert isinstance(r["price_jpy"], float)


# ---------------------------------------------------------------------------
# TTL config
# ---------------------------------------------------------------------------

class TestTtlConfig:
    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("MTGCOMPARE_CACHE_TTL_HOURS", "0.5")
        assert cache_module._ttl_from_env() == timedelta(hours=0.5)

    def test_unset_falls_back_to_24h(self, monkeypatch):
        monkeypatch.delenv("MTGCOMPARE_CACHE_TTL_HOURS", raising=False)
        assert cache_module._ttl_from_env() == timedelta(hours=24)

    def test_invalid_falls_back_to_24h(self, monkeypatch):
        monkeypatch.setenv("MTGCOMPARE_CACHE_TTL_HOURS", "not-a-number")
        assert cache_module._ttl_from_env() == timedelta(hours=24)


# ---------------------------------------------------------------------------
# Singleflight
# ---------------------------------------------------------------------------

class TestSingleflight:
    def test_concurrent_same_key_runs_once(self):
        sf = _Singleflight()
        gate = threading.Event()
        calls = {"n": 0}

        def slow():
            calls["n"] += 1
            gate.wait()
            return "ok"

        results = []
        threads = []
        for _ in range(5):
            t = threading.Thread(target=lambda: results.append(sf.do("k", slow)))
            t.start()
            threads.append(t)
        # Give threads a moment to enter and coalesce
        import time
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(timeout=2)

        assert calls["n"] == 1
        assert results == ["ok"] * 5

    def test_different_keys_run_independently(self):
        sf = _Singleflight()
        calls = {"a": 0, "b": 0}

        def make_fn(key):
            def f():
                calls[key] += 1
                return key
            return f

        assert sf.do("a", make_fn("a")) == "a"
        assert sf.do("b", make_fn("b")) == "b"
        assert calls == {"a": 1, "b": 1}

    def test_exception_propagates_to_all_waiters(self):
        sf = _Singleflight()
        gate = threading.Event()

        def boom():
            gate.wait()
            raise RuntimeError("nope")

        results = []
        errors = []

        def runner():
            try:
                results.append(sf.do("k", boom))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=runner) for _ in range(3)]
        for t in threads:
            t.start()
        import time
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(timeout=2)

        assert len(errors) == 3
        assert all(isinstance(e, RuntimeError) for e in errors)

    def test_key_freed_after_completion(self):
        sf = _Singleflight()
        sf.do("k", lambda: 1)
        # New call with same key should run again, not coalesce with a stale entry
        assert sf.do("k", lambda: 2) == 2


# ---------------------------------------------------------------------------
# Concurrent CachedScrapper.get_prices
# ---------------------------------------------------------------------------

class TestSchemaBootstrap:
    def test_first_use_creates_tables_on_fresh_db(self, tmp_path, monkeypatch):
        """Standalone mode lands on /search before any inventory call has run
        init_schema. The cache must bootstrap its own tables on first use.
        """
        db_path = tmp_path / "fresh.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        monkeypatch.setattr(db_module, "engine", engine)
        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        monkeypatch.setattr(db_module, "IS_POSTGRES", False)
        monkeypatch.setattr(cache_module, "_schema_ready", False)

        inner = FakeScrapper([_record()])
        cached = CachedScrapper(inner, shop_name="FakeShop")
        # No db_module.init_schema() call — that's the point.
        out = cached.get_prices("Lightning Bolt")
        assert len(out) == 1
        # And the second call should hit the cache that the first call populated.
        cached.get_prices("Lightning Bolt")
        assert inner.calls == 1


class TestConcurrentScrape:
    def test_simultaneous_misses_coalesce(self, test_db):
        """N threads searching the same card on a cold cache → 1 HTTP call."""
        inner = FakeScrapper([_record()])
        inner.gate = threading.Event()
        cached = CachedScrapper(inner, shop_name="FakeShop", ttl=timedelta(hours=24))

        results: list[list[dict]] = []

        def worker():
            results.append(cached.get_prices("Lightning Bolt"))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        import time
        time.sleep(0.05)
        inner.gate.set()
        for t in threads:
            t.join(timeout=2)

        assert inner.calls == 1
        assert len(results) == 4
        for r in results:
            assert len(r) == 1
            assert r[0]["card"] == "Lightning Bolt"
