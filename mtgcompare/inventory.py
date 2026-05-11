"""Inventory (owned-cards) storage + Deckbox-style CSV import.

The Deckbox / CardCastle export has three quirks we handle:
- UTF-8 BOM on the first byte.
- An `"sep=,"` Excel hint line before the real header.
- A blank line between every record.

Import strategy is clobber-replace — the CSV is the source of truth.

Usage:
    uv run python -m mtgcompare.inventory import binder.csv
    uv run python -m mtgcompare.inventory stats
"""
import argparse
import csv
import sys
from collections.abc import Iterator
from typing import IO

from sqlalchemy import bindparam, text

from .db import get_conn, init_schema, row_to_dict


def _rows_from_csv(f: IO[str]) -> Iterator[dict]:
    """Yield record dicts from a Deckbox-style CSV stream.

    Assumes the caller opened with `encoding="utf-8-sig"` so the BOM is
    already stripped.
    """
    first = f.readline()
    if not first.startswith('"sep='):
        f.seek(0)
    non_empty = (line for line in f if line.strip())
    yield from csv.DictReader(non_empty)


def _to_int(s: str) -> int:
    return int(s.strip()) if s.strip() else 0


def _to_float(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _dict(r: dict, user_id: str) -> dict:
    return {
        "user_id":      user_id,
        "card_name":    r["card_name"],
        "set_code":     r["set_code"],
        "set_name":     r.get("set_name") or "",
        "card_number":  r.get("card_number") or "",
        "quantity":     r["quantity"],
        "condition":    r.get("condition") or "NM",
        "printing":     r.get("printing") or "Normal",
        "language":     r.get("language") or "English",
        "price_bought": r.get("price_bought"),
        "date_bought":  r.get("date_bought"),
    }


_INSERT_SQL = text("""
    INSERT INTO inventory
        (user_id, card_name, set_code, set_name, card_number, quantity,
         condition, printing, language, price_bought, date_bought)
    VALUES
        (:user_id, :card_name, :set_code, :set_name, :card_number, :quantity,
         :condition, :printing, :language, :price_bought, :date_bought)
""")


def import_csv(path: str, replace: bool = True, user_id: str = "local") -> int:
    """Load a Deckbox CSV at `path`. `replace=True` clobbers the user's rows first."""
    init_schema()
    with open(path, encoding="utf-8-sig", newline="") as f:
        records = [
            {
                "card_name":    r["Card Name"].strip(),
                "set_code":     r["Set Code"].strip(),
                "set_name":     r.get("Set Name", "").strip(),
                "card_number":  r.get("Card Number", "").strip(),
                "quantity":     _to_int(r["Quantity"]),
                "condition":    r.get("Condition", "").strip(),
                "printing":     r.get("Printing", "").strip(),
                "language":     r.get("Language", "").strip(),
                "price_bought": _to_float(r.get("Price Bought", "")),
                "date_bought":  r.get("Date Bought", "").strip() or None,
            }
            for r in _rows_from_csv(f)
        ]
    with get_conn() as conn:
        if replace:
            conn.execute(
                text("DELETE FROM inventory WHERE user_id = :uid"),
                {"uid": user_id},
            )
        conn.execute(_INSERT_SQL, [_dict(r, user_id) for r in records])
    return len(records)


def add_one(record: dict, user_id: str = "local") -> None:
    """Append a single lot to the inventory table."""
    init_schema()
    with get_conn() as conn:
        conn.execute(_INSERT_SQL, _dict(record, user_id))


def add_many(records: list[dict], user_id: str = "local") -> int:
    """Append multiple lots in a single transaction. Returns count inserted."""
    if not records:
        return 0
    init_schema()
    with get_conn() as conn:
        conn.execute(_INSERT_SQL, [_dict(r, user_id) for r in records])
    return len(records)


def list_all(user_id: str = "local") -> list[dict]:
    """Return all inventory rows for the given user."""
    with get_conn() as conn:
        rows = conn.execute(
            text("""SELECT id, card_name, set_code, set_name, card_number, quantity,
                          condition, printing, language, price_bought, date_bought
                   FROM inventory
                   WHERE user_id = :uid
                   ORDER BY card_name, set_code, card_number"""),
            {"uid": user_id},
        ).mappings().all()
    return [row_to_dict(r) for r in rows]


def delete(ids: list[int], user_id: str = "local") -> int:
    """Delete inventory rows by id, scoped to user_id. Returns rows affected.

    The user_id clause is the ownership gate: a user cannot delete another
    user's rows even if they guess the id.
    """
    if not ids:
        return 0
    with get_conn() as conn:
        result = conn.execute(
            text("DELETE FROM inventory WHERE user_id = :uid AND id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"uid": user_id, "ids": ids},
        )
    return result.rowcount or 0


def list_all_global() -> list[dict]:
    """Return all inventory rows across all users (used for shared price updates)."""
    with get_conn() as conn:
        rows = conn.execute(
            text("""SELECT card_name, set_code, set_name, card_number, quantity,
                          condition, printing, language, price_bought, date_bought
                   FROM inventory
                   ORDER BY card_name, set_code, card_number"""),
        ).mappings().all()
    return [row_to_dict(r) for r in rows]


def stats(user_id: str = "local") -> dict:
    with get_conn() as conn:
        row = conn.execute(
            text("""SELECT COUNT(*) AS printings,
                          COALESCE(SUM(quantity), 0) AS total_copies,
                          COALESCE(SUM(quantity * COALESCE(price_bought, 0)), 0.0) AS total_cost
                   FROM inventory
                   WHERE user_id = :uid"""),
            {"uid": user_id},
        ).mappings().first()
    row = row_to_dict(row)
    return {
        "printings":    row["printings"],
        "total_copies": row["total_copies"],
        "total_cost":   round(float(row["total_cost"]), 2),
    }


# ---------------------------------------------------------------------------
# Paginated / filtered read API used by the /inventory and /market pages.
#
# Everything that takes filters builds the same WHERE fragment, so calls
# stay consistent across the page (table query, count, aggregate, bulk
# delete). Filter args are bound parameters — no SQL injection surface.
#
# Sort column names are whitelisted; anything outside the set falls back
# to card_name. The whitelist's job is purely to keep user input out of
# the ORDER BY string — it does NOT imply each column is covered by an
# index.
# ---------------------------------------------------------------------------

_SORT_COLUMNS = {
    "card_name", "set_code", "quantity", "price_bought",
    "condition", "printing", "date_bought",
}

_PRICE_MODES = {"any", "empty", "has", "lte", "gte", "eq"}

_SELECT_COLUMNS = (
    "id, card_name, set_code, set_name, card_number, quantity, "
    "condition, printing, language, price_bought, date_bought"
)


def _filter_clause(q: str | None, price_mode: str | None,
                   price_value: float | None) -> tuple[str, dict]:
    """Build the WHERE-clause fragment + bind dict for the inventory filters.

    The returned string has no leading AND/WHERE — callers prepend whichever
    they need (the user_id clause is always present, so callers join with AND).
    """
    parts: list[str] = []
    binds: dict = {}

    if q:
        # Substring match — matches "Beacon Bolt" / "Firebolt" when the
        # user types "bolt". The `WHERE user_id = ...` clause narrows to
        # one user first, so the per-user scan is over hundreds of rows,
        # not millions.
        parts.append("lower(card_name) LIKE :q")
        binds["q"] = "%" + q.lower() + "%"

    if price_mode in _PRICE_MODES and price_mode != "any":
        if price_mode == "empty":
            parts.append("price_bought IS NULL")
        elif price_mode == "has":
            parts.append("price_bought IS NOT NULL")
        elif price_value is not None:
            op = {"lte": "<=", "gte": ">=", "eq": "="}[price_mode]
            parts.append(f"price_bought {op} :price_v")
            binds["price_v"] = price_value

    return (" AND ".join(parts), binds)


def _user_where(user_id: str, *, q: str | None = None,
                price_mode: str | None = None,
                price_value: float | None = None) -> tuple[str, dict]:
    """Build the full `user_id = :uid [AND filter...]` clause + binds.

    All four paginated functions interpolate the returned string into a
    SQL f-string. The fragment only contains the :uid placeholder and
    whitelisted filter clauses produced by `_filter_clause`; user input
    is exclusively bound, never concatenated.
    """
    where_extra, binds = _filter_clause(q, price_mode, price_value)
    binds["uid"] = user_id
    sql = "user_id = :uid" + (f" AND {where_extra}" if where_extra else "")
    return sql, binds


def _order_by_sort(sort: str | None, direction: str | None) -> str:
    """Return the ORDER BY clause body (column names only, no leading keyword)."""
    col = sort if sort in _SORT_COLUMNS else "card_name"
    dir_norm = "DESC" if (direction or "").lower() == "desc" else "ASC"
    # Stable secondary sort so paginated views don't shuffle rows that
    # happen to be tied on the primary sort key.
    tail = "set_code, card_number, id" if col == "card_name" \
        else "card_name, set_code, card_number, id"
    return f"{col} {dir_norm}, {tail}"


def list_paginated(
    user_id: str,
    *,
    q: str | None = None,
    sort: str | None = "card_name",
    direction: str | None = "asc",
    page: int = 1,
    per_page: int = 50,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> list[dict]:
    """Return a single page of the user's inventory matching the filters.

    `page` is 1-indexed; `per_page` is clamped by the caller.
    """
    where_sql, binds = _user_where(user_id, q=q,
                                   price_mode=price_mode, price_value=price_value)
    order_sql = _order_by_sort(sort, direction)
    binds.update({"lim": per_page, "off": (page - 1) * per_page})

    with get_conn() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM inventory "  # noqa: S608
                 f"WHERE {where_sql} ORDER BY {order_sql} "
                 f"LIMIT :lim OFFSET :off"),
            binds,
        ).mappings().all()
    return [row_to_dict(r) for r in rows]


def count_matching(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> int:
    """Count inventory rows for the user that match the same filters as list_paginated."""
    where_sql, binds = _user_where(user_id, q=q,
                                   price_mode=price_mode, price_value=price_value)
    with get_conn() as conn:
        row = conn.execute(
            text(f"SELECT count(*) FROM inventory WHERE {where_sql}"),  # noqa: S608
            binds,
        ).scalar()
    return int(row or 0)


def aggregate_inventory(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> dict:
    """Aggregate stats for the rows matching the filter (NOT just the current page)."""
    where_sql, binds = _user_where(user_id, q=q,
                                   price_mode=price_mode, price_value=price_value)
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) AS printings, "  # noqa: S608
                 "COALESCE(SUM(quantity), 0) AS total_copies, "
                 "COALESCE(SUM(quantity * COALESCE(price_bought, 0)), 0.0) AS total_cost "
                 f"FROM inventory WHERE {where_sql}"),
            binds,
        ).mappings().first()
    row = row_to_dict(row)
    return {
        "printings":    int(row["printings"]),
        "total_copies": int(row["total_copies"]),
        "total_cost":   round(float(row["total_cost"]), 2),
    }


