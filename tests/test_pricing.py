"""MTGJSON pricing: set candidates, UUID resolution, history cutoffs/densify,
portfolio history, market_prices population and cross-process cache invalidation,
and the set-file refresh."""
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

import mtgcompare.db as db_module
from mtgcompare import inventory as inv
from mtgcompare import meta, pricing, web


def test_normalize_set_code_and_foil_helpers():
    assert pricing.normalize_set_code("neo_123") == "neo"
    assert pricing.normalize_set_code("neo_123", upper=True) == "NEO"
    assert pricing.normalize_set_code(None) == ""
    assert pricing.is_foil("Foil") is True
    assert pricing.is_foil("Normal") is False


def test_history_cutoff_for_known_period():
    now = datetime(2026, 4, 22, tzinfo=UTC)
    assert pricing.history_cutoff("1m", now=now) == datetime(2026, 3, 23, tzinfo=UTC)
    assert pricing.history_cutoff("all", now=now) is None


def test_densify_daily_points_fills_gaps():
    points = {
        "2026-04-20": 3.0,
        "2026-04-22": 5.0,
    }

    assert pricing.densify_daily_points(points) == [
        {"market_date": "2026-04-20", "price_usd": 3.0},
        {"market_date": "2026-04-21", "price_usd": None},
        {"market_date": "2026-04-22", "price_usd": 5.0},
    ]


def test_compute_portfolio_history_weights_lots_and_densifies():
    inventory = [
        {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
         "printing": "Normal", "quantity": 2},
        {"card_name": "Mana Crypt", "set_code": "2XM", "card_number": "270",
         "printing": "Foil", "quantity": 1},
    ]
    card_map = {
        ("sol ring", "C21", "263", 0): "uuid-sol",
        ("mana crypt", "2XM", "270", 1): "uuid-crypt-foil",
    }
    captured = {}

    def fake_value_series(weights):
        # The DB-side weighted sum is mocked; we assert the weights the
        # function hands it, then return a totals map with a calendar gap.
        captured["weights"] = sorted(weights)
        return {"2026-01-01": 4.0, "2026-01-03": 106.0}

    result = pricing.compute_portfolio_history(
        inventory, card_map=card_map, value_series=fake_value_series,
    )

    assert captured["weights"] == [
        ("uuid-crypt-foil", "foil", 1),
        ("uuid-sol", "normal", 2),
    ]
    assert result["has_history"] is True
    assert result["lot_count"] == 2
    assert result["mapped_count"] == 2
    assert result["available_since"] == "2026-01-01"
    # densify fills the missing 2026-01-02 with a blank (no forward-fill).
    assert result["points"] == [
        {"market_date": "2026-01-01", "price_usd": 4.0},
        {"market_date": "2026-01-02", "price_usd": None},
        {"market_date": "2026-01-03", "price_usd": 106.0},
    ]


def test_compute_portfolio_history_aggregates_quantity_for_same_printing():
    # Two lots of the same printing (different conditions) collapse to one
    # weighted entry, so the DB query values it once.
    inventory = [
        {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
         "printing": "Normal", "quantity": 2, "condition": "NM"},
        {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
         "printing": "Normal", "quantity": 3, "condition": "LP"},
    ]
    captured = {}

    def fake_value_series(weights):
        captured["weights"] = list(weights)
        return {"2026-01-01": 50.0}

    result = pricing.compute_portfolio_history(
        inventory,
        card_map={("sol ring", "C21", "263", 0): "uuid-sol"},
        value_series=fake_value_series,
    )

    assert captured["weights"] == [("uuid-sol", "normal", 5)]
    assert result["mapped_count"] == 2
    assert result["points"] == [{"market_date": "2026-01-01", "price_usd": 50.0}]


def test_compute_portfolio_history_no_mapped_lots_skips_query():
    def boom(weights):
        raise AssertionError("value_series should not run with no mapped lots")

    result = pricing.compute_portfolio_history(
        [{"card_name": "Nope", "set_code": "ZZZ", "card_number": "1",
          "printing": "Normal", "quantity": 1}],
        card_map={},
        value_series=boom,
    )

    assert result["points"] == []
    assert result["has_history"] is False
    assert result["mapped_count"] == 0
    assert result["lot_count"] == 1
    assert result["available_since"] is None


