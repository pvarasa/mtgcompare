"""Backend-agnostic access to MTGJSON price history.

Local (SQLite) deployments keep price history in a single DuckDB file;
remote (PostgreSQL) deployments keep it in the ``price_rows`` table. Both
expose the same operations through ``PriceHistoryStore`` so the web
layer stops repeating ``if db.IS_POSTGRES:`` at every call site — the one
place that branch survives is ``get_store()``.

``get_store()`` reads ``db.IS_POSTGRES`` afresh on each call (the test
suite flips it per-test), and ``PostgresPriceStore`` goes through
``db.get_conn()`` rather than holding its own connection so test
monkeypatches on the db module still land. The local store funnels every
DuckDB access through a single module-level lock, mirroring the
serialization the web module used to do inline: DuckDB allows one
read-write process, and a concurrent read-only connection during a write
can still race.
"""
import logging
import threading
from datetime import date
from pathlib import Path

import duckdb
from sqlalchemy import text

from .. import db
from . import history_import

logger = logging.getLogger(__name__)

# Shared across every (per-call) DuckDbPriceStore instance so serialization
# holds even though the factory hands out a fresh store on each call.
_duckdb_lock = threading.Lock()


class PriceHistoryStore:
    """Interface for the two price-history backends.

    Concrete implementations: ``PostgresPriceStore`` and
    ``DuckDbPriceStore``. Construct via ``get_store()``.
    """

    def has_history(self) -> bool:
        """Whether any price history exists for the active backend."""
        raise NotImplementedError

    def query(self, uuid: str, finish: str) -> dict[str, float]:
        """Full {date-string: price_usd} series for one (uuid, finish)."""
        raise NotImplementedError

    def portfolio_value_series(
        self, weights: list[tuple[str, str, int]],
    ) -> dict[str, float]:
        """Whole-portfolio USD value per market day, aggregated in the DB.

        ``weights`` is ``[(uuid, finish, quantity), ...]``. Returns
        ``{date-string: total_usd}`` from a single weighted
        ``SUM(qty * price_usd) ... GROUP BY market_date`` — the rows never
        leave the database un-summed, so a 5000-lot portfolio costs one
        query returning one row per priced day, not millions of price rows.
        No forward-fill: a day is only present if at least one held printing
        was priced on it.
        """
        raise NotImplementedError

    def latest_prices(
        self, uuids: list[str],
    ) -> dict[tuple[str, str], float | None]:
        """Latest price per (uuid, finish) for the given uuids.

        Always a dict — an empty one when there are no matching rows.
        Callers that must distinguish "no history at all" gate on
        ``has_history()`` first rather than on the return value.
        """
        raise NotImplementedError

    def rebuild(self, xz_path: Path, *, progress_cb=None) -> int:
        """Full rebuild from an ``AllPrices.json.xz`` file. Returns row count."""
        raise NotImplementedError

    def merge_today(self, xz_path: Path, *, progress_cb=None) -> tuple[int, int]:
        """Upsert an ``AllPricesToday.json.xz`` file. Returns (uuids, rows)."""
        raise NotImplementedError


class PostgresPriceStore(PriceHistoryStore):
    """Price history in the PostgreSQL ``price_rows`` table.

    Reads go through ``db.get_conn()``; rebuild/merge hand the live engine
    to the ``history_import`` COPY pipeline.
    """

    def has_history(self) -> bool:
        with db.get_conn() as conn:
            return conn.execute(
                text("SELECT 1 FROM price_rows LIMIT 1")
            ).fetchone() is not None

    def query(self, uuid: str, finish: str) -> dict[str, float]:
        with db.get_conn() as conn:
            rows = conn.execute(
                text("SELECT market_date, price_usd FROM price_rows"
                     " WHERE uuid = :uuid AND finish = :finish ORDER BY market_date ASC"),
                {"uuid": uuid, "finish": finish},
            ).fetchall()
        return {
            (r[0].isoformat() if isinstance(r[0], date) else str(r[0])): float(r[1])
            for r in rows if r[1] is not None
        }

    def portfolio_value_series(self, weights: list[tuple[str, str, int]]) -> dict[str, float]:
        if not weights:
            return {}
        # A (uuid, finish, qty) VALUES list joined onto price_rows, so the
        # weighted sum happens in-engine. The first VALUES row carries casts
        # (price_rows.uuid is UUID); the rest inherit those column types.
        values_rows: list[str] = []
        # Mixed value types (the uuid-array param plus per-row str/int binds),
        # so annotate explicitly — otherwise the initializer pins params to
        # dict[str, list[str]] and the str/int binds below fail type-checking.
        params: dict[str, object] = {"uuids": sorted({uuid for uuid, _f, _q in weights})}
        for i, (uuid, finish, qty) in enumerate(weights):
            if i == 0:
                values_rows.append(f"(CAST(:u{i} AS uuid), CAST(:f{i} AS text), CAST(:q{i} AS integer))")
            else:
                values_rows.append(f"(:u{i}, :f{i}, :q{i})")
            params[f"u{i}"], params[f"f{i}"], params[f"q{i}"] = uuid, finish, qty
        values_sql = ", ".join(values_rows)
        with db.get_conn() as conn:
            rows = conn.execute(
                # VALUES placeholders are :u0/:f0/:q0…; user values bound via `params`.
                # The `p.uuid = ANY(:uuids)` predicate is logically redundant with
                # the join, but it is the sargable condition the planner needs to
                # pick the `price_rows_covering` index-only scan. Without it the
                # planner seq-scans all of price_rows (≈19M rows, seconds); with it
                # the same query is sub-second. See db.py `price_rows_covering`.
                text(f"""
                    SELECT p.market_date, SUM(w.qty * p.price_usd) AS total
                    FROM price_rows p
                    JOIN (VALUES {values_sql}) AS w(uuid, finish, qty)
                      ON p.uuid = w.uuid AND p.finish = w.finish
                    WHERE p.uuid = ANY(:uuids) AND p.price_usd IS NOT NULL
                    GROUP BY p.market_date
                    ORDER BY p.market_date
                """),  # noqa: S608
                params,
            ).fetchall()
        return {
            (r[0].isoformat() if isinstance(r[0], date) else str(r[0])): float(r[1])
            for r in rows if r[1] is not None
        }

    def latest_prices(self, uuids: list[str]) -> dict[tuple[str, str], float | None]:
        if not uuids:
            return {}
        params = {f"u{i}": u for i, u in enumerate(uuids)}
        placeholders = ", ".join(f":u{i}" for i in range(len(uuids)))
        with db.get_conn() as conn:
            rows = conn.execute(
                # placeholders are :u0, :u1, …; user values bound via `params`.
                text(f"""
                    SELECT DISTINCT ON (uuid, finish) uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({placeholders})
                    ORDER BY uuid, finish, market_date DESC
                """),  # noqa: S608
                params,
            ).fetchall()
        return {
            (str(r[0]), r[1]): float(r[2]) if r[2] is not None else None
            for r in rows
        }

    def rebuild(self, xz_path: Path, *, progress_cb=None) -> int:
        return history_import.rebuild_history_pg(xz_path, db.engine, progress_cb=progress_cb)

    def merge_today(self, xz_path: Path, *, progress_cb=None) -> tuple[int, int]:
        return history_import.merge_today_prices_pg(xz_path, db.engine, progress_cb=progress_cb)