def delete_matching(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> int:
    """Delete all inventory rows for the user that match the filter.

    Refuses to run with no filter (would wipe the user's inventory) — for
    that, use a SQL truncate / the /inventory/import replace flow instead.
    """
    if not q and price_mode in (None, "", "any"):
        raise ValueError("delete_matching requires at least one filter")
    where_sql, binds = _user_where(user_id, q=q,
                                   price_mode=price_mode, price_value=price_value)
    with get_conn() as conn:
        result = conn.execute(
            text(f"DELETE FROM inventory WHERE {where_sql}"),  # noqa: S608
            binds,
        )
    return result.rowcount or 0


def list_filtered_for_market(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> list[dict]:
    """Return ALL matching rows (no pagination, no sort).

    Used by the /market route on SQLite (where the joined SQL path isn't
    available). Python-side join + sort + paginate happens in the caller.
    """
    where_sql, binds = _user_where(user_id, q=q,
                                   price_mode=price_mode, price_value=price_value)
    with get_conn() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM inventory "  # noqa: S608
                 f"WHERE {where_sql} "
                 "ORDER BY card_name, set_code, card_number, id"),
            binds,
        ).mappings().all()
    return [row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Postgres-only /market path: push the LEFT JOIN against market_prices,
# the PnL math, and the pagination into SQL. Cuts per-request cost on
# /market from O(N) Python work down to two indexed-friendly queries
# returning O(50) rows + O(1) aggregates.
#
# The join key normalization (lower + split on '_') mirrors what the
# Python path does — see _normalize_set_code in web.py.
# ---------------------------------------------------------------------------

# SQL expressions for each sort column. Bare column refs for indexed
# inventory columns, SELECT-list aliases for derived/JOIN'd columns
# (Postgres allows the alias in ORDER BY).
_MARKET_SORT_TO_SQL = {
    "card_name":        "inv.card_name",
    "set_code":         "inv.set_code",
    "quantity":         "inv.quantity",
    "price_bought":     "inv.price_bought",
    "market_price_usd": "market_price_usd",
    # Sort by JPY is monotonic with USD * qty (fx is a positive constant),
    # so the ordering matches without needing the runtime fx value.
    "market_value_jpy": "(mp.price_usd * inv.quantity)",
    "pnl_usd":          "pnl_usd",
    "pnl_pct":          "pnl_pct",
}

_MARKET_JOIN_CLAUSE = """
    LEFT JOIN market_prices mp
      ON lower(mp.card_name) = lower(inv.card_name)
      AND lower(mp.set_code) = lower(split_part(inv.set_code, '_', 1))
      AND mp.is_foil = CASE WHEN lower(inv.printing) = 'foil' THEN 1 ELSE 0 END
"""


def list_market_paginated(
    user_id: str,
    *,
    q: str | None = None,
    sort: str = "pnl_usd",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 50,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> list[dict]:
    """Paginated /market rows with prices joined and PnL pre-computed in SQL.

    Postgres-only — uses `split_part`. Caller is responsible for adding
    `market_price_jpy` and `market_value_jpy` (those depend on the
    runtime FX rate, which is a Python value).
    """
    from .db import IS_POSTGRES
    if not IS_POSTGRES:
        raise NotImplementedError("list_market_paginated requires PostgreSQL")

    where_extra, binds = _filter_clause(q, price_mode, price_value)
    where_sql = "inv.user_id = :uid" + (f" AND {where_extra}" if where_extra else "")
    binds["uid"] = user_id

    sort_expr = _MARKET_SORT_TO_SQL.get(sort, _MARKET_SORT_TO_SQL["pnl_usd"])
    dir_norm = "DESC NULLS LAST" if (direction or "").lower() == "desc" else "ASC NULLS LAST"
    binds.update({"lim": per_page, "off": (page - 1) * per_page})

    # The CASE expressions match the rounding the Python path applied so
    # outputs are byte-for-byte equivalent across the two code paths.
    sql = text(f"""
        SELECT
          inv.id, inv.card_name, inv.set_code, inv.set_name, inv.card_number,
          inv.quantity, inv.condition, inv.printing, inv.language,
          inv.price_bought, inv.date_bought,
          mp.price_usd AS market_price_usd,
          CASE WHEN inv.price_bought IS NOT NULL
               THEN ROUND((inv.price_bought * inv.quantity)::numeric, 2)
          END AS cost_basis_usd,
          CASE WHEN mp.price_usd IS NOT NULL
               THEN ROUND((mp.price_usd * inv.quantity)::numeric, 2)
          END AS market_value_usd,
          CASE WHEN inv.price_bought IS NOT NULL AND mp.price_usd IS NOT NULL
               THEN ROUND(((mp.price_usd - inv.price_bought) * inv.quantity)::numeric, 2)
          END AS pnl_usd,
          CASE WHEN inv.price_bought > 0 AND mp.price_usd IS NOT NULL
               THEN ROUND(((mp.price_usd / inv.price_bought - 1) * 100)::numeric, 1)
          END AS pnl_pct
        FROM inventory inv
        {_MARKET_JOIN_CLAUSE}
        WHERE {where_sql}
        ORDER BY {sort_expr} {dir_norm},
                 inv.card_name, inv.set_code, inv.card_number, inv.id
        LIMIT :lim OFFSET :off
    """)  # noqa: S608

    with get_conn() as conn:
        rows = conn.execute(sql, binds).mappings().all()
    return [row_to_dict(r) for r in rows]


def aggregate_market(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> dict:
    """SQL aggregate for the /market summary.

    Computed across the whole filtered set (not just the page), so the
    "Cost basis $X" totals stay correct regardless of pagination state.
    Postgres-only — caller must dispatch on IS_POSTGRES.
    """
    from .db import IS_POSTGRES
    if not IS_POSTGRES:
        raise NotImplementedError("aggregate_market requires PostgreSQL")

    where_extra, binds = _filter_clause(q, price_mode, price_value)
    where_sql = "inv.user_id = :uid" + (f" AND {where_extra}" if where_extra else "")
    binds["uid"] = user_id

    sql = text(f"""
        SELECT
          COUNT(*) AS lots_total,
          COALESCE(SUM(CASE WHEN inv.price_bought IS NOT NULL
                            THEN inv.price_bought * inv.quantity END), 0) AS total_cost_usd,
          COALESCE(SUM(CASE WHEN mp.price_usd IS NOT NULL
                            THEN mp.price_usd * inv.quantity END), 0) AS total_market_usd,
          COALESCE(SUM(CASE WHEN inv.price_bought IS NOT NULL AND mp.price_usd IS NOT NULL
                            THEN (mp.price_usd - inv.price_bought) * inv.quantity END), 0) AS total_pnl_usd,
          COUNT(CASE WHEN inv.price_bought IS NULL THEN 1 END) AS lots_no_cost,
          COUNT(CASE WHEN mp.price_usd IS NULL THEN 1 END) AS lots_no_market,
          COUNT(CASE WHEN inv.price_bought IS NOT NULL AND mp.price_usd IS NOT NULL
                     THEN 1 END) AS lots_in_pnl
        FROM inventory inv
        {_MARKET_JOIN_CLAUSE}
        WHERE {where_sql}
    """)  # noqa: S608

    with get_conn() as conn:
        row = conn.execute(sql, binds).mappings().first()

    row = row_to_dict(row)
    total_cost = float(row["total_cost_usd"])
    total_pnl  = float(row["total_pnl_usd"])
    pnl_pct = round(total_pnl / total_cost * 100, 1) if total_cost > 0 else None
    return {
        "total_cost_usd":   round(total_cost, 2),
        "total_pnl_usd":    round(total_pnl, 2),
        "pnl_pct":          pnl_pct,
        "total_market_usd": round(float(row["total_market_usd"]), 2),
        "lots_total":       int(row["lots_total"]),
        "lots_no_cost":     int(row["lots_no_cost"]),
        "lots_no_market":   int(row["lots_no_market"]),
        "lots_in_pnl":      int(row["lots_in_pnl"]),
    }


def count_market_matching(
    user_id: str,
    *,
    q: str | None = None,
    price_mode: str | None = None,
    price_value: float | None = None,
) -> int:
    """Count of rows the SQL-paginated /market would yield (for total_pages)."""
    return count_matching(user_id, q=q, price_mode=price_mode, price_value=price_value)


def _main() -> None:
    p = argparse.ArgumentParser(prog="inventory")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="import a Deckbox CSV (default: replace)")
    imp.add_argument("path")
    imp.add_argument("--append", action="store_true",
                     help="append instead of replacing the table")
    sub.add_parser("stats", help="print inventory stats")

    args = p.parse_args()
    if args.cmd == "import":
        n = import_csv(args.path, replace=not args.append)
        verb = "Appended" if args.append else "Imported"
        print(f"{verb} {n} rows from {args.path}")
        print(stats())
    elif args.cmd == "stats":
        init_schema()
        print(stats())


if __name__ == "__main__":
    sys.exit(_main())
