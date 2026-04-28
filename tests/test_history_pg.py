"""Tests for the PostgreSQL bulk-load path in history_import.py.

These tests require a real PostgreSQL instance.  Run with:

    DATABASE_URL=postgresql+psycopg2://... uv run pytest -m pg

The DATABASE_URL database must exist and the connecting user must have
CREATE TABLE / COPY privileges.  The tests create and drop their own
tables so they are safe to run against a shared dev instance.
"""
import json
import lzma
import os

import pytest
from sqlalchemy import create_engine, text

from mtgcompare import history_import

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def price_rows_table(pg_engine):
    """Create a fresh price_rows table for each test, drop it after."""
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS price_rows"))
        conn.execute(text("""
            CREATE TABLE price_rows (
                uuid        UUID        NOT NULL,
                finish      TEXT        NOT NULL,
                market_date DATE        NOT NULL,
                price_usd   NUMERIC(10,4),
                PRIMARY KEY (uuid, finish, market_date)
            )
        """))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS price_rows"))


def _make_xz(path, data: dict):
    with lzma.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))


_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_SAMPLE_PRICES = {
    "meta": {"date": "2026-04-23"},
    "data": {
        _UUID_A: {
            "paper": {
                "tcgplayer": {
                    "retail": {
                        "normal": {"2026-04-20": 1.5, "2026-04-21": 1.6},
                        "foil":   {"2026-04-20": 3.0},
                    }
                }
            }
        },
        _UUID_B: {
            "paper": {
                "tcgplayer": {
                    "retail": {
                        "etched": {"2026-04-20": 2.5},
                    }
                }
            }
        },
    },
}


def _query_pg(engine) -> list[tuple]:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT CAST(uuid AS TEXT), finish, CAST(market_date AS TEXT), price_usd"
                 " FROM price_rows ORDER BY uuid, finish, market_date")
        ).fetchall()


def test_rebuild_history_pg_loads_all_rows(price_rows_table, pg_engine, tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)

    row_count = history_import.rebuild_history_pg(xz_path, pg_engine)

    # 2 normal + 1 foil from _UUID_A, 1 etched from _UUID_B = 4
    assert row_count == 4
    rows = _query_pg(pg_engine)
    assert len(rows) == 4
    found = {(r[0], r[1], r[2]): float(r[3]) for r in rows}
    assert found[(_UUID_A, "normal", "2026-04-20")] == pytest.approx(1.5)
    assert found[(_UUID_A, "normal", "2026-04-21")] == pytest.approx(1.6)
    assert found[(_UUID_A, "foil",   "2026-04-20")] == pytest.approx(3.0)
    assert found[(_UUID_B, "etched", "2026-04-20")] == pytest.approx(2.5)


def test_rebuild_history_pg_cleans_up_temp_files(price_rows_table, pg_engine, tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)

    history_import.rebuild_history_pg(xz_path, pg_engine)

    assert not (tmp_path / "AllPrices.ndjson").exists()
    assert not (tmp_path / "AllPrices_flat.csv").exists()


def test_rebuild_history_pg_raises_on_empty_data(price_rows_table, pg_engine, tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, {"meta": {}, "data": {}})

    with pytest.raises(RuntimeError, match="0 rows"):
        history_import.rebuild_history_pg(xz_path, pg_engine)


def test_merge_today_prices_pg_upserts(price_rows_table, pg_engine, tmp_path):
    # Seed with initial data
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    history_import.rebuild_history_pg(xz_path, pg_engine)

    today_data = {
        "meta": {"date": "2026-04-24"},
        "data": {
            _UUID_A: {
                "paper": {
                    "tcgplayer": {
                        "retail": {
                            "normal": {"2026-04-20": 1.9, "2026-04-24": 2.1},
                        }
                    }
                }
            },
        },
    }
    today_xz = tmp_path / "AllPricesToday.json.xz"
    _make_xz(today_xz, today_data)

    merged = history_import.merge_today_prices_pg(today_xz, pg_engine)

    assert merged == 2

    rows = _query_pg(pg_engine)
    found = {(r[0], r[1], r[2]): float(r[3]) for r in rows}
    # Existing row updated
    assert found[(_UUID_A, "normal", "2026-04-20")] == pytest.approx(1.9)
    # New row inserted
    assert found[(_UUID_A, "normal", "2026-04-24")] == pytest.approx(2.1)
    # Untouched rows from initial load
    assert (_UUID_B, "etched", "2026-04-20") in found


def test_merge_today_prices_pg_cleans_up_temp_files(price_rows_table, pg_engine, tmp_path):
    xz_path = tmp_path / "AllPrices.json.xz"
    _make_xz(xz_path, _SAMPLE_PRICES)
    history_import.rebuild_history_pg(xz_path, pg_engine)

    today_xz = tmp_path / "AllPricesToday.json.xz"
    _make_xz(today_xz, _SAMPLE_PRICES)
    history_import.merge_today_prices_pg(today_xz, pg_engine)

    assert not (tmp_path / "AllPricesToday.ndjson").exists()
    assert not (tmp_path / "AllPricesToday_flat.csv").exists()