class DuckDbPriceStore(PriceHistoryStore):
    """Price history in a local DuckDB file.

    Every access is serialized through the module-level ``_duckdb_lock``;
    reads use a fresh read-only connection.
    """

    def __init__(self, duckdb_path: Path):
        self.duckdb_path = duckdb_path

    def has_history(self) -> bool:
        return self.duckdb_path.exists()

    def query(self, uuid: str, finish: str) -> dict[str, float]:
        if not self.duckdb_path.exists():
            return {}
        with _duckdb_lock:
            conn = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                rows = conn.execute(
                    "SELECT market_date, price_usd FROM price_rows "
                    "WHERE uuid = ? AND finish = ? ORDER BY market_date ASC",
                    [uuid, finish],
                ).fetchall()
            finally:
                conn.close()
        return {row[0]: row[1] for row in rows if row[1] is not None}

    def portfolio_value_series(self, weights: list[tuple[str, str, int]]) -> dict[str, float]:
        if not weights or not self.duckdb_path.exists():
            return {}
        # Same shape as the Postgres path: a (uuid, finish, qty) VALUES list
        # joined onto price_rows so DuckDB does the weighted sum. uuid is
        # VARCHAR here; the first row's casts pin the VALUES column types.
        values_rows, binds = [], []
        for i, (uuid, finish, qty) in enumerate(weights):
            values_rows.append("(?::VARCHAR, ?::VARCHAR, ?::INTEGER)" if i == 0 else "(?, ?, ?)")
            binds.extend((uuid, finish, qty))
        values_sql = ", ".join(values_rows)
        with _duckdb_lock:
            conn = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                rows = conn.execute(
                    # VALUES placeholders are positional `?`; values bound via `binds`.
                    f"""
                    SELECT p.market_date, SUM(w.qty * p.price_usd) AS total
                    FROM price_rows p
                    JOIN (VALUES {values_sql}) AS w(uuid, finish, qty)
                      ON p.uuid = w.uuid AND p.finish = w.finish
                    WHERE p.price_usd IS NOT NULL
                    GROUP BY p.market_date
                    ORDER BY p.market_date
                    """,  # noqa: S608
                    binds,
                ).fetchall()
            finally:
                conn.close()
        return {row[0]: float(row[1]) for row in rows if row[1] is not None}

    def latest_prices(self, uuids: list[str]) -> dict[tuple[str, str], float | None]:
        if not self.duckdb_path.exists() or not uuids:
            return {}
        placeholders = ", ".join("?" for _ in uuids)
        with _duckdb_lock:
            conn = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                rows = conn.execute(
                    # placeholders are positional `?`; user values bound via `uuids`.
                    f"""
                    SELECT uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({placeholders})
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY uuid, finish ORDER BY market_date DESC) = 1
                    """,  # noqa: S608
                    uuids,
                ).fetchall()
            finally:
                conn.close()
        return {(r[0], r[1]): r[2] for r in rows}

    def rebuild(self, xz_path: Path, *, progress_cb=None) -> int:
        with _duckdb_lock:
            return history_import.rebuild_history_db(xz_path, self.duckdb_path, progress_cb=progress_cb)

    def merge_today(self, xz_path: Path, *, progress_cb=None) -> tuple[int, int]:
        with _duckdb_lock:
            return history_import.merge_today_prices(xz_path, self.duckdb_path, progress_cb=progress_cb)


def get_store(duckdb_path: Path | None = None) -> PriceHistoryStore:
    """Return the price-history store for the active backend.

    Reads ``db.IS_POSTGRES`` on each call so per-test backend flips are
    honoured. ``duckdb_path`` is required for the local backend and ignored
    for PostgreSQL.
    """
    if db.IS_POSTGRES:
        return PostgresPriceStore()
    if duckdb_path is None:
        raise ValueError("duckdb_path is required for the local DuckDB backend")
    return DuckDbPriceStore(duckdb_path)