def test_load_card_map_for_sets_filters_by_set(test_db):
    from mtgcompare.pricing import common as pricing_common
    from mtgcompare.pricing import market_repo

    with db_module.get_conn() as conn:
        market_repo.upsert_card_map(conn, [
            {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
             "is_foil": 0, "uuid": "u-sol", "updated_at": "t"},
            {"card_name": "Mana Crypt", "set_code": "2XM", "card_number": "270",
             "is_foil": 1, "uuid": "u-crypt", "updated_at": "t"},
        ])
        rows = market_repo.load_card_map_for_sets(conn, ["C21"])
        assert {r["uuid"] for r in rows} == {"u-sol"}  # 2XM not scanned
        assert market_repo.load_card_map_for_sets(conn, []) == []

    # The common wrapper narrows by the inventory's own sets and indexes by
    # the same key shape callers resolve with.
    card_map = pricing_common.load_card_map_for_inventory([
        {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263", "printing": "Normal"},
    ])
    assert card_map == {("sol ring", "C21", "263", 0): "u-sol"}


def test_market_history_portfolio_endpoint_wires_mapping_path(test_db):
    # No DuckDB price file exists in SQLite test mode, so the store reports
    # no history — but the route's full mapping path still runs.
    inv.add_one({"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
                 "quantity": 2, "condition": "NM", "printing": "Normal",
                 "language": "English", "price_bought": 1.0, "date_bought": "2025-01-01"})
    inv.add_one({"card_name": "Unmapped", "set_code": "C21", "card_number": "999",
                 "quantity": 1, "condition": "NM", "printing": "Normal",
                 "language": "English", "price_bought": 1.0, "date_bought": "2025-01-01"})
    with db_module.get_conn() as conn:
        from mtgcompare.pricing import market_repo
        market_repo.upsert_card_map(conn, [
            {"card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
             "is_foil": 0, "uuid": "u-sol", "updated_at": "t"},
        ])

    with web.app.test_client() as client:
        resp = client.get("/market/history/portfolio")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["lot_count"] == 2
    assert data["mapped_count"] == 1            # only Sol Ring is in the card map
    assert data["has_history"] is False         # no DuckDB price file in test mode
    assert data["points"] == []
    assert data["default_period"] == "all"


def test_mtgjson_set_candidates_include_trimmed_variants():
    assert pricing.mtgjson_set_candidates("FMB1")[:2] == ["FMB1", "FMB"]


# ---------------------------------------------------------------------------
# _resolve_candidate_uuid
# ---------------------------------------------------------------------------

def _candidates_for_dmr_force_of_will():
    return {
        ("force of will", "DMR", "50"):  {"normal": "uuid-50",  "foil": "uuid-50f"},
        ("force of will", "DMR", "284"): {"normal": "uuid-284", "foil": "uuid-284f"},
        ("force of will", "DMR", "418"): {"normal": "uuid-418"},
    }


def test_resolve_candidate_uuid_exact_match():
    row = {"card_name": "Force of Will", "set_code": "DMR", "card_number": "50", "printing": "Normal"}
    assert pricing.resolve_candidate_uuid(row, _candidates_for_dmr_force_of_will()) == "uuid-50"


def test_resolve_candidate_uuid_falls_back_to_lowest_collector_when_number_mismatched():
    """Manual entry with the wrong card_number should still map to the standard printing
    of (name, set) rather than silently failing — that's how Force of Will entered as
    DMR #1 used to drop off the market page entirely."""
    row = {"card_name": "Force of Will", "set_code": "DMR", "card_number": "1", "printing": "Normal"}
    assert pricing.resolve_candidate_uuid(row, _candidates_for_dmr_force_of_will()) == "uuid-50"


def test_resolve_candidate_uuid_falls_back_when_card_number_empty():
    row = {"card_name": "Force of Will", "set_code": "DMR", "card_number": "", "printing": "Normal"}
    assert pricing.resolve_candidate_uuid(row, _candidates_for_dmr_force_of_will()) == "uuid-50"


def test_resolve_candidate_uuid_respects_finish():
    row = {"card_name": "Force of Will", "set_code": "DMR", "card_number": "999", "printing": "Foil"}
    # Only #50 and #284 have foils; lowest collector wins.
    assert pricing.resolve_candidate_uuid(row, _candidates_for_dmr_force_of_will()) == "uuid-50f"


def test_resolve_candidate_uuid_returns_none_when_set_has_no_match():
    row = {"card_name": "Black Lotus", "set_code": "DMR", "card_number": "1", "printing": "Normal"}
    assert pricing.resolve_candidate_uuid(row, _candidates_for_dmr_force_of_will()) is None


def test_collector_sort_key_orders_numerically():
    nums = ["100", "20", "1", "284", "50"]
    assert sorted(nums, key=pricing.collector_sort_key) == ["1", "20", "50", "100", "284"]


def test_collector_sort_key_plain_before_suffixed():
    nums = ["50a", "50", "50★"]
    assert sorted(nums, key=pricing.collector_sort_key)[0] == "50"


# ---------------------------------------------------------------------------
# _populate_market_prices_from_history — UUID type normalisation
# ---------------------------------------------------------------------------

def test_populate_market_prices_handles_postgres_types(monkeypatch):
    # Simulates psycopg2 behaviour on first run:
    #   - price_rows.uuid is UUID type  → psycopg2 returns uuid.UUID objects
    #   - price_rows.price_usd is NUMERIC → psycopg2 returns decimal.Decimal
    #   - card_maps UUID is a plain str   → from MTGJSON JSON on first load
    # Both mismatches must be resolved so price_usd is stored as a plain float.
    raw_uuid = "df0e1ede-b627-5b95-8eed-0ce7d08a897b"
    card_maps = [("Sol Ring", "C21", "", 0, raw_uuid, "2026-04-29T00:00:00+00:00")]

    captured = {}

    def fake_get_conn():
        import contextlib

        class FakeConn:
            def execute(self, stmt, params=None):
                class FakeResult:
                    def fetchall(self_):
                        return [(uuid.UUID(raw_uuid), "normal", Decimal("1.23"))]

                    def fetchone(self_):
                        # has_history()'s "SELECT 1 FROM price_rows LIMIT 1"
                        # gate — non-None means history is present.
                        return (1,)
                return FakeResult()

        @contextlib.contextmanager
        def _ctx():
            yield FakeConn()

        return _ctx()

    import mtgcompare.db as db_mod

    monkeypatch.setattr(db_mod, "IS_POSTGRES", True)
    monkeypatch.setattr(db_mod, "get_conn", fake_get_conn)
    def fake_upsert(conn, table, cols, rows):
        if table == "market_prices":
            captured["rows"] = rows

    monkeypatch.setattr(db_mod, "upsert", fake_upsert)

    pricing.populate_market_prices_from_history(card_maps, None, "2026-04-29T00:00:00+00:00")

    assert captured["rows"][0]["price_usd"] == pytest.approx(1.23)
    assert isinstance(captured["rows"][0]["price_usd"], float)


# ---------------------------------------------------------------------------
# market_prices per printing + cross-process invalidation
# ---------------------------------------------------------------------------

def _write_market_prices(rows, version):
    with db_module.get_conn() as conn:
        pricing.market_repo.upsert_market_prices(conn, [
            {"card_name": n, "set_code": "MH2", "card_number": num, "is_foil": 0,
             "price_usd": p, "fetched_at": "2026-09-23T00:00:00+00:00"}
            for n, num, p in rows
        ])
        meta.write(conn, pricing.market_repo.PRICES_VERSION_KEY, version)


def test_market_prices_are_keyed_per_collector_number(test_db):
    """A borderless variant in the same set must not take the regular
    printing's price (or vice versa)."""
    pricing.market_cache_clear()
    _write_market_prices([("Ragavan", "138", 50.0), ("Ragavan", "435", 90.0)], "v1")
    lots = [
        {"card_name": "Ragavan", "set_code": "MH2", "card_number": n,
         "printing": "normal", "quantity": 1}
        for n in ("138", "435")
    ]
    price_cache, _ = pricing.get_price_cache()
    priced = pricing.attach_market_prices(lots, price_cache, 150.0, True)
    assert [r["market_price_usd"] for r in priced] == [50.0, 90.0]


def test_price_cache_rebuilds_when_another_process_imports(test_db):
    """An import elsewhere bumps the version key; this worker's cached dict
    must notice without its own market_cache_clear()."""
    pricing.market_cache_clear()
    _write_market_prices([("Ragavan", "138", 50.0)], "v1")
    assert pricing.get_price_cache()[0][("ragavan", "mh2", "138", 0)] == 50.0
    _write_market_prices([("Ragavan", "138", 55.0)], "v2")
    assert pricing.get_price_cache()[0][("ragavan", "mh2", "138", 0)] == 55.0


def test_migrate_recreates_market_prices_without_card_number(test_db):
    with db_module.get_conn() as conn:
        conn.execute(text("DROP TABLE market_prices"))
        conn.execute(text(
            "CREATE TABLE market_prices (card_name TEXT, set_code TEXT, is_foil INTEGER,"
            " price_usd NUMERIC, fetched_at TEXT, PRIMARY KEY (card_name, set_code, is_foil))"
        ))
    db_module.init_schema()
    with db_module.get_conn() as conn:
        assert db_module._column_exists(conn, "market_prices", "card_number")


# ---------------------------------------------------------------------------
# MTGJSON set-file refresh
# ---------------------------------------------------------------------------

def test_set_file_is_refreshed_once_stale(tmp_path, monkeypatch):
    import os

    import requests

    from mtgcompare.pricing import import_service

    path = tmp_path / "MH2.json.xz"
    path.write_bytes(b"old")
    monkeypatch.setattr(import_service, "mtgjson_set_path", lambda _c: path)
    downloads = []

    def fake_download(url, target):
        downloads.append(url)
        target.write_bytes(b"new")

    monkeypatch.setattr(import_service, "download_file", fake_download)

    # Fresh copy: reused.
    assert import_service.download_mtgjson_set_file("MH2") == ("MH2", path)
    assert downloads == []

    # Stale copy: re-downloaded.
    old = time.time() - 2 * import_service._SET_FILE_MAX_AGE_S
    os.utime(path, (old, old))
    import_service.download_mtgjson_set_file("MH2")
    assert len(downloads) == 1 and path.read_bytes() == b"new"

    # Stale copy + failing refresh: fall back to the cached file.
    os.utime(path, (old, old))

    def failing_download(url, target):
        resp = requests.Response()
        resp.status_code = 503
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(import_service, "download_file", failing_download)
    assert import_service.download_mtgjson_set_file("MH2") == ("MH2", path)


# --- per-card chart payload (GET /market/history) ------------------------------

def _map_card(uuid="uuid-1"):
    from mtgcompare.pricing import market_repo
    with db_module.get_conn() as conn:
        market_repo.upsert_card_map(conn, [{
            "card_name": "Sol Ring", "set_code": "C21", "card_number": "263",
            "is_foil": 0, "uuid": uuid, "updated_at": "t",
        }])


def test_card_history_payload_cuts_to_period_and_flags_coverage(test_db, monkeypatch):
    from mtgcompare.pricing import market_service
    _map_card()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(market_service, "query_history", lambda uuid, finish: {
        "2026-08-01": 1.0, "2026-10-20": 2.0, "2026-11-01": 3.0,
    })

    def payload(period, **kw):
        args = {"card_name": "sol ring", "set_code": "C21", "card_number": "263", "foil": False}
        return pricing.card_history_payload(**{**args, **kw}, period=period, now=now)

    month = payload("bogus")                                 # unknown period -> default
    assert month["period"] == "1m" and month["has_history"]
    dates = [p["market_date"] for p in month["points"]]
    # 30 days back from 12:00 on 11-03: 10-04 is before the cutoff, 10-05 isn't.
    assert dates[0] == "2026-10-05" and dates[-1] == "2026-11-03"
    assert month["available_since"] == "2026-10-20"          # first priced day in range
    assert month["points"][-1]["price_usd"] is None          # densified, not carried forward

    everything = payload("all")
    assert everything["points"][0] == {"market_date": "2026-08-01", "price_usd": 1.0}

    unmapped = payload("all", card_name="Other")
    assert unmapped["has_history"] is False and unmapped["points"] == []
