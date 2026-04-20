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
from typing import IO, Iterator

from .db import get_conn, init_schema


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


_INSERT_SQL = """INSERT INTO inventory
    (card_name, set_code, set_name, card_number, quantity,
     condition, printing, language, price_bought, date_bought)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _tuple(r: dict) -> tuple:
    """Coerce a record dict into the _INSERT_SQL parameter tuple."""
    return (
        r["card_name"],
        r["set_code"],
        r.get("set_name") or "",
        r.get("card_number") or "",
        r["quantity"],
        r.get("condition") or "NM",
        r.get("printing") or "Normal",
        r.get("language") or "English",
        r.get("price_bought"),
        r.get("date_bought"),
    )


def import_csv(path: str, replace: bool = True) -> int:
    """Load a Deckbox CSV at `path`. `replace=True` clobbers the table first."""
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
            conn.execute("DELETE FROM inventory")
        conn.executemany(_INSERT_SQL, [_tuple(r) for r in records])
    return len(records)


def add_one(record: dict) -> None:
    """Append a single lot to the inventory table."""
    init_schema()
    with get_conn() as conn:
        conn.execute(_INSERT_SQL, _tuple(record))


def add_many(records: list[dict]) -> int:
    """Append multiple lots in a single transaction. Returns count inserted."""
    if not records:
        return 0
    init_schema()
    with get_conn() as conn:
        conn.executemany(_INSERT_SQL, [_tuple(r) for r in records])
    return len(records)


def list_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT card_name, set_code, set_name, card_number, quantity,
                      condition, printing, language, price_bought, date_bought
               FROM inventory
               ORDER BY card_name, set_code, card_number"""
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*)                            AS printings,
                      COALESCE(SUM(quantity), 0)          AS total_copies,
                      COALESCE(SUM(quantity * COALESCE(price_bought, 0)), 0.0)
                                                          AS total_cost
               FROM inventory"""
        ).fetchone()
    return {
        "printings": row["printings"],
        "total_copies": row["total_copies"],
        "total_cost": round(row["total_cost"], 2),
    }


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
