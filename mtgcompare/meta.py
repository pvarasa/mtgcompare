"""Key/value access to the ``app_meta`` table.

A tiny generic store the price-import pipeline uses for bookkeeping
(``mtgjson_history_downloaded_at``, ``mtgjson_history_db_row_count``, …).
Functions take a live connection so callers can compose reads and writes
inside a larger transaction.
"""
from sqlalchemy import text

from . import db


def read(conn, key: str) -> str | None:
    row = conn.execute(
        text("SELECT value FROM app_meta WHERE key = :key"), {"key": key}
    ).mappings().first()
    return row["value"] if row else None


def write(conn, key: str, value: str) -> None:
    db.upsert(conn, "app_meta", ["key"], [{"key": key, "value": value}])
