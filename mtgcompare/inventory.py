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

from sqlalchemy import text

from .db import get_conn, init_schema, upsert


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
            text("""SELECT card_name, set_code, set_name, card_number, quantity,
                          condition, printing, language, price_bought, date_bought
                   FROM inventory
                   WHERE user_id = :uid
                   ORDER BY card_name, set_code, card_number"""),
            {"uid": user_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def list_all_global() -> list[dict]:
    """Return all inventory rows across all users (used for shared price updates)."""
    with get_conn() as conn:
        rows = conn.execute(
            text("""SELECT card_name, set_code, set_name, card_number, quantity,
                          condition, printing, language, price_bought, date_bought
                   FROM inventory
                   ORDER BY card_name, set_code, card_number"""),
        ).mappings().all()
    return [dict(r) for r in rows]


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
    return {
        "printings":    row["printings"],
        "total_copies": row["total_copies"],
        "total_cost":   round(float(row["total_cost"]), 2),
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
