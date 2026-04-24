"""Tests for DuckDB-based MTGJSON history import pipeline."""
import json
import lzma

import duckdb
import pytest

from mtgcompare import history_import


def _make_xz(path, data: dict):
    """Write a mock AllPrices.json.xz for testing."""
    with lzma.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))


_SAMPLE_PRICES = {
    "meta": {"date": "2026-04-23"},
    "data": {
        "uuid-a": {
            "paper": {
                "tcgplayer": {
                    "retail": {
                        "normal": {"2026-04-20": 1.5, "2026-04-21": 1.6},
                        "foil":   {"2026-04-20": 3.0},
                    }
                }
            }
        },
        "uuid-b": {
            "paper": {
                "tcgplayer": {
                    "retail": {
                        "etched": {"2026-04-20": 2.5},
                    }
                }
            }
        },
        "uuid-c": {
            "paper": {"tcgplayer": {"retail": {}}}
        },
    },
}


def test_stream_to_ndjson_writes_all_uuids(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    ndjson_path = tmp_path / "prices.ndjson"

    count = history_import._stream_to_ndjson(xz_path, ndjson_path)

    assert count == 3
    lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    rows = [json.loads(line) for line in lines]
    uuids = {r["uuid"] for r in rows}
    assert uuids == {"uuid-a", "uuid-b", "uuid-c"}


def test_stream_to_ndjson_extracts_retail_maps(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    ndjson_path = tmp_path / "prices.ndjson"

    history_import._stream_to_ndjson(xz_path, ndjson_path)

    rows = {
        json.loads(line)["uuid"]: json.loads(line)
        for line in ndjson_path.read_text().splitlines()
    }
    assert rows["uuid-a"]["normal"] == {"2026-04-20": 1.5, "2026-04-21": 1.6}
    assert rows["uuid-a"]["foil"]   == {"2026-04-20": 3.0}
    assert rows["uuid-a"]["etched"] == {}
    assert rows["uuid-b"]["etched"] == {"2026-04-20": 2.5}
    assert rows["uuid-c"]["normal"] == {}


def _query_price_rows(duckdb_path) -> list[tuple]:
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return conn.execute(
            "SELECT uuid, finish, market_date, price_usd, source_updated "
            "FROM price_rows ORDER BY uuid, finish, market_date"
        ).fetchall()
    finally:
        conn.close()


def test_rebuild_history_db_produces_duckdb(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    duckdb_path = tmp_path / "AllPricesHistory.duckdb"

    row_count = history_import.rebuild_history_db(
        xz_path,
        "2026-04-23T00:00:00+00:00",
        duckdb_path,
    )

    # 2 normal + 1 foil from uuid-a, 1 etched from uuid-b = 4 total
    assert row_count == 4
    assert duckdb_path.exists()

    rows = _query_price_rows(duckdb_path)
    assert len(rows) == 4
    found = {(r[0], r[1], r[2]): r[3] for r in rows}
    assert found[("uuid-a", "normal", "2026-04-20")] == pytest.approx(1.5)
    assert found[("uuid-a", "normal", "2026-04-21")] == pytest.approx(1.6)
    assert found[("uuid-a", "foil",   "2026-04-20")] == pytest.approx(3.0)
    assert found[("uuid-b", "etched", "2026-04-20")] == pytest.approx(2.5)


def test_rebuild_history_db_source_updated_stored(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    duckdb_path = tmp_path / "AllPricesHistory.duckdb"
    ts = "2026-04-23T12:00:00+00:00"

    history_import.rebuild_history_db(xz_path, ts, duckdb_path)

    rows = _query_price_rows(duckdb_path)
    assert all(r[4] == ts for r in rows)


def test_rebuild_history_db_atomic_on_empty_data(tmp_path):
    empty = {"meta": {}, "data": {}}
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, empty)
    duckdb_path = tmp_path / "AllPricesHistory.duckdb"

    with pytest.raises(RuntimeError, match="0 rows"):
        history_import.rebuild_history_db(
            xz_path, "2026-04-23T00:00:00+00:00", duckdb_path
        )

    assert not duckdb_path.exists()
    assert not (tmp_path / "AllPricesHistory.duckdb.tmp").exists()


def test_rebuild_history_db_cleans_up_ndjson(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    duckdb_path = tmp_path / "AllPricesHistory.duckdb"

    history_import.rebuild_history_db(
        xz_path, "2026-04-23T00:00:00+00:00", duckdb_path
    )

    assert not (tmp_path / "AllPrices.ndjson").exists()


def test_merge_today_prices_upserts_into_duckdb(tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    duckdb_path = tmp_path / "AllPricesHistory.duckdb"
    ts1 = "2026-04-23T00:00:00+00:00"
    ts2 = "2026-04-24T00:00:00+00:00"

    history_import.rebuild_history_db(xz_path, ts1, duckdb_path)

    today_data = {
        "meta": {"date": "2026-04-24"},
        "data": {
            "uuid-a": {
                "paper": {
                    "tcgplayer": {
                        "retail": {
                            "normal": {"2026-04-20": 1.7, "2026-04-24": 2.0},
                        }
                    }
                }
            },
        },
    }
    today_xz = tmp_path / "AllPricesToday.json.xz"
    _make_xz(today_xz, today_data)

    merged = history_import.merge_today_prices(today_xz, duckdb_path, ts2)

    assert merged == 2
    rows = _query_price_rows(duckdb_path)
    found = {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows}
    # existing row updated with new price and timestamp
    assert found[("uuid-a", "normal", "2026-04-20")][0] == pytest.approx(1.7)
    assert found[("uuid-a", "normal", "2026-04-20")][1] == ts2
    # new row added
    assert found[("uuid-a", "normal", "2026-04-24")][0] == pytest.approx(2.0)
    # untouched rows from original build still present
    assert ("uuid-b", "etched", "2026-04-20") in found
